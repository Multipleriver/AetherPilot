#!/usr/bin/env python3
"""Run the bounded Reviewer workflow without writes or simulation.

The script intentionally has no historical fallback.  It performs two native
read-only schematic queries, inspects one already-completed run, and passes
that run's immutable OP/netlist files to the public ``Harness.review_files``
entry point.  Paths supplied to the script are local inputs; the output
directory contains only sanitized review/execution summaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Prefer the explicit project root from argv, then the source tree that
# contains this entry point, even when the command is launched from another
# working directory or the editable package is not installed.
_ENTRY = Path(__file__).resolve()
_ARGV_ROOT = None
for _index, _value in enumerate(sys.argv):
    if _value == "--project-root" and _index + 1 < len(sys.argv):
        _ARGV_ROOT = Path(sys.argv[_index + 1]).expanduser()
        break
    if _value.startswith("--project-root="):
        _ARGV_ROOT = Path(_value.split("=", 1)[1]).expanduser()
        break
_candidates = ([(_ARGV_ROOT.resolve() if _ARGV_ROOT is not None else _ENTRY.parent)] if _ARGV_ROOT is not None else []) + [_ENTRY.parent, *_ENTRY.parents]
for _candidate in _candidates:
    if (_candidate / "aetherpilot").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from aetherpilot.artifacts import redact
import aetherpilot
from aetherpilot.harness import Harness
from aetherpilot.reviewer import render_review_markdown


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_source_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    package = root / "aetherpilot"
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _root_readback(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    readback = payload.get("readback", payload)
    if not isinstance(readback, dict):
        raise RuntimeError(f"readback is not an object: {path}")
    records = readback.get("records", [])
    root = next((item for item in records if isinstance(item, dict) and item.get("path") == [] and isinstance(item.get("readback"), dict)), None)
    if root is None:
        raise RuntimeError(f"complete root readback is missing: {path}")
    value = root["readback"]
    if value.get("source", {}).get("saved") is not True or value.get("source", {}).get("modified") is not False:
        raise RuntimeError(f"native read is not a saved, unmodified snapshot: {path}")
    return value


def _read_summary(operation_id: str, readback_path: Path) -> dict[str, Any]:
    payload = _load_json(readback_path)
    value = _root_readback(readback_path)
    source = value.get("source", {})
    readback = payload.get("readback", payload) if isinstance(payload, dict) else {}
    return {
        "operation_id": operation_id,
        "ok": True,
        "saved": source.get("saved"),
        "modified": source.get("modified"),
        "completeness": value.get("completeness", {}),
        "records": len(readback.get("records", [])) if isinstance(readback, dict) else 0,
        "instances": len(value.get("instances", [])),
        "pins": len(value.get("pins", [])),
        "nets": len(value.get("nets", [])),
        "database_sha256": value.get("database_sha256"),
        "design_counter": source.get("design_counter"),
    }


def _find_run_file(run_dir: Path, suffix: str) -> Path:
    matches = sorted(run_dir.rglob(suffix))
    if not matches:
        raise RuntimeError(f"selected run has no {suffix}: {run_dir}")
    return matches[0]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.project_root).resolve()
    if not root.is_dir() or not (root / "aetherpilot").is_dir():
        raise RuntimeError(f"--project-root is not an AetherPilot project: {root}")
    output_arg = Path(args.output_dir).expanduser()
    output = (root / output_arg).resolve() if not output_arg.is_absolute() else output_arg.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory already contains delivery artifacts: {output}")
    output.mkdir(parents=True, exist_ok=True)
    intent_arg = Path(args.intent).expanduser()
    intent = (root / intent_arg).resolve() if not intent_arg.is_absolute() else intent_arg.resolve()
    if not intent.is_file():
        raise RuntimeError(f"intent file does not exist: {intent}")
    config_path = Path(args.config).expanduser() if args.config else None
    if config_path is not None and not config_path.is_absolute():
        config_path = (root / config_path).resolve()
    if config_path is None:
        configured = os.environ.get("AETHERPILOT_CONFIG")
        config_path = Path(configured).expanduser() if configured else None
    if config_path is None:
        config_path = next((candidate for candidate in (root / ".aetherpilot" / "config.json", root / "aetherpilot.local.json") if candidate.is_file()), None)
    if config_path is None or not config_path.is_file():
        raise RuntimeError("No runtime config supplied; pass --config or set AETHERPILOT_CONFIG")
    h = Harness(config=config_path)
    started = _now()
    preflight = h.preflight()
    if not preflight.get("ok"):
        raise RuntimeError(f"preflight failed: {preflight.get('errors')}")
    targets = {
        "dut": {"library": args.library, "cell": args.dut_cell, "view": "schematic"},
        "testbench": {"library": args.library, "cell": args.testbench_cell, "view": "schematic"},
    }
    reads: dict[str, dict[str, Any]] = {}
    read_paths: dict[str, Path] = {}
    for label, target in targets.items():
        result = h.read(target, recursive=True, max_depth=args.max_depth, include_geometry=True, include_properties=True)
        if not result.get("ok"):
            raise RuntimeError(f"{label} read failed: {result.get('errors')}")
        operation_id = result["operation_id"]
        path = Path(h.config.artifact_root) / operation_id / "readback.json"
        reads[label] = _read_summary(operation_id, path)
        read_paths[label] = path
    selected = h.inspect(args.run_operation)
    if not selected.get("ok") or selected.get("stage") != "complete":
        raise RuntimeError(f"selected historical run is not complete: {selected.get('errors')}")
    run_dir = Path(h.config.artifact_root) / args.run_operation
    run_result = run_dir / "result.json"
    netlist = _find_run_file(run_dir, "*.netlist")
    review_result = h.review_files(read_paths["dut"], op_path=run_result, intent_path=intent, netlist_path=netlist)
    if not review_result.get("ok"):
        raise RuntimeError(f"review failed: {review_result.get('errors')}")
    review_operation = review_result["operation_id"]
    review = review_result.get("readback", {})
    review_time = _now()
    trial_context = {
        "input_mode": "current_native",
        "database_queried_this_run": True,
        "review_timestamp": review_time,
        "snapshot_timestamp": {label: _read_stage_time(Path(h.config.artifact_root) / reads[label]["operation_id"]) for label in reads},
        "selected_run": args.run_operation,
        "dut_read_operation": reads["dut"]["operation_id"],
        "testbench_read_operation": reads["testbench"]["operation_id"],
        "testbench_limitations": [
            "本轮读取了 testbench 的保存结构、器件、端子—网络和激励/负载对象；没有把全部当前 MDE setting 内容宣称为已核对。",
            "历史 run 的来源配对由其 manifest、netlist 和 OP 文件摘要验证；历史仿真不等于本轮重新仿真。",
        ],
        "pairing_scope": [
            "DUT 保存回读与选定 OP/netlist 的 run manifest、netlist SHA 和 raw OP SHA",
            "DUT/TB 保存状态、结构、器件、端子—网络及 testbench 负载对象的只读查询",
        ],
    }
    review["trial_context"] = trial_context
    sanitized_review = redact(review)
    # ``redact`` treats every slash as a possible path delimiter.  The
    # caller-supplied trial context is already path-free prose, so preserve it
    # verbatim instead of turning ordinary wording such as ``激励/负载`` into a
    # private-path placeholder.
    sanitized_review["trial_context"] = trial_context
    for original, sanitized in zip(review.get("findings", []), sanitized_review.get("findings", [])):
        if isinstance(original, dict) and isinstance(sanitized, dict) and "applies_when" in original:
            value = original["applies_when"]
            if value == "用户本次明确声明该角色<private-path>":
                value = "用户本次明确声明该角色/器件的工作区目标时"
            sanitized["applies_when"] = value
    (output / "review.json").write_text(json.dumps(sanitized_review, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "review.md").write_text(render_review_markdown(sanitized_review), encoding="utf-8")
    execution = {
        "schema_version": 1,
        "workflow": "AetherPilot Reviewer trial handoff",
        "started_at": started,
        "finished_at": _now(),
        "input_mode": "current_native",
        "project_root": "<project-root>",
        "code": {"package": "aetherpilot", "version": "0.1.0", "git_identity": "GIT_IDENTITY_PENDING", "python_executable": "<project-root>/.venv/bin/python", "native_import": "<project-root>/aetherpilot/__init__.py", "package_source_sha256": _package_source_sha256(root), "runner_path": _relative(Path(__file__), root), "runner_sha256": _sha256(Path(__file__).resolve()), "intent_sha256": _sha256(intent)},
        "request": {"dut": f"{args.library}/{args.dut_cell}/schematic", "testbench": f"{args.library}/{args.testbench_cell}/schematic", "selected_run": args.run_operation, "intent": _relative(intent, root)},
        "preflight": {"operation_id": preflight.get("operation_id"), "ok": preflight.get("ok"), "native_python": preflight.get("readback", {}).get("native_python"), "license_initialized": preflight.get("readback", {}).get("license_initialized"), "simulator_configured": preflight.get("readback", {}).get("simulator_configured")},
        "fresh_reads": reads,
        "selected_run": {"inspect_operation_id": args.run_operation, "ok": selected.get("ok"), "operation": selected.get("operation"), "stage": selected.get("stage"), "errors": len(selected.get("errors", [])), "warnings": len(selected.get("warnings", [])), "source_files": {"run_result": "<private-run-result>", "netlist": "<private-run-netlist>"}},
        "review": {"operation_id": review_operation, "ok": review_result.get("ok"), "coverage_complete": review.get("coverage", {}).get("complete"), "pairing_status": review.get("pairing", {}).get("status"), "finding_ids": [item.get("rule_id") for item in review.get("findings", [])], "writes_database": False, "starts_simulation": False, "modifies_configuration": False},
        "safety": {"eda_writes": False, "simulation_started": False, "license_or_pdk_modified": False, "historical_fallback": False},
        "public_call": {"preflight": "Harness.preflight()", "read": "Harness.read(target, recursive=True, max_depth=10, include_geometry=True, include_properties=True)", "inspect": "Harness.inspect(selected_run_operation)", "review": "Harness.review_files(dut_readback, op_path=selected_run_result, intent_path=intent, netlist_path=selected_netlist)"},
    }
    (output / "execution.json").write_text(json.dumps(redact(execution), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"preflight": preflight, "reads": reads, "selected": selected, "review_result": review_result, "review": review, "execution": execution, "output": output}


def _read_stage_time(operation_dir: Path) -> str | None:
    try:
        value = _load_json(operation_dir / "stage.json")
        return value.get("time")
    except (OSError, ValueError, TypeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--config", help="Local backend configuration JSON; do not commit this file")
    parser.add_argument("--library", required=True)
    parser.add_argument("--dut-cell", required=True)
    parser.add_argument("--testbench-cell", required=True)
    parser.add_argument("--run-operation", required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-depth", type=int, default=10)
    args = parser.parse_args()
    try:
        result = run(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"reproduce_review: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    review = result["review"]
    print(json.dumps({"ok": True, "input_mode": "current_native", "dut_read_operation": result["reads"]["dut"]["operation_id"], "testbench_read_operation": result["reads"]["testbench"]["operation_id"], "selected_run": args.run_operation, "review_operation": result["review_result"]["operation_id"], "coverage_complete": review.get("coverage", {}).get("complete"), "pairing_status": review.get("pairing", {}).get("status"), "finding_ids": [item.get("rule_id") for item in review.get("findings", [])]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
