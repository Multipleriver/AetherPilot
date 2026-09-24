"""Exclusive local result exports and deterministic setup replay scripts."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .artifacts import file_digest
from .errors import ValidationError
from .results import safe_result_path


def write_export(destination, payload):
    path = safe_result_path(destination, must_exist=False)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise ValidationError("Export destination already exists", stage="results", code="target_exists") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str(path), "sha256": file_digest(path), "bytes": len(payload)}


def export_output_table(run, table_view, output):
    table_view = table_view.lower()
    if table_view not in {"detail", "summary", "transpose", "yield"}:
        raise ValidationError("Unknown native table view", stage="results", code="invalid_maestro_request")
    candidates = [table for table in run["tables"] if Path(table["path"]).name.lower().endswith("_" + table_view + ".csv")]
    if len(candidates) != 1:
        raise ValidationError("Native table view is absent or ambiguous", stage="results", code="ambiguous_result")
    table = candidates[0]
    source = safe_result_path(table["path"], max_bytes=16 * 1024 ** 2)
    if file_digest(source) != table["sha256"]:
        raise ValidationError("Native table changed before export", stage="results", code="result_provenance")
    exported = write_export(output, source.read_bytes())
    if exported["sha256"] != table["sha256"]:
        raise ValidationError("Exported table fingerprint differs", stage="results", code="result_provenance")
    return {**exported, "table_view": table_view, "run_id": run["run_id"], "history": run["history"]}


def portable_setup(state):
    from .maestro import _normalize_state

    setup = _normalize_state(state)
    setup["results"] = []
    setup["metadata"] = {"history": [], "state_view": "state"}
    metadata = state.get("metadata", {})
    for key in ("native_notes", "native_hspice_legacy", "native_hspice_model_base", "native_hspice_opaque_sections"):
        if key in metadata:
            setup["metadata"][key] = copy.deepcopy(metadata[key])
    return copy.deepcopy(setup)


def replay_script(state):
    setup = json.dumps(portable_setup(state), sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return '''"""Recreate an exported AetherPilot setup in a new native state view."""

import argparse
import json

from aetherpilot.harness import Harness


SETUP = json.loads(%r)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--view", default="state")
    arguments = parser.parse_args()
    target = {"library": arguments.library, "cell": arguments.cell, "view": arguments.view}
    result = Harness(arguments.config).maestro_action(target, "restore_setup", setup=SETUP)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
''' % setup
