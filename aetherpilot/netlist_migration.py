"""Explicit front-end schematic netlist migration between PDKs.

The migrator is intentionally separate from normal netlist import.  It never
infers a PDK, rewrites layout/parasitics, or claims simulation equivalence.  A
manifest names both PDK roots and every model/terminal/parameter mapping; the
caller must explicitly approve the reviewed manifest before a plan is emitted.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Mapping

from .contract import identifier
from .errors import ValidationError
from .netlist import import_schematic_netlist
from .pdk import DEFAULT_PDK_ROOT, parse_device_template


_MODEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PREFIXES = set("MQDRCX")


def load_manifest(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 ** 2:
        raise ValidationError("Migration manifest is missing or unsafe", stage="netlist", code="migration_manifest_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError("Migration manifest is not valid JSON", stage="netlist", code="migration_manifest_invalid") from exc
    return validate_manifest(value)


def validate_manifest(source: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(source, Mapping) or set(source) - {"schema_version", "source_pdk", "target_pdk", "mappings", "policy", "description"}:
        raise ValidationError("Migration manifest fields are invalid", stage="netlist", code="migration_manifest_invalid")
    if source.get("schema_version") != 1:
        raise ValidationError("Unsupported migration manifest schema", stage="netlist", code="migration_manifest_invalid")
    result = json.loads(json.dumps(dict(source)))
    for side in ("source_pdk", "target_pdk"):
        value = result.get(side)
        if not isinstance(value, dict) or set(value) != {"name", "root"} or not isinstance(value["name"], str) or not isinstance(value["root"], str):
            raise ValidationError(f"{side} requires name and root", stage="netlist", code="migration_manifest_invalid")
        if not value["root"].startswith("/"):
            raise ValidationError(f"{side}.root must be an absolute path", stage="netlist", code="migration_manifest_invalid")
        identifier(value["name"], side + ".name")
    if result["target_pdk"]["name"] != "018um_PDK":
        raise ValidationError("Migration target must be the certified 018um_PDK", stage="netlist", code="unsupported_target_pdk")
    mappings = result.get("mappings")
    if not isinstance(mappings, list) or not mappings or len(mappings) > 512:
        raise ValidationError("Migration mappings must be a nonempty bounded list", stage="netlist", code="migration_manifest_invalid")
    seen = set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) - {"source_model", "source_prefix", "source_terminal_order", "target_cell", "terminal_map", "parameter_map", "allow_unmapped_parameters"}:
            raise ValidationError("Migration mapping has unknown fields", stage="netlist", code="migration_manifest_invalid")
        for key in ("source_model", "target_cell"):
            if not isinstance(mapping.get(key), str) or not _MODEL.fullmatch(mapping[key]):
                raise ValidationError("Migration model names must be simple identifiers", stage="netlist", code="migration_manifest_invalid")
        source_model = mapping["source_model"].casefold()
        if source_model in seen:
            raise ValidationError("Migration source model is repeated", stage="netlist", code="migration_manifest_invalid")
        seen.add(source_model)
        prefix = mapping.get("source_prefix", "")
        if prefix and (not isinstance(prefix, str) or len(prefix) != 1 or prefix.upper() not in _PREFIXES):
            raise ValidationError("Migration source prefix is invalid", stage="netlist", code="migration_manifest_invalid")
        order = mapping.get("source_terminal_order")
        terminal_map = mapping.get("terminal_map")
        if not isinstance(order, list) or not order or any(not isinstance(item, str) or not _MODEL.fullmatch(item) for item in order):
            raise ValidationError("Migration source terminal order is required", stage="netlist", code="migration_manifest_invalid")
        if not isinstance(terminal_map, dict) or set(terminal_map) != set(order) or any(not isinstance(value, str) or not _MODEL.fullmatch(value) for value in terminal_map.values()) or len(set(terminal_map.values())) != len(terminal_map):
            raise ValidationError("Migration terminal map must be a bijection", stage="netlist", code="migration_manifest_invalid")
        parameter_map = mapping.get("parameter_map", {})
        if not isinstance(parameter_map, dict) or any(not isinstance(key, str) or not _MODEL.fullmatch(key) or not isinstance(value, str) or not _MODEL.fullmatch(value) for key, value in parameter_map.items()):
            raise ValidationError("Migration parameter map is invalid", stage="netlist", code="migration_manifest_invalid")
        mapping.setdefault("allow_unmapped_parameters", False)
        if type(mapping["allow_unmapped_parameters"]) is not bool:
            raise ValidationError("allow_unmapped_parameters must be boolean", stage="netlist", code="migration_manifest_invalid")
    policy = result.setdefault("policy", {})
    if not isinstance(policy, dict) or set(policy) - {"require_approval", "allow_draft"}:
        raise ValidationError("Migration policy is invalid", stage="netlist", code="migration_manifest_invalid")
    policy.setdefault("require_approval", True)
    policy.setdefault("allow_draft", True)
    if type(policy["require_approval"]) is not bool or type(policy["allow_draft"]) is not bool:
        raise ValidationError("Migration policy values must be boolean", stage="netlist", code="migration_manifest_invalid")
    return result


def review_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_manifest(manifest)
    source = Path(value["source_pdk"]["root"]).expanduser().resolve()
    target = Path(value["target_pdk"]["root"]).expanduser().resolve()
    checks = []
    for label, path in (("source_pdk", source), ("target_pdk", target)):
        checks.append({"name": label + "_root", "path": str(path), "exists": path.is_dir(), "symlink": path.is_symlink()})
    mapping_checks = []
    for item in value["mappings"]:
        definition = target / item["target_cell"] / "device.def"
        native = None
        if definition.is_file() and not definition.is_symlink():
            native = parse_device_template(definition.read_text(encoding="utf-8", errors="replace"))
        mapping_checks.append({"source_model": item["source_model"], "target_cell": item["target_cell"],
                               "target_definition": str(definition), "target_definition_exists": native is not None,
                               "target_terminal_order": native["terminals"] if native else None,
                               "terminal_map": item["terminal_map"], "parameter_map": item.get("parameter_map", {})})
    return {"schema_version": 1, "kind": "front_end_netlist_migration_review", "source_pdk": {**value["source_pdk"], "resolved_root": str(source)},
            "target_pdk": {**value["target_pdk"], "resolved_root": str(target)}, "root_checks": checks,
            "mappings": mapping_checks, "requires_explicit_approval": value["policy"]["require_approval"],
            "scope": ["front_end_schematic_only", "model_terminal_parameter_rewrite", "draft_geometry"],
            "excluded": ["layout", "parasitics", "pex", "simulation_equivalence", "model_calibration", "silent_pdk_inference"]}


def _read_source(source: str | Path) -> tuple[str, str]:
    path = Path(source).expanduser().resolve() if isinstance(source, Path) or isinstance(source, str) and "\n" not in source and len(source) < 4096 else None
    if path is not None and path.is_file():
        if path.is_symlink() or path.stat().st_size > 64 * 1024 ** 2:
            raise ValidationError("Migration netlist path is unsafe or too large", stage="netlist", code="resource_limit")
        text = path.read_text(encoding="utf-8")
        return text, hashlib.sha256(text.encode()).hexdigest()
    if not isinstance(source, str) or len(source.encode()) > 64 * 1024 ** 2:
        raise ValidationError("Migration netlist source is invalid or too large", stage="netlist", code="resource_limit")
    return source, hashlib.sha256(source.encode()).hexdigest()


def _rewrite(text: str, manifest: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    mappings = {item["source_model"].casefold(): item for item in manifest["mappings"]}
    target_root = Path(manifest["target_pdk"]["root"]).expanduser().resolve()
    rewritten, devices = [], []
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("*") or stripped.casefold().startswith((".subckt", ".ends", ".end", ".title")):
            rewritten.append(raw)
            continue
        if stripped.startswith("."):
            raise ValidationError("Migration only supports flat front-end directives", stage="netlist", code="unsupported_netlist_directive", details={"line": line_number})
        try:
            tokens = shlex.split(stripped, comments=False, posix=True)
        except ValueError as exc:
            raise ValidationError("Migration device line is malformed", stage="netlist", code="netlist_parse", details={"line": line_number}) from exc
        if not tokens or tokens[0][0].upper() not in _PREFIXES:
            raise ValidationError("Migration encountered an unsupported line", stage="netlist", code="unsupported_netlist_device", details={"line": line_number})
        candidates = [(model, item) for model, item in mappings.items() if model in {token.casefold() for token in tokens}]
        if len(candidates) != 1:
            raise ValidationError("Migration device model is unmapped or ambiguous", stage="netlist", code="migration_unmapped_model", details={"line": line_number, "models": sorted(mappings)})
        source_model, mapping = candidates[0]
        if mapping.get("source_prefix") and tokens[0][0].upper() != mapping["source_prefix"].upper():
            raise ValidationError("Migration instance prefix differs from the manifest", stage="netlist", code="migration_prefix_mismatch", details={"line": line_number, "expected": mapping["source_prefix"]})
        source_order = mapping["source_terminal_order"]
        model_index = next(index for index, token in enumerate(tokens) if token.casefold() == source_model)
        if model_index != len(source_order) + 1:
            raise ValidationError("Migration source terminal count differs from the manifest", stage="netlist", code="migration_terminal_mismatch", details={"line": line_number, "expected": len(source_order), "actual": model_index - 1})
        target_def = target_root / mapping["target_cell"] / "device.def"
        if not target_def.is_file() or target_def.is_symlink():
            raise ValidationError("Migration target PDK device definition is missing", stage="netlist", code="target_pdk_device_missing", details={"cell": mapping["target_cell"]})
        parsed = parse_device_template(target_def.read_text(encoding="utf-8", errors="replace"))
        inverse = {value: key for key, value in mapping["terminal_map"].items()}
        try:
            target_terminals = [tokens[1 + source_order.index(inverse[terminal])] for terminal in parsed["terminals"]]
        except (ValueError, KeyError):
            raise ValidationError("Migration terminal map does not cover target PDK terminals", stage="netlist", code="migration_terminal_mismatch", details={"line": line_number})
        params = []
        for token in tokens[model_index + 1:]:
            if token.count("=") != 1:
                raise ValidationError("Migration parameter assignment is malformed", stage="netlist", code="netlist_parse", details={"line": line_number})
            key, value = token.split("=", 1)
            target_key = mapping.get("parameter_map", {}).get(key, key)
            if key not in mapping.get("parameter_map", {}) and not mapping.get("allow_unmapped_parameters", False):
                raise ValidationError("Migration parameter is not explicitly mapped", stage="netlist", code="migration_unmapped_parameter", details={"line": line_number, "parameter": key, "model": source_model})
            params.append(f"{target_key}={value}")
        prefix = parsed["prefix"]
        rewritten.append(" ".join([prefix + tokens[0][1:], *target_terminals, mapping["target_cell"], *params]))
        devices.append({"line": line_number, "instance": tokens[0][1:], "source_model": source_model,
                        "target_cell": mapping["target_cell"], "source_prefix": tokens[0][0].upper(), "target_prefix": prefix,
                        "terminal_map": mapping["terminal_map"], "parameter_map": mapping.get("parameter_map", {})})
    return "\n".join(rewritten) + ("\n" if text.endswith("\n") else ""), devices


def migrate_netlist(source: str | Path, *, target: Mapping[str, Any], manifest: Mapping[str, Any], approved: bool = False) -> dict[str, Any]:
    value = validate_manifest(manifest)
    if value["policy"]["require_approval"] and approved is not True:
        raise ValidationError("Netlist migration requires explicit reviewed-manifest approval", stage="netlist", code="approval_required")
    if not isinstance(target, Mapping) or not isinstance(target.get("library"), str) or not isinstance(target.get("cell"), str):
        raise ValidationError("Migration target requires library and cell", stage="netlist", code="invalid_migration_target")
    identifier(target["library"], "target.library")
    identifier(target["cell"], "target.cell")
    text, source_hash = _read_source(source)
    rewritten, devices = _rewrite(text, value)
    target_root = Path(value["target_pdk"]["root"]).expanduser().resolve()
    imported = import_schematic_netlist(rewritten, target={"library": target["library"], "cell": target["cell"], "technology": "018um_PDK"},
                                         pdk_root=target_root, require_metadata=False)
    manifest_hash = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    review = review_manifest(value)
    report = {"schema_version": 1, "kind": "front_end_netlist_migration", "source_sha256": source_hash,
              "rewritten_sha256": hashlib.sha256(rewritten.encode()).hexdigest(), "manifest_sha256": manifest_hash,
              "approved": bool(approved), "source_pdk": value["source_pdk"], "target_pdk": value["target_pdk"],
              "devices": devices, "review": review, "scope": review["scope"], "excluded": review["excluded"],
              "source_text_preserved": True, "target_plan_is_draft": True, "pdk_path_review_required": True}
    imported["migration"] = report
    imported["rewritten_netlist"] = rewritten
    return imported


__all__ = ["load_manifest", "validate_manifest", "review_manifest", "migrate_netlist"]
