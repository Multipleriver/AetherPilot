"""Read-only structural and operating-point review helpers.

The reviewer deliberately consumes facts that have already been read from a
saved schematic or from an immutable simulation artifact.  It never opens an
editable Aether view, writes a database, changes a configuration, or starts a
simulation.  The small amount of topology recognition here is intentionally
bounded to the three structures supported by the Phase 2 MVP.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
_NUMBER = re.compile(r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?(?:meg|[TGMKkmunpfaz])?$", re.I)
_SUFFIX = re.compile(r"(meg|[TGMKkmunpfaz])$", re.I)
_MOS_MASTER = re.compile(r"^(?:n|p)(?:mos|18|fet|device)?", re.I)
_OP_REQUIRED = ("region", "id", "vgs", "vds", "vbs", "vth", "vdsat", "gm", "gds")
_FIELD_UNITS = {
    "id": "A", "ibs": "A", "ibd": "A", "i1": "A", "i2": "A", "i3": "A", "i4": "A", "ids": "A",
    "vgs": "V", "vds": "V", "vbs": "V", "vth": "V", "vdsat": "V", "vod": "V",
    "gm": "S", "gds": "S", "gmb": "S", "cdtot": "F", "cgtot": "F", "cstot": "F", "cds": "F",
    "cbtot": "F", "cgs": "F", "cgd": "F", "cgb": "F", "cgg": "F", "pwr": "W", "res": "ohm",
}
_ENGINEERING = {"T": 1e12, "G": 1e9, "M": 1e-3, "K": 1e3, "k": 1e3, "meg": 1e6,
                "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18, "z": 1e-21}


class ReviewInputError(ValueError):
    """Raised when a review input is malformed or unsafe to read."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_file(path: str | Path, *, max_bytes: int = 64 * 1024 * 1024) -> Path:
    candidate = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ReviewInputError("review input path contains a symlink")
    if not candidate.is_file() or candidate.stat().st_size > max_bytes:
        raise ReviewInputError("review input file is missing or exceeds its size budget")
    return candidate.resolve()


def _json_load(path: str | Path) -> Any:
    candidate = _safe_file(path)
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ReviewInputError(f"cannot parse review JSON: {candidate.name}") from exc


def _unwrap_readback(value: Any) -> dict[str, Any]:
    """Accept a readback, snapshot, or Harness result wrapper."""
    if not isinstance(value, Mapping):
        raise ReviewInputError("design readback must be an object")
    current: Any = value
    wrapper_metadata = {key: copy.deepcopy(value[key]) for key in ("operation_id", "database_sha256", "content_sha256", "target") if key in value}
    seen: set[int] = set()
    while isinstance(current, Mapping) and isinstance(current.get("readback"), Mapping) and id(current) not in seen:
        seen.add(id(current))
        current = current["readback"]
    if isinstance(current, Mapping) and isinstance(current.get("result"), Mapping):
        current = current["result"]
    if not isinstance(current, Mapping):
        raise ReviewInputError("design readback is not an object")
    # A hierarchy operation stores the root facts under records[status=complete].
    if not current.get("instances") and isinstance(current.get("records"), list):
        root = next((item for item in current["records"] if isinstance(item, Mapping) and item.get("path") == [] and isinstance(item.get("readback"), Mapping)), None)
        if root is not None:
            merged = copy.deepcopy(dict(root["readback"]))
            for key in ("target", "root", "records", "completeness", "complete", "dependencies", "schema_version"):
                if key in current and key not in merged:
                    merged[key] = copy.deepcopy(current[key])
            current = merged
    result = copy.deepcopy(dict(current))
    for key, item in wrapper_metadata.items():
        result.setdefault(key, item)
    return result


def _target(readback: Mapping[str, Any]) -> dict[str, Any]:
    value = readback.get("target") or readback.get("root") or {}
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _source_ref(path: str, field: str) -> dict[str, str]:
    return {"path": path, "field": field}


def _engineering(value: Any) -> tuple[float | None, str, str]:
    """Return SI value, original suffix, and parse status without guessing."""
    raw = str(value).strip() if value is not None else ""
    if not raw or not _NUMBER.fullmatch(raw):
        return None, "", "unparsed"
    match = _SUFFIX.search(raw)
    suffix = match.group(1) if match else ""
    number = raw[:match.start()] if match else raw
    try:
        result = float(number) * (_ENGINEERING.get(suffix, _ENGINEERING.get(suffix.lower(), 1.0)))
    except (TypeError, ValueError, OverflowError):
        return None, suffix, "unparsed"
    if not math.isfinite(result):
        return None, suffix, "unparsed"
    return result, suffix, "parsed"


def _value_record(value: Any, field: str, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = str(value) if value is not None else ""
    number, suffix, status = _engineering(raw)
    return {"raw": raw, "value": number, "unit": _FIELD_UNITS.get(field.casefold(), ""),
            "raw_suffix": suffix, "parse_status": status, "source": copy.deepcopy(dict(source or {}))}


def _parameter_map(instance: Mapping[str, Any]) -> dict[str, Any]:
    values = instance.get("parameters")
    result: dict[str, Any] = copy.deepcopy(dict(values)) if isinstance(values, Mapping) else {}
    if isinstance(instance.get("properties"), list):
        for item in instance["properties"]:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"] not in result:
                result[item["name"]] = item.get("value", "")
    return result


def _is_mos(instance: Mapping[str, Any]) -> bool:
    master = instance.get("master") if isinstance(instance.get("master"), Mapping) else {}
    cell = str(master.get("cell", ""))
    connections = instance.get("connections")
    if not isinstance(connections, Mapping):
        return False
    terminals = {str(key).casefold() for key in connections}
    return {"d", "g", "s"}.issubset(terminals) and (bool(_MOS_MASTER.match(cell)) or "b" in terminals or "body" in terminals)


def _mos_polarity(instance: Mapping[str, Any]) -> str:
    master = instance.get("master") if isinstance(instance.get("master"), Mapping) else {}
    cell = str(master.get("cell", "")).casefold()
    if cell.startswith("p"):
        return "p"
    if cell.startswith("n"):
        return "n"
    return "unknown"


def _normalize_region(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "saturation" if text.startswith("saturati") else text


def _connection(instance: Mapping[str, Any], terminal: str) -> str | None:
    connections = instance.get("connections")
    if not isinstance(connections, Mapping):
        return None
    wanted = terminal.casefold()
    for key, value in connections.items():
        if str(key).casefold() in {wanted, "body" if wanted == "b" else wanted}:
            return str(value)
    return None


def _collect_instances(readback: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect instances while preserving hierarchy paths and source fields."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(items: Any, prefix: Iterable[str], source_path: str) -> None:
        if not isinstance(items, list):
            return
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                continue
            name = item["name"]
            path_parts = [str(part) for part in prefix] + [name]
            path = "/".join(path_parts)
            key = (path.casefold(), str(item.get("master", {})))
            if key in seen:
                continue
            seen.add(key)
            normalized = copy.deepcopy(dict(item))
            normalized["path"] = path
            normalized["source"] = {"path": f"{source_path}.instances[{index}]", "field": "instance"}
            normalized["parameters"] = _parameter_map(item)
            normalized["is_mos"] = _is_mos(item)
            result.append(normalized)

    add(readback.get("instances"), (), "readback")
    records = readback.get("records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping) or not isinstance(record.get("readback"), Mapping):
                continue
            add(record["readback"].get("instances"), record.get("path") or (), f"records[{records.index(record)}].readback")
    return result


def _design_payload(readback: Mapping[str, Any]) -> dict[str, Any]:
    instances = _collect_instances(readback)
    pins = copy.deepcopy(readback.get("pins", [])) if isinstance(readback.get("pins"), list) else []
    nets = copy.deepcopy(readback.get("nets", [])) if isinstance(readback.get("nets"), list) else []
    complete = readback.get("completeness", {}).get("status") == "complete" if isinstance(readback.get("completeness"), Mapping) else bool(readback.get("complete"))
    return {"target": _target(readback), "instances": instances, "pins": pins, "nets": nets,
            "complete": bool(complete), "content_sha256": readback.get("database_sha256") or readback.get("content_sha256") or _digest(readback),
            "database_sha256": readback.get("database_sha256"), "operation_id": readback.get("operation_id"),
            "source": copy.deepcopy(readback.get("source", {})) if isinstance(readback.get("source"), Mapping) else {}}


def _parse_op_text(text: str, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    lines = text.splitlines()
    devices: list[dict[str, Any]] = []
    node_voltages: dict[str, dict[str, Any]] = {}
    section = ""
    section_start = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        for match in re.finditer(r"(?:^|\s)(?:\d+)\s*:([^\s=]+)\s*=\s*(\S+)", line):
            node_voltages[match.group(1)] = _value_record(match.group(2), "v", {"path": "op0", "line": index + 1})
        match = re.match(r"^####\s+([^:]+):", line)
        if match:
            section = match.group(1).strip()
            section_start = index
            index += 1
            continue
        if section.casefold() == "mosfetlevel49" and re.match(r"^\s*hierarchy\s+", line):
            headers = line.split()
            device_line = index + 1
            while device_line < len(lines) and not lines[device_line].strip():
                device_line += 1
            if device_line >= len(lines) or not re.match(r"^\s*device\s+", lines[device_line]):
                index += 1
                continue
            hierarchies = line.split()[1:]
            devices_raw = lines[device_line].split()[1:]
            count = len(devices_raw)
            rows: dict[str, list[str]] = {}
            cursor = device_line + 1
            while cursor < len(lines):
                current = lines[cursor]
                if not current.strip() or current.startswith("####") or current.startswith("Maximum and minimum"):
                    break
                tokens = current.split()
                if len(tokens) >= count + 1:
                    rows[tokens[0]] = tokens[1:1 + count]
                cursor += 1
            for column_index, device in enumerate(devices_raw):
                record: dict[str, Any] = {"device": device, "hierarchy": hierarchies[column_index] if column_index < len(hierarchies) else "",
                                          "source": {"path": "op0", "line": section_start + 1, "section": section}}
                for field, values in rows.items():
                    if column_index < len(values):
                        raw = values[column_index]
                        if field == "region":
                            record[field] = raw
                            record["region_normalized"] = "Saturation" if raw.casefold().startswith("saturati") else raw
                        else:
                            record[field] = raw if field in {"model", "hierarchy", "device"} else _value_record(raw, field, {"path": "op0", "line": section_start + 1, "section": section, "column": column_index})
                devices.append(record)
            index = cursor
            continue
        index += 1
    return {"schema_version": 1, "format": "aether-op0", "devices": devices, "node_voltages": node_voltages,
            "source": copy.deepcopy(dict(source or {})), "raw_sections": ["mosfetLevel49"] if devices else []}


def parse_operating_point(value: Any, *, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Parse a native ``.op0`` file or normalize a structured OP fixture."""
    if isinstance(value, (str, Path)):
        path = _safe_file(value)
        return _parse_op_text(path.read_text(encoding="utf-8", errors="replace"), {**(source or {}), "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if isinstance(value, Mapping):
        current = value.get("readback") if isinstance(value.get("readback"), Mapping) else value
        if isinstance(current, Mapping) and isinstance(current.get("operating_point"), Mapping):
            current = current["operating_point"]
        raw_devices = current.get("devices") if isinstance(current, Mapping) else None
        if raw_devices is None and isinstance(current, Mapping):
            raw_devices = current.get("mosfetLevel49")
        records: list[dict[str, Any]] = []
        if isinstance(raw_devices, Mapping):
            for name, fields in raw_devices.items():
                if not isinstance(fields, Mapping):
                    continue
                record = {"device": str(name), "hierarchy": fields.get("hierarchy", ""), "source": {"path": "structured.devices", "device": str(name)}}
                for key, item in fields.items():
                    if key in {"device", "hierarchy", "source"}:
                        continue
                    if key.casefold() == "region":
                        record[key] = str(item)
                        record["region_normalized"] = "Saturation" if str(item).casefold().startswith("saturati") else str(item)
                    elif isinstance(item, Mapping) and "raw" in item:
                        record[key] = copy.deepcopy(dict(item))
                    else:
                        record[key] = _value_record(item, key, {"path": "structured.devices", "device": str(name), "field": key})
                records.append(record)
        elif isinstance(raw_devices, list):
            for item in raw_devices:
                if not isinstance(item, Mapping):
                    continue
                record = copy.deepcopy(dict(item))
                record.setdefault("device", record.get("name", ""))
                record.setdefault("source", {"path": "structured.devices"})
                if "region" in record:
                    record.setdefault("region_normalized", "Saturation" if str(record["region"]).casefold().startswith("saturati") else str(record["region"]))
                for key, item_value in list(record.items()):
                    if key in {"device", "name", "hierarchy", "source", "region", "region_normalized"}:
                        continue
                    if not isinstance(item_value, Mapping) or "raw" not in item_value:
                        record[key] = _value_record(item_value, key, {"path": "structured.devices", "field": key})
                records.append(record)
        return {"schema_version": 1, "format": "structured", "devices": records,
                "node_voltages": copy.deepcopy(current.get("node_voltages", {})) if isinstance(current, Mapping) else {},
                "source": copy.deepcopy(dict((current or {}).get("source", source or {}))) if isinstance(current, Mapping) else copy.deepcopy(dict(source or {}))}
    raise ReviewInputError("operating-point input must be a path or object")


def _raw_device_name(value: Any) -> str:
    text = str(value or "").strip()
    text = text.split(":", 1)[-1]
    text = text.split(".")[-1]
    return text


def _map_op_devices(op: Mapping[str, Any], instances: list[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    by_path = {str(item.get("path", "")).casefold(): item for item in instances}
    for item in instances:
        by_name.setdefault(str(item.get("name", "")).casefold(), []).append(item)
    mapped: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    for record in op.get("devices", []) if isinstance(op.get("devices"), list) else []:
        raw_name = _raw_device_name(record.get("device") or record.get("name"))
        candidates = [raw_name.casefold()]
        if raw_name.casefold().startswith("m"):
            candidates.append(raw_name[1:].casefold())
        hierarchy = str(record.get("hierarchy", "")).strip().replace(".", "/").strip("/").casefold()
        selected_item = None
        if hierarchy:
            for candidate in candidates:
                path_candidate = f"{hierarchy}/{candidate}"
                if path_candidate in by_path:
                    selected_item = by_path[path_candidate]
                    break
        name_matches = [item for candidate in candidates for item in by_name.get(candidate, [])]
        if selected_item is None and len(name_matches) == 1:
            selected_item = name_matches[0]
        if selected_item is None:
            record = copy.deepcopy(record)
            record["mapping_reason"] = "ambiguous or absent explicit hierarchy/name"
            unmatched.append(copy.deepcopy(record))
            continue
        selected = str(selected_item.get("name", "")).casefold()
        mapped[selected] = copy.deepcopy(dict(record))
        mapped[selected]["object_path"] = selected_item.get("path", selected_item.get("name"))
        mapped[selected]["raw_device"] = record.get("device", raw_name)
    missing = [str(item.get("name")) for item in instances if item.get("is_mos") and str(item.get("name", "")).casefold() not in mapped]
    return mapped, unmatched, missing


def _netlist_signature(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        path = _safe_file(value)
        text = path.read_text(encoding="utf-8", errors="replace")
        source = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    elif isinstance(value, Mapping):
        if isinstance(value.get("netlist"), (str, Path)):
            return _netlist_signature(value["netlist"])
        text = str(value.get("text", ""))
        source = copy.deepcopy(dict(value.get("source", {})))
        if not text:
            return None
    else:
        return None
    devices: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("+") or stripped.startswith("."):
            continue
        tokens = stripped.split()
        if len(tokens) < 6 or not tokens[0].casefold().startswith("m"):
            continue
        params: dict[str, str] = {}
        for token in tokens[6:]:
            if "=" in token:
                key, raw = token.split("=", 1)
                params[key.casefold()] = raw
        devices.append({"name": tokens[0], "d": tokens[1], "g": tokens[2], "s": tokens[3], "b": tokens[4], "model": tokens[5], "parameters": params,
                        "source": {**source, "line": line_number}})
    return {"devices": devices, "source": source, "text_sha256": _digest(text)}


def _pair_sources(design: Mapping[str, Any], op: Mapping[str, Any] | None, source: Mapping[str, Any] | None,
                  netlist: Any = None) -> dict[str, Any]:
    if op is None:
        return {"status": "not_requested", "workpoint_usable": False, "evidence": []}
    source_map: dict[str, Any] = {}
    if isinstance(op.get("source"), Mapping):
        source_map.update(op["source"])
    if isinstance(source, Mapping):
        source_map.update(source)
    run_identity = source_map.get("run_identity")
    if isinstance(run_identity, Mapping):
        if run_identity.get("same_manifest") is False:
            return {"status": "rejected", "workpoint_usable": False, "reason": "run evidence identity differs", "evidence": ["run_identity.op", "run_identity.netlist"]}
        if run_identity.get("verified") is not True:
            return {"status": "rejected", "workpoint_usable": False, "reason": "run evidence manifest or file hash is not verified", "evidence": ["run_identity.manifest", "run_identity.op", "run_identity.netlist"]}
    elif source_map.get("op_path"):
        synthetic = source_map.get("kind") == "offline_synthetic_fixture" or (isinstance(netlist, Mapping) and isinstance(netlist.get("source"), Mapping) and netlist["source"].get("kind") == "offline_synthetic_fixture")
        if not synthetic:
            return {"status": "rejected", "workpoint_usable": False, "reason": "run evidence manifest is missing", "evidence": ["source.op_path", "source.run_manifest"]}
    op_target = source_map.get("target") or op.get("target")
    design_target = design.get("target", {})
    if isinstance(op_target, Mapping) and isinstance(design_target, Mapping):
        mismatch = {key: (design_target.get(key), op_target.get(key)) for key in ("library", "cell", "view") if design_target.get(key) and op_target.get(key) and design_target.get(key) != op_target.get(key)}
        if mismatch:
            return {"status": "rejected", "workpoint_usable": False, "reason": "target identity differs", "mismatch": mismatch, "evidence": ["source.target", "design.target"]}
    # Bind the OP to the run/netlist that supplied it before considering the
    # design identity.  This prevents a caller from pairing a before OP with
    # an after netlist (or vice versa) merely because both contain five MOS.
    op_netlist_hash = source_map.get("op_run_netlist_sha256") or source_map.get("run_netlist_sha256")
    selected_netlist_hash = source_map.get("netlist_sha256")
    if op_netlist_hash and selected_netlist_hash and op_netlist_hash != selected_netlist_hash:
        return {"status": "rejected", "workpoint_usable": False, "reason": "simulation provenance mismatch (OP run netlist differs from supplied netlist)",
                "expected": op_netlist_hash, "actual": selected_netlist_hash, "evidence": ["op.run.netlist_sha256", "source.netlist_sha256"]}
    expected_run = source_map.get("expected_run_id")
    op_run = source_map.get("op_run_id")
    if expected_run and op_run and expected_run != op_run:
        return {"status": "rejected", "workpoint_usable": False, "reason": "simulation provenance mismatch (run identity differs)",
                "expected": expected_run, "actual": op_run, "evidence": ["source.expected_run_id", "op.run_id"]}
    if source_map.get("op_raw_sha256_verified") is False:
        return {"status": "rejected", "workpoint_usable": False, "reason": "simulation provenance mismatch (raw OP hash differs from run manifest)",
                "evidence": ["source.op_sha256", "source.run_manifest.files"]}
    expected_operation = source_map.get("design_operation_id") or source_map.get("operation_id")
    explicit_provenance_match = False
    if expected_operation is not None:
        if design.get("operation_id") and expected_operation == design.get("operation_id"):
            for source_field, design_field in (("database_sha256", "database_sha256"), ("design_sha256", "content_sha256"), ("readback_sha256", "content_sha256")):
                supplied = source_map.get(source_field)
                actual = design.get(design_field)
                if supplied and actual and supplied != actual:
                    return {"status": "rejected", "workpoint_usable": False, "reason": f"design provenance mismatch ({source_field} differs)", "expected": supplied, "actual": actual, "evidence": [f"source.{source_field}", f"design.{design_field}"]}
            explicit_provenance_match = True
        if design.get("operation_id") and expected_operation != design.get("operation_id"):
            return {"status": "rejected", "workpoint_usable": False, "reason": "design provenance mismatch (operation identity differs)", "expected": expected_operation, "actual": design.get("operation_id"), "evidence": ["source.design_operation_id", "design.operation_id"]}
    expected_hash = source_map.get("design_sha256") or source_map.get("database_sha256") or source_map.get("readback_sha256")
    if expected_hash:
        if expected_hash != design.get("content_sha256"):
            return {"status": "rejected", "workpoint_usable": False, "reason": "design provenance mismatch (fingerprint differs)", "expected": expected_hash, "actual": design.get("content_sha256"), "evidence": ["source.design_sha256", "design.content_sha256"]}
    signature = _netlist_signature(netlist or source_map.get("netlist") or source_map.get("netlist_path"))
    if signature is None:
        if explicit_provenance_match:
            return {"status": "matched", "workpoint_usable": True, "method": "explicit_operation_and_fingerprint", "evidence": ["source.design_operation_id", "design.operation_id"]}
        return {"status": "unverified", "workpoint_usable": False, "reason": "no design-to-run identity anchor was supplied", "evidence": []}
    design_mos = [item for item in design.get("instances", []) if item.get("is_mos")]
    by_name = {str(item.get("name", "")).casefold(): item for item in design_mos}
    mismatches: list[dict[str, Any]] = []
    matched = 0
    for net_item in signature["devices"]:
        raw = _raw_device_name(net_item["name"])
        candidates = [raw.casefold(), raw[1:].casefold() if raw.casefold().startswith("m") else raw.casefold()]
        design_item = next((by_name.get(name) for name in candidates if by_name.get(name)), None)
        if design_item is None:
            mismatches.append({"kind": "netlist_device_not_in_design", "device": net_item["name"], "evidence": net_item["source"]})
            continue
        matched += 1
        for terminal in ("d", "g", "s", "b"):
            expected = _connection(design_item, terminal)
            actual = net_item[terminal]
            if expected is not None and expected.casefold() != actual.casefold() and not (expected.casefold() == "vss" and actual == "0"):
                mismatches.append({"kind": "connection_mismatch", "object_path": design_item.get("path"), "terminal": terminal, "design": expected, "netlist": actual, "evidence": net_item["source"]})
        raw_params = design_item.get("parameters", {})
        params = {str(name).casefold(): value for name, value in raw_params.items()} if isinstance(raw_params, Mapping) else {}
        aliases = {"l": ("length",), "w": ("single_width", "width"), "m": ("m", "simm")}
        for key, names in aliases.items():
            actual = net_item["parameters"].get(key)
            if actual is None:
                continue
            expected = next((params.get(str(name).casefold()) for name in names if str(name).casefold() in params and params.get(str(name).casefold()) not in (None, "")), None)
            if expected is not None:
                ev, _, estate = _engineering(expected)
                av, _, astate = _engineering(actual)
                if estate == "parsed" and astate == "parsed" and not math.isclose(ev, av, rel_tol=1e-9, abs_tol=1e-18):
                    mismatches.append({"kind": "parameter_mismatch", "object_path": design_item.get("path"), "field": names[0], "design": str(expected), "netlist": actual, "evidence": net_item["source"]})
    if not signature["devices"] or matched != len(design_mos) or mismatches:
        return {"status": "rejected", "workpoint_usable": False, "reason": "design and netlist facts differ", "mismatches": mismatches,
                "matched_devices": matched, "design_mos": len(design_mos), "netlist": signature["source"], "evidence": ["design.instances", "netlist"]}
    return {"status": "matched", "workpoint_usable": True, "method": "explicit_provenance_and_netlist" if explicit_provenance_match else "netlist_signature", "matched_devices": matched, "design_mos": len(design_mos),
            "netlist": signature["source"], "evidence": ["design.instances", "netlist"]}


def _finding(rule_id: str, object_path: Any, classification: str, evidence: Any, explanation: str,
             recommendation: str, applies_when: str = "") -> dict[str, Any]:
    return {"rule_id": rule_id, "object_path": object_path if isinstance(object_path, list) else [str(object_path)] if object_path else [],
            "classification": classification, "evidence": evidence if isinstance(evidence, list) else [evidence],
            "explanation": explanation, "recommendation": recommendation, "applies_when": applies_when}


def _topology(instances: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    mos = [item for item in instances if item.get("is_mos")]
    candidates: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    # Differential pair candidates: same polarity, shared source, distinct gates.
    pairs = []
    for left, right in itertools.combinations(mos, 2):
        if _mos_polarity(left) == "unknown" or _mos_polarity(left) != _mos_polarity(right):
            continue
        shared = _connection(left, "s")
        if shared and shared == _connection(right, "s") and _connection(left, "g") != _connection(right, "g"):
            pairs.append((left, right, shared))
    if pairs:
        for left, right, shared in pairs:
            candidates.append({"kind": "mos_input_differential_pair", "status": "confirmed" if len(pairs) == 1 else "candidate",
                               "objects": [left["path"], right["path"]], "polarity": _mos_polarity(left),
                               "evidence": [_source_ref(left["source"]["path"], "connections.S"), _source_ref(right["source"]["path"], "connections.S")],
                               "facts": {"shared_source_net": shared, "gate_nets": [_connection(left, "g"), _connection(right, "g")]}})
    # Current mirror candidates: same gate and source/body, with one diode-connected drain.
    mirrors = []
    for left, right in itertools.combinations(mos, 2):
        if _mos_polarity(left) == "unknown" or _mos_polarity(left) != _mos_polarity(right):
            continue
        if _connection(left, "g") == _connection(right, "g") and _connection(left, "s") == _connection(right, "s") and _connection(left, "b") == _connection(right, "b"):
            diode = [item for item in (left, right) if _connection(item, "d") == _connection(item, "g")]
            if diode:
                mirrors.append((left, right, diode[0]))
    for left, right, diode in mirrors:
        candidates.append({"kind": "current_mirror_load", "status": "confirmed" if len(mirrors) == 1 else "candidate",
                           "objects": [left["path"], right["path"]], "polarity": _mos_polarity(left),
                           "evidence": [_source_ref(left["source"]["path"], "connections"), _source_ref(right["source"]["path"], "connections")],
                           "facts": {"shared_gate_net": _connection(left, "g"), "diode_connected": diode["path"]}})
    # Tail candidate: a device whose drain is the differential pair's shared source.
    for pair in pairs:
        left, right, shared = pair
        tails = [item for item in mos if item not in (left, right) and _connection(item, "d") == shared and _connection(item, "s") == _connection(item, "b")]
        for tail in tails:
            candidates.append({"kind": "tail_device", "status": "confirmed" if len(tails) == 1 else "candidate",
                               "objects": [tail["path"]], "polarity": _mos_polarity(tail),
                               "evidence": [_source_ref(tail["source"]["path"], "connections"), _source_ref(left["source"]["path"], "connections.S")],
                               "facts": {"tail_net": shared, "source_net": _connection(tail, "s"), "gate_net": _connection(tail, "g")}})
    kinds = {item["kind"] for item in candidates}
    for kind, text in (("mos_input_differential_pair", "输入差分对"), ("current_mirror_load", "电流镜负载"), ("tail_device", "尾支路器件")):
        if kind not in kinds:
            findings.append(_finding("TOPOLOGY_NOT_CONFIRMED", [], "unconfirmed", ["design.instances"], f"未能仅凭端子—网络事实确认{text}。", "补充可验证的层次回读或由工程师确认设计意图。", "拓扑证据不足时"))
    return {"candidates": candidates, "mos_count": len(mos), "instance_count": len(instances), "scope": "bounded_three_structures"}, findings


def _resolve_named(name: Any, instances: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    text = str(name).casefold()
    matches = [item for item in instances if str(item.get("name", "")).casefold() == text or str(item.get("path", "")).casefold() == text]
    return matches[0] if len(matches) == 1 else None


def _role_objects(role: str, candidates: list[Mapping[str, Any]], instances: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:
    """Resolve only one connection-confirmed candidate; ambiguity is not intent."""
    aliases = {"input_pair": "mos_input_differential_pair", "tail": "tail_device", "mirror_load": "current_mirror_load"}
    kind = aliases.get(role, role)
    eligible = [candidate for candidate in candidates if candidate.get("kind") == kind and candidate.get("status") == "confirmed"]
    if len(eligible) != 1:
        return [], None
    candidate = eligible[0]
    paths = candidate.get("objects", [])
    expected_count = 1 if kind == "tail_device" else 2
    if not isinstance(paths, list) or len(paths) != expected_count:
        return [], None
    objects = [_resolve_named(path, instances) for path in paths]
    if any(item is None for item in objects):
        return [], None
    return [item for item in objects if item is not None], candidate


def _checks(design: Mapping[str, Any], op: Mapping[str, Any] | None, mapped: Mapping[str, Mapping[str, Any]], intent: Mapping[str, Any] | None,
            pairing: Mapping[str, Any], instances: list[Mapping[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    workspace: dict[str, Any] = {"status": "not_evaluated", "observations": [], "goals": []}
    matching: dict[str, Any] = {"status": "not_evaluated", "relations": []}
    evidence: dict[str, Any] = {"status": "complete", "observations": []}
    design_source = design.get("source", {}) if isinstance(design.get("source"), Mapping) else {}
    topology_candidates = _topology([dict(item) for item in instances])[0]["candidates"]
    if design_source.get("modified") is True or design_source.get("unsaved") is True:
        evidence["status"] = "incomplete"
        findings.append(_finding("DESIGN_UNSAVED_OR_EDITED", [], "unconfirmed", ["design.source.modified"], "回读来源标记为未保存或正在编辑，不能把磁盘事实冒充为当前 GUI 状态。", "保存设计后重新进行只读回读；本次仍仅报告已取得的事实。", "来源标记为 modified/unsaved 时"))
    if design_source.get("occupied") is True or design_source.get("locked") is True:
        evidence["status"] = "incomplete"
        findings.append(_finding("DESIGN_OCCUPIED_OR_LOCKED", [], "unconfirmed", ["design.source.occupied"], "设计来源标记为被占用或锁定，当前回读的时效性/一致性未能确认。", "由工程师结束冲突编辑后重新读取，不删除锁或接管会话。", "来源标记为 occupied/locked 时"))
    if not design.get("complete"):
        evidence["status"] = "incomplete"
        findings.append(_finding("DESIGN_READBACK_INCOMPLETE", [], "unconfirmed", ["design.completeness"], "原理图回读没有声明完整覆盖，不能把缺失对象当作不存在。", "先取得完整保存设计回读，再进行电气判断。", "设计回读不完整时"))
    if op is None:
        workspace["status"] = "not_evaluated"
        findings.append(_finding("OP_NOT_PROVIDED", [], "unconfirmed", ["review.input"], "本次只提供了结构事实，没有工作点结果；工作区未评估。", "提供与当前设计明确配对的原始 OP 结果；Reviewer 不会自动启动仿真。", "没有工作点输入时"))
    elif not pairing.get("workpoint_usable"):
        workspace["status"] = "blocked"
        findings.append(_finding("SOURCE_PAIRING_BLOCKED", [], "unconfirmed", pairing.get("evidence", []), f"工作点联合判断被阻止：{pairing.get('reason', '设计与结果来源未能证明一致')}。", "提供同一保存设计生成的网表/manifest 或显式设计指纹。", "工作点来源未匹配时"))
    else:
        workspace["status"] = "observed"
        for name, record in mapped.items():
            region = record.get("region")
            raw = region.get("raw") if isinstance(region, Mapping) else region
            workspace["observations"].append({"object_path": record.get("object_path", name), "region": raw, "evidence": record.get("source", {})})
        expected_regions = {}
        expected_roles = {}
        if isinstance(intent, Mapping):
            expected_regions.update(intent.get("expected_regions", {}) if isinstance(intent.get("expected_regions"), Mapping) else {})
            assumptions = intent.get("workspace") if isinstance(intent.get("workspace"), Mapping) else {}
            expected_regions.update(assumptions.get("expected_regions", {}) if isinstance(assumptions.get("expected_regions"), Mapping) else {})
            expected_roles.update(assumptions.get("expected_regions_by_role", {}) if isinstance(assumptions.get("expected_regions_by_role"), Mapping) else {})
        requested_goals: list[tuple[str, Mapping[str, Any], Any, Mapping[str, Any] | None]] = []
        for name, expected in expected_regions.items():
            selected = _resolve_named(name, instances)
            if selected is None:
                findings.append(_finding("WORKSPACE_OBJECT_MISSING", [str(name)], "unconfirmed", ["intent.expected_regions", "design.instances"], "显式工作区假设引用的器件不能唯一定位。", "核对稳定实例路径和仿真结果来源。", "给出了该器件的工作区假设时"))
            else:
                requested_goals.append((f"intent.expected_regions.{name}", selected, expected, None))
        for role, expected in expected_roles.items():
            selected, candidate = _role_objects(str(role), topology_candidates, instances)
            if candidate is None:
                findings.append(_finding("WORKSPACE_ROLE_TOPOLOGY_UNCONFIRMED", [], "unconfirmed", [f"intent.workspace.expected_regions_by_role.{role}", "topology.candidates"], f"连接事实未能唯一确认 {role} 角色，未把工作区目标强加于器件。", "由工程师核对该角色的端子—网络关系。", "显式声明角色工作区目标时"))
            for item in selected:
                requested_goals.append((f"intent.workspace.expected_regions_by_role.{role}", item, expected, candidate))
        for intent_ref, item, expected, candidate in requested_goals:
            object_path = str(item.get("path", ""))
            record = mapped.get(str(item.get("name", "")).casefold())
            actual = record.get("region") if record is not None else None
            actual = actual.get("raw") if isinstance(actual, Mapping) else actual
            fact = {"object_path": object_path, "region": actual, "op_fields": {field: copy.deepcopy(record[field]) for field in ("id", "vgs", "vds", "vbs", "vth", "vdsat", "gm", "gds") if record is not None and field in record}, "op_source": copy.deepcopy(record.get("source", {})) if record else {}, "connections": copy.deepcopy(item.get("connections", {})), "design_source": copy.deepcopy(item.get("source", {}))}
            goal = {"source": intent_ref, "expected_region": expected, "role": candidate.get("kind") if candidate else None, "role_evidence": copy.deepcopy(candidate.get("evidence", [])) if candidate else []}
            comparison = {"object_path": object_path, "fact": fact, "intent": goal, "status": "unconfirmed"}
            if record is None or not actual:
                finding = _finding("WORKSPACE_REGION_UNCONFIRMED", [object_path], "unconfirmed", [fact["op_source"], intent_ref], "目标器件缺少可确认的原始工作区记录，未判断是否符合目标。", "核对该器件的 OP 映射与原始 region 字段。", "显式声明工作区目标时")
            elif _normalize_region(actual) == _normalize_region(expected):
                comparison["status"] = "met"
                finding = None
            else:
                comparison["status"] = "mismatch"
                connections = item.get("connections", {})
                nodes = ", ".join(f"{terminal}={_connection(item, terminal)}" for terminal in ("d", "g", "s", "b") if _connection(item, terminal))
                finding = _finding("WORKSPACE_REGION_MISMATCH", [object_path], "suspicion", [fact["op_source"], *goal["role_evidence"], intent_ref], f"原始 region 为 {actual!r}，不符合本次声明的 {expected!r} 工作区目标；这不是接线错误或唯一根因的证明。", f"优先核对 {nodes} 的实际偏置及相关 OP 电压，再核对端子连接与目标条件；不要据此直接改尺寸。", "用户本次明确声明该角色/器件的工作区目标时")
            workspace["goals"].append(comparison)
            if finding is not None:
                finding["fact"] = fact
                finding["intent"] = goal
                finding["judgement"] = comparison["status"]
                findings.append(finding)
    relations = []
    if isinstance(intent, Mapping):
        relations = intent.get("matching") if isinstance(intent.get("matching"), list) else intent.get("mirrors", []) if isinstance(intent.get("mirrors"), list) else []
    if relations:
        matching["status"] = "evaluated"
        for relation in relations:
            if not isinstance(relation, Mapping):
                continue
            names = relation.get("objects") or relation.get("devices") or []
            role = relation.get("role")
            role_candidate = None
            if role and not names:
                selected, role_candidate = _role_objects(str(role), topology_candidates, instances)
                if role_candidate is None:
                    findings.append(_finding("MATCHING_ROLE_TOPOLOGY_UNCONFIRMED", [], "unconfirmed", ["intent.matching.role", "topology.candidates"], f"连接事实未能唯一确认 {role} 角色，未执行其参数一致性检查。", "由工程师核对角色对象和端子—网络关系。", "使用结构角色指定匹配关系时"))
                    continue
                names = [item.get("path") for item in selected]
            if not isinstance(names, list) or len(names) < 2:
                findings.append(_finding("MATCHING_INTENT_INCOMPLETE", [], "unconfirmed", ["intent.matching"], "匹配关系没有至少两个明确对象。", "补充实例路径和有效参数字段。", "匹配意图不完整时"))
                continue
            objects = [_resolve_named(name, instances) for name in names]
            if any(item is None for item in objects):
                findings.append(_finding("MATCHING_OBJECT_MISSING", [str(name) for name in names], "unconfirmed", ["intent.matching", "design.instances"], "匹配关系引用的对象无法从当前回读定位。", "使用稳定层次路径并重新读取设计。", "对象无法定位时"))
                continue
            fields = relation.get("fields") or relation.get("parameters")
            if not isinstance(fields, list) or not fields:
                findings.append(_finding("MATCHING_FIELDS_UNCONFIRMED", [str(item.get("path")) for item in objects], "unconfirmed", ["intent.matching"], "匹配意图没有声明要比较的有效参数字段，不能假设所有电流镜都是 1:1。", "明确给出 Length/Single_Width/m 等语义及目标比例。", "没有有效参数字段时"))
                continue
            relation_result = {"objects": [item.get("path") for item in objects], "fields": fields, "role": role_candidate.get("kind") if role_candidate else None, "role_evidence": copy.deepcopy(role_candidate.get("evidence", [])) if role_candidate else [], "comparisons": []}
            for field in fields:
                values = [item.get("parameters", {}).get(field) for item in objects]
                numeric = [_engineering(value) for value in values]
                if any(value is None or status != "parsed" for value, (_, _, status) in zip(values, numeric)):
                    equal = len({str(value) for value in values}) == 1 if all(value is not None for value in values) else None
                else:
                    equal = all(math.isclose(numeric[0][0], item[0], rel_tol=1e-9, abs_tol=1e-18) for item in numeric[1:])
                relation_result["comparisons"].append({"field": field, "values": values, "value_records": [{"raw": value, "value": parsed, "unit": "m" if str(field).casefold() in {"length", "l", "single_width", "width", "w"} else "", "parse_status": status} for value, (parsed, _, status) in zip(values, numeric)], "equal": equal, "evidence": [f"design.instances.{item.get('path')}.parameters.{field}" for item in objects]})
            matching["relations"].append(relation_result)
            target_ratio = relation.get("ratio")
            for comparison in relation_result["comparisons"]:
                if comparison["equal"] is None:
                    findings.append(_finding("MATCHING_PARAMETER_UNCONFIRMED", [str(item.get("path")) for item in objects], "unconfirmed", comparison["evidence"], f"字段 {comparison['field']} 缺少可比较的参数值。", "核对当前保存设计的参数来源与单位。", "显式匹配目标但参数缺失时"))
                    continue
                if comparison["equal"]:
                    continue
                field_name = str(comparison["field"]).casefold()
                if field_name in {"single_width", "width", "w"} and target_ratio is not None:
                    continue
                finding = _finding("MATCHING_PARAMETER_MISMATCH", [str(item.get("path")) for item in objects], "suspicion", [*comparison.get("evidence", []), *relation_result["role_evidence"], "intent.matching"], f"本次显式目标要求字段 {comparison['field']} 一致，但保存设计中的原始值为 {comparison['values']}；不能据此推导电流比、失调量或唯一原因。", "先核对两器件的原始 Length、有效尺寸语义和该匹配目标是否仍适用，再决定是否需要受控设计变更。", "用户本次明确声明该参数一致性目标时")
                finding["fact"] = {"objects": relation_result["objects"], "field": comparison["field"], "values": comparison["value_records"], "evidence": comparison["evidence"]}
                finding["intent"] = {"source": "intent.matching", "role": relation_result["role"], "fields": fields, "role_evidence": relation_result["role_evidence"]}
                finding["judgement"] = "mismatch"
                findings.append(finding)
            if target_ratio is not None and len(objects) == 2 and fields:
                width_field = next((field for field in fields if str(field).casefold() in {"single_width", "width", "w"}), None)
                if width_field:
                    left, _, left_status = _engineering(objects[0].get("parameters", {}).get(width_field, ""))
                    right, _, right_status = _engineering(objects[1].get("parameters", {}).get(width_field, ""))
                    modifiers = [objects[i].get("parameters", {}).get(name, "") for i in (0, 1) for name in ("Fingers", "m", "simM")]
                    if left_status != "parsed" or right_status != "parsed" or any(str(item) not in {"", "1", "1.0", "1u"} for item in modifiers):
                        findings.append(_finding("MATCHING_EFFECTIVE_WIDTH_UNCONFIRMED", [str(item.get("path")) for item in objects], "unconfirmed", relation_result["comparisons"], "有效宽度比例还受 Fingers/m/simM 或 PDK 语义影响，当前资料不足以确认目标比例。", "确认 PDK 的有效宽度和倍乘语义后再判断比例。", "声明了比例但倍乘语义未确认时"))
                    else:
                        try:
                            ratio = float(target_ratio)
                        except (TypeError, ValueError):
                            ratio = None
                        if ratio is None:
                            findings.append(_finding("MATCHING_RATIO_UNCONFIRMED", [str(item.get("path")) for item in objects], "unconfirmed", relation_result["comparisons"], "匹配目标比例不是明确数值，未猜测其含义。", "提供明确的数值比例。", "显式给出非数值比例时"))
                        elif right and left and not math.isclose(right / left, ratio, rel_tol=1e-6):
                            findings.append(_finding("MATCHING_RATIO_MISMATCH", [str(item.get("path")) for item in objects], "suspicion", relation_result["comparisons"], f"按声明字段计算的宽度比例为 {right / left:g}，目标为 {ratio:g}。", "检查器件有效宽度、倍乘参数和匹配意图。", "显式给出目标比例且参数语义已确认时"))
    else:
        matching["status"] = "not_evaluated"
    # Role assertions are evaluated only when the caller explicitly supplies
    # them.  The structural detector itself does not infer design intent.
    if isinstance(intent, Mapping):
        for role in ("input_pair", "tail"):
            requested = intent.get(role)
            if requested is None:
                continue
            requested = requested if isinstance(requested, list) else [requested]
            resolved = [_resolve_named(name, instances) for name in requested]
            if any(item is None for item in resolved):
                findings.append(_finding("WORKSPACE_ROLE_OBJECT_MISSING", [str(name) for name in requested], "unconfirmed", [f"intent.{role}", "design.instances"], f"显式 {role} 假设引用了无法定位的器件。", "核对实例名称或使用稳定层次路径。", "显式声明了该角色时"))
                continue
            expected_set = {str(item.get("path")) for item in resolved}
            kind = "mos_input_differential_pair" if role == "input_pair" else "tail_device"
            if not any(expected_set == set(candidate.get("objects", [])) for candidate in topology_candidates if candidate.get("kind") == kind):
                findings.append(_finding("WORKSPACE_ROLE_TOPOLOGY_UNCONFIRMED", [str(item.get("path")) for item in resolved], "unconfirmed", [f"intent.{role}", "topology.candidates"], f"端子—网络事实没有确认显式 {role} 假设。", "由工程师核对角色意图与实际连接，不自动修改设计。", "显式声明了该角色时"))
    # Parameter credibility is independent of whether an OP was supplied.
    mos = [item for item in instances if item.get("is_mos")]
    if op is not None and pairing.get("workpoint_usable"):
        for item in mos:
            record = mapped.get(str(item.get("name", "")).casefold())
            if record is None:
                continue
            for field in _OP_REQUIRED:
                if field not in record or record.get(field) in (None, ""):
                    findings.append(_finding("OP_FIELD_UNCONFIRMED", [str(item.get("path"))], "unconfirmed", [record.get("source", {}), f"operating_point.devices.{item.get('name')}.{field}"], f"工作点缺少 {field} 原始字段，未用 0 或推断值替代。", "补充包含该字段的原始 OP 记录。", "需要该字段进行判断时"))
            for field, value in record.items():
                if field in {"device", "name", "hierarchy", "region", "region_normalized", "source", "object_path", "raw_device"}:
                    continue
                if isinstance(value, Mapping) and value.get("parse_status") == "unparsed":
                    findings.append(_finding("OP_PARAMETER_SEMANTICS_UNCONFIRMED", [str(item.get("path"))], "unconfirmed", [value.get("source", record.get("source", {})), f"operating_point.devices.{item.get('name')}.{field}"], f"工作点字段 {field} 的原始值 {value.get('raw', '')!r} 无法解析，未猜测其数值或单位。", "保留原始字段并由仿真器/工程师确认其语义。", "OP 字段无法解析时"))
    numeric_parameter_names = {"length", "l", "single_width", "width", "w", "m", "simm", "fingers", "pd", "ps", "ad", "as", "rd", "rs", "r", "c"}
    for item in instances:
        for field, value in item.get("parameters", {}).items():
            # CDF choice strings such as ``None`` and ``Mid`` are preserved as
            # facts, not misreported as malformed numeric expressions.  Only
            # fields whose names normally carry a numeric effective value (or
            # strings containing expression syntax) are gated here.
            field_name = str(field).casefold()
            expression_like = isinstance(value, str) and any(token in value for token in ("+", "-", "*", "/", "(", ")", "="))
            if isinstance(value, str) and value and _engineering(value)[2] == "unparsed" and (field_name in numeric_parameter_names or expression_like):
                findings.append(_finding("PARAMETER_SEMANTICS_UNCONFIRMED", [str(item.get("path"))], "unconfirmed", [f"design.instances.{item.get('path')}.parameters.{field}"], f"参数 {field} 的值 {value!r} 不是可解析的数值字面量，未猜测其含义。", "由工程师或 PDK 文档确认表达式/枚举语义。", "参数不是明确数值时"))
    if any(any(field in item.get("parameters", {}) for field in ("pd", "ps")) for item in instances):
        findings.append(_finding("PD_PS_PROCESS_BASIS_UNCONFIRMED", [], "unconfirmed", ["design.instances.*.parameters.pd/ps"], "实例中存在 pd/ps 显式值，但当前 Reviewer 没有 PDK perimeter callback 或设计规则依据，不能宣称工艺合法或违规。", "由 PDK 工程师确认 perimeter 语义和设计规则。", "涉及 pd/ps 参数时"))
    evidence["observations"] = [{"object_path": item.get("path"), "parameter_count": len(item.get("parameters", {}))} for item in instances]
    return {"workspace": workspace, "matching": matching, "evidence": evidence}, findings


def review_design(readback: Mapping[str, Any], *, op: Any = None, intent: Mapping[str, Any] | None = None,
                  source: Mapping[str, Any] | None = None, result: Any = None, netlist: Any = None,
                  trial_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Review a saved readback and optionally a paired operating point.

    ``result`` is an alias for ``op`` for callers that already use the result
    vocabulary.  Passing both is rejected to avoid silently choosing one
    source.  The returned ``ok`` means only that the review executed; it does
    not mean that the circuit passed the checks.
    """
    if op is not None and result is not None:
        raise ReviewInputError("provide only one of op and result")
    if result is not None:
        op = result
    normalized = _unwrap_readback(readback)
    design = _design_payload(normalized)
    review_source = dict(source or {}) if isinstance(source, Mapping) else {}
    if isinstance(op, (str, Path)):
        op_candidate = Path(op).expanduser()
        if op_candidate.is_file() and not review_source.get("run_identity") and design.get("source", {}).get("kind") != "offline_synthetic_fixture":
            review_source["run_identity"] = {"verified": False, "same_manifest": False, "reason": "file OP was supplied without a verified run manifest"}
    instances = design["instances"]
    op_payload = parse_operating_point(op, source=review_source) if op is not None else None
    mapped, unmatched, missing = _map_op_devices(op_payload, instances) if op_payload is not None else ({}, [], [])
    pairing = _pair_sources(design, op_payload, review_source, netlist=netlist)
    topology, topology_findings = _topology(instances)
    checks, check_findings = _checks(design, op_payload, mapped if pairing.get("workpoint_usable") else {}, intent, pairing, instances)
    findings = topology_findings + check_findings
    if unmatched:
        findings.append(_finding("OP_DEVICE_UNMAPPED", [], "unconfirmed", [item.get("source", {}) for item in unmatched], "部分 OP 器件无法通过显式实例名映射到当前设计，未合并同名记录。", "提供完整层次路径或核对网表映射。", "OP 有未匹配器件时"))
    if missing and pairing.get("workpoint_usable"):
        findings.append(_finding("OP_DEVICE_MISSING", missing, "unconfirmed", ["design.instances", "operating_point.devices"], "当前设计中的器件缺少对应 OP 记录，器件级工作区覆盖不完整。", "使用包含全部器件的原始 OP 结果。", "OP 缺少设计器件时"))
    coverage = {
        "design": "complete" if design["complete"] else "incomplete",
        "topology": "complete" if all(item.get("status") == "confirmed" for item in topology["candidates"]) and {"mos_input_differential_pair", "current_mirror_load", "tail_device"} <= {item["kind"] for item in topology["candidates"]} else "partial",
        "operating_point": "matched" if pairing.get("workpoint_usable") else "blocked" if op_payload is not None else "not_requested",
        "pairing_status": pairing.get("status", "not_requested"),
        "checks": "complete",
    }
    coverage["complete"] = coverage["design"] == "complete" and coverage["operating_point"] in {"matched", "not_requested"} and checks.get("evidence", {}).get("status") == "complete"
    return {
        "schema_version": SCHEMA_VERSION,
        "reviewer": "aetherpilot.readonly",
        "ok": True,
        "execution": {"status": "complete", "read_only": True, "writes_database": False, "starts_simulation": False, "modifies_configuration": False},
        "mode": "structure_and_operating_point" if op_payload is not None else "structure",
        "coverage": coverage,
        "pairing": pairing,
        "source": copy.deepcopy(review_source),
        "trial_context": copy.deepcopy(dict(trial_context)) if isinstance(trial_context, Mapping) else {},
        "design": {"target": design["target"], "complete": design["complete"], "instances": instances, "pins": design["pins"], "nets": design["nets"], "content_sha256": design["content_sha256"]},
        "topology": topology,
        "operating_point": {"available": op_payload is not None, "devices": mapped if pairing.get("workpoint_usable") else {}, "unmatched": unmatched, "missing": missing, "source": op_payload.get("source", {}) if op_payload else {}},
        "checks": checks,
        "findings": findings,
        "summary": {"execution_ok": True, "coverage_complete": coverage["complete"], "finding_count": len(findings), "electrical_judgement": "available" if pairing.get("workpoint_usable") else "not_available"},
    }


def render_review_markdown(review: Mapping[str, Any]) -> str:
    """Explain existing review facts without extending the electrical rules.

    Optional ``trial_context`` is caller-supplied provenance, never a substitute
    for ``pairing``.  Derived descriptions are Markdown-only and do not mutate
    the structured facts or turn a field difference into a region rule.
    """
    def _text(value: Any, default: str = "未确认") -> str:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, Mapping):
            return "；".join(f"{key}：{_text(item)}" for key, item in value.items()) or default
        if isinstance(value, (list, tuple)):
            return "、".join(_text(item) for item in value) or default
        return str(value).replace("|", "\\|").replace("\n", " ")

    def _status(value: Any) -> str:
        return {"matched": "已配对", "rejected": "已拒绝", "unverified": "未验证", "not_requested": "未请求", "available": "可用", "not_available": "不可用", "confirmed": "连接事实支持", "candidate": "候选，尚未唯一确认"}.get(str(value), "未确认")

    def _sentence(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            text = "；".join(_text(item).rstrip("。；;，,") for item in value)
        else:
            text = _text(value)
        return text.rstrip("。；;，,") + "。"

    def _role_label(kind: Any) -> str:
        return {
            "mos_input_differential_pair": "输入差分对",
            "current_mirror_load": "电流镜负载",
            "tail_device": "尾支路",
        }.get(str(kind), str(kind or "未确认角色"))

    def _field_text(device: Mapping[str, Any], field: str) -> str:
        value = device.get(field)
        if not isinstance(value, Mapping):
            return "未确认"
        raw = value.get("raw")
        unit = value.get("unit") or "未确认单位"
        if raw is not None and str(raw) != "":
            return f"{_text(raw)} {_text(unit)}"
        numeric = value.get("value")
        return f"{_text(numeric)} {_text(unit)}" if numeric is not None else "未确认"

    def _source_text(source: Any, field: str | None = None) -> str:
        if not isinstance(source, Mapping):
            return "来源未确认"
        path = source.get("path", "未知")
        line = source.get("line")
        section = source.get("section")
        details = []
        if line is not None:
            details.append(f"区段起始行 {line}" if section else f"行 {line}")
        if section:
            details.append(f"区段 {_text(section)}")
        if source.get("column") is not None:
            details.append(f"器件列索引 {_text(source['column'])}（从 0 开始）")
        if field or source.get("field"):
            details.append(f"字段 {_text(field or source.get('field'))}")
        return f"来源 {_text(path)}" + (f"（{'，'.join(details)}）" if details else "")

    def _evidence_text(evidence: Any) -> str:
        items = evidence if isinstance(evidence, list) else [evidence]
        return "；".join(_source_text(item) if isinstance(item, Mapping) else _text(item) for item in items) or "来源未确认"

    def _structure_sentence(item: Mapping[str, Any]) -> str:
        kind = item.get("kind")
        objects = _text(item.get("objects", []))
        facts = item.get("facts", {}) if isinstance(item.get("facts"), Mapping) else {}
        if kind == "mos_input_differential_pair":
            gates = ", ".join(str(value) for value in facts.get("gate_nets", [])) or "未确认"
            shared = facts.get("shared_source_net", "未确认")
            return f"输入差分对 {objects}：两个栅极分别接 {gates}，源极共接 {shared}，用于把输入电压差转换为差分电流。"
        if kind == "current_mirror_load":
            diode = facts.get("diode_connected", "未确认")
            shared = facts.get("shared_gate_net", "未确认")
            return f"电流镜负载 {objects}：{diode} 为二极管连接参考管，共享栅网 {shared}，用于把参考支路电流镜像到另一支路。"
        if kind == "tail_device":
            return f"尾支路 {objects}：栅极接 {facts.get('gate_net', '未确认')}，源极接 {facts.get('source_net', '未确认')}，漏极接尾节点 {facts.get('tail_net', '未确认')}，用于设定输入对的尾电流。"
        return f"{_role_label(kind)} {objects}：端子—网络关系已读取，但该角色不在本轮有限结构说明范围内。"

    design = review.get("design", {})
    coverage = review.get("coverage", {})
    summary = review.get("summary", {})
    findings = [item for item in review.get("findings", []) if isinstance(item, Mapping)]
    suspicions = [item for item in findings if item.get("classification") == "suspicion"]
    unknown = [item for item in findings if item.get("classification") == "unconfirmed"]
    pairing = review.get("pairing", {})
    context = review.get("trial_context", {}) if isinstance(review.get("trial_context"), Mapping) else {}
    paired = pairing.get("status") == "matched" and pairing.get("workpoint_usable") is True
    lines = ["# 只读 Design Review", "", "## 本次结论与适用范围", "",
             f"执行状态：{'已完成' if summary.get('execution_ok') is True else '未确认完成'}；证据覆盖：{'完整' if coverage.get('complete') is True else '不完整'}；工作点联合判断：{_status(summary.get('electrical_judgement'))}。本结论只适用于本次提供的设计快照、已配对输入和显式意图，不表示电路或 PDK 已通过签核。",
             "", f"Review 模式：{'结构与工作点' if review.get('mode') == 'structure_and_operating_point' else '仅结构'}；来源配对：{_status(pairing.get('status'))}；记录事项 {len(findings)} 项。"]
    target = design.get("target", {})
    if isinstance(target, Mapping) and target:
        lines.extend(["", f"DUT：{'/'.join(_text(target.get(key)) for key in ('library', 'cell', 'view'))}。"])
    if context.get("input_mode") == "current_native" and context.get("database_queried_this_run") is True:
        scope = "输入模式：本次原生只读查询；本轮实际查询了数据库。"
    elif context.get("input_mode") == "historical_files":
        scope = "输入模式：历史文件重评；所用设计事实来自历史快照，不证明当前保存工程状态。"
    else:
        scope = "输入模式：已提供的回读文件；本报告没有证据证明本轮重新查询数据库，不宣称已检查当前保存工程。"
    lines.extend(["", scope])
    for key, label in (("review_timestamp", "本轮 Review 时间"), ("snapshot_timestamp", "设计快照时间"), ("selected_run", "指定历史仿真 operation"), ("dut_read_operation", "DUT 读取 operation"), ("testbench_read_operation", "testbench 读取 operation")):
        if context.get(key):
            lines.append(f"{label}：{_text(context[key])}。")
            lines.append("")
    if context.get("selected_run"):
        lines.append("上述工作点来自指定历史仿真，本轮未启动新仿真。")
    if context.get("pairing_scope"):
        lines.extend(["", "已核对的来源范围：" + _sentence(context["pairing_scope"])])
    lines.extend(["", "## 简短结构解释", ""])
    lines.append(f"实例 {len(design.get('instances', []))} 个，端口 {len(design.get('pins', []))} 个，网络 {len(design.get('nets', []))} 个。")
    lines.append("")
    for item in review.get("topology", {}).get("candidates", []):
        lines.append(f"- {_structure_sentence(item)}（识别状态：{_status(item.get('status'))}。）")
    extra_instances = [item for item in design.get("instances", []) if not item.get("is_mos")]
    if extra_instances:
        for item in extra_instances:
            connections = item.get("connections", {}) if isinstance(item.get("connections"), Mapping) else {}
            connection_text = ", ".join(f"{key}={_text(value)}" for key, value in connections.items()) or "端子未确认"
            parameters = item.get("parameters", {}) if isinstance(item.get("parameters"), Mapping) else {}
            parameter_text = ", ".join(f"{key}={_text(value)}" for key, value in parameters.items() if value not in (None, "")) or "参数未确认"
            lines.append(f"- 辅助器件 {item.get('path', item.get('name', '未命名'))}：端子 {connection_text}；参数 {parameter_text}。它未参与本轮工作区或镜像长度判断。")
    devices = review.get("operating_point", {}).get("devices", {})
    if paired and isinstance(devices, Mapping) and devices:
        role_by_object: dict[str, str] = {}
        for candidate in review.get("topology", {}).get("candidates", []):
            for object_path in candidate.get("objects", []):
                role_by_object[str(object_path).casefold()] = _role_label(candidate.get("kind"))
        lines.extend(["", "## 关键工作点", "", "以下数值保留已配对 OP 的原始工程后缀和正负号，工作区同时列出原始文本与有限规范化结果。来源行号为区段起始位置，不是各字段所在行；精确字段和器件列以 JSON 中对应字段的 source 为准。", "", "| 器件 | 角色 | 原始/规范化工作区 | Id | VGS | VDS | VDSAT |", "| --- | --- | --- | ---: | ---: | ---: | ---: |"])
        ordered_devices = sorted(devices.values(), key=lambda item: str(item.get("object_path", item.get("name", ""))))
        for device in ordered_devices:
            object_path = str(device.get("object_path", device.get("name", "未命名")))
            raw_region = device.get("region", "未确认")
            if isinstance(raw_region, Mapping):
                raw_region = raw_region.get("raw", "未确认")
            normalized_region = device.get("region_normalized") or "未确认"
            source = device.get("source", {}) if isinstance(device.get("source"), Mapping) else {}
            source_text = _source_text(source)
            lines.append(f"| {_text(object_path)} | {role_by_object.get(object_path.casefold(), '未确认角色')} | {_text(raw_region)} / {_text(normalized_region)}；{source_text} | {_field_text(device, 'id')} | {_field_text(device, 'vgs')} | {_field_text(device, 'vds')} | {_field_text(device, 'vdsat')} |")
    lines.extend(["", "## 与本次目标有关的重点发现", ""])
    goals = review.get("checks", {}).get("workspace", {}).get("goals", [])
    met_goals = [item for item in goals if item.get("status") == "met"]
    lines.extend(["", "### 目标符合情况", ""])
    if paired and met_goals:
        lines.append("符合项：" + "；".join(f"{item.get('object_path')} 的工作区为 {item.get('fact', {}).get('region')}，满足声明的 {item.get('intent', {}).get('expected_region')} 目标" for item in met_goals) + "。")
    elif not paired:
        lines.append("没有已配对且可用的 OP，本次不作工作区目标符合性判断。")
    else:
        lines.append("没有可报告的工作区目标符合项；仅针对显式意图判断，不把未声明的工作区当作通过或失败。")
    matching_relations = review.get("checks", {}).get("matching", {}).get("relations", [])
    matching_summaries = []
    for relation in matching_relations:
        for comparison in relation.get("comparisons", []):
            objects = ", ".join(str(value) for value in relation.get("objects", []))
            if comparison.get("equal") is True:
                values = "、".join(_text(value) for value in comparison.get("values", []))
                matching_summaries.append(f"{objects} 的 {comparison.get('field')} 一致（原始值 {values}）")
    if matching_summaries:
        lines.append("参数目标：" + "；".join(matching_summaries) + "。")
    def _derived_tail(fact: Mapping[str, Any], object_path: str) -> str | None:
        instance = next((item for item in design.get("instances", []) if item.get("path") == object_path), None)
        fields = fact.get("op_fields", {})
        values = [fields.get(key) for key in ("vds", "vdsat")]
        if not paired or not instance or not instance.get("is_mos") or _mos_polarity(instance) != "n":
            return None
        if any(not isinstance(value, Mapping) or value.get("parse_status") != "parsed" or value.get("unit") != "V" or isinstance(value.get("value"), bool) or not isinstance(value.get("value"), (float, int)) or not math.isfinite(value["value"]) or value["value"] < 0 for value in values):
            return None
        vds, vdsat = values
        difference = vds["value"] - vdsat["value"]
        refs = "；".join(_source_text(value.get("source", {}), field) for value, field in zip(values, ("vds", "vdsat")))
        return f"派生辅助量（非仿真器原始字段）：VDS−VDSAT = {vds['value']:.7g} V − {vdsat['value']:.7g} V = {difference:.7g} V（约 {difference:.4f} V）。依据已识别的 NMOS 和两个非负、同为 V 的已解析字段作同向差值；输入引用：{refs}。这不是通用工作区判据，不适用于未经核实的 PMOS／符号约定，也不表示将节点电压改变该差值即可修好电路。"

    if suspicions:
        for index, item in enumerate(suspicions, 1):
            rule = item.get("rule_id")
            fact = item.get("fact", {})
            intent = item.get("intent", {})
            objects = item.get("object_path", [])
            lines.extend(["", f"### {index}. {_text(objects)}：{'工作区偏离目标' if rule == 'WORKSPACE_REGION_MISMATCH' else '参数一致性偏离目标' if rule == 'MATCHING_PARAMETER_MISMATCH' else _text(rule)}", ""])
            if rule == "WORKSPACE_REGION_MISMATCH" and paired:
                fields = fact.get("op_fields", {})
                connections = fact.get("connections", {})
                connection_text = "，".join(f"{key}={_text(value)}" for key, value in connections.items()) or "未确认"
                lines.append(f"事实与数字：原始 region={_text(fact.get('region'))}；VDS={_field_text(fields, 'vds')}，VDSAT={_field_text(fields, 'vdsat')}，VGS={_field_text(fields, 'vgs')}，Id={_field_text(fields, 'id')}。端子连接为 {connection_text}。证据：{_source_text(fact.get('op_source', {}), 'region')}；{_source_text(fact.get('design_source', {}), 'connections')}。")
                is_tail = intent.get("role") == "tail_device"
                if is_tail:
                    derived = _derived_tail(fact, str(objects[0]) if objects else "")
                    if derived:
                        lines.extend(["", derived])
                lines.extend(["", f"本次目标：{_role_label(intent.get('role'))} 的工作区为 {_text(intent.get('expected_region'))}；显式目标来源为 {_text(intent.get('source'))}。"])
                if is_tail and _normalize_region(fact.get("region")) == "linear" and _normalize_region(intent.get("expected_region")) == "saturation":
                    tail = _connection(fact, "d") or "未确认尾节点"
                    bias = _connection(fact, "g") or "未确认尾偏置"
                    source_net = _connection(fact, "s") or "未确认源节点"
                    lines.extend(["", f"条件性解释：只有在上述饱和区尾电流源目标仍适用时，原始 Linear 才构成本次目标偏离。漏极接 {tail}，栅极接 {bias}，源极接 {source_net}；尾节点同时连接输入对源极，因此尾管的漏源电压、尾偏置与输入对栅源偏置需要协调核对。它提示尾支路的偏置／电压余量值得优先检查，不证明接线错误或唯一根因。", "", "优先核对什么、为什么：", "", f"1. 先核对 {tail} 相对 {source_net} 的实际电压与已配对 VDS；尾节点决定尾管漏源电压，先确认电压与连接事实一致。", f"2. 再核对 {bias} 相对 {source_net} 的偏置与 VGS、VDSAT；尾偏置决定该 OP 的导通要求，必须与尾节点电压一起解释，不能只按差值改节点。", "3. 然后核对输入共模与尾节点的协调关系；输入对栅源偏置会影响尾节点。当前报告未提供经配对 testbench 证明的输入共模数值，该数值及激励、负载配置仍待核对，不能从 DUT／OP 反推为已验证配置。"])
                else:
                    lines.extend(["", "条件性解释：这是原始工作区与显式目标的差异，不单独证明连接错误或唯一根因。", "", f"优先核对什么、为什么：{_text(item.get('recommendation'))} 首先确认偏置和目标条件一致，才能区分目标变更与器件工作点偏离。"])
            elif rule == "MATCHING_PARAMETER_MISMATCH":
                names = fact.get("objects", objects)
                values = fact.get("values", [])
                value_text = "；".join(f"{_text(name)}={_text(value.get('raw'))} {_text(value.get('unit'), '单位未确认')}" for name, value in zip(names, values) if isinstance(value, Mapping)) or "参数值未确认"
                lines.extend([f"事实与数字：{_text(fact.get('field'))} 的原始值为 {value_text}。证据：{_evidence_text(fact.get('evidence', []))}。", "", f"本次目标：{_text(names)} 的 {_text(fact.get('field'))} 一致；显式目标来源为 {_text(intent.get('source'))}。", "", "条件性解释：仅在这项一致性目标仍适用时，原始参数不一致才是需要确认的偏离。不能只由长度差推出电流比、失调量或唯一根因，也不能默认所有镜像都要求 1:1。", "", "优先核对什么、为什么：", "", "1. 先确认该参数一致性目标是否仍适用；刻意采用不同尺寸与意外偏离需要不同解释。", "2. 再核对两器件的原始参数、单位和有效尺寸语义；只有语义与目标均确认后，才有依据讨论受控设计调整，本报告不自动修复。"])
            else:
                lines.extend([f"事实与条件性解释：{_text(item.get('explanation'))}", "", f"适用条件：{_text(item.get('applies_when'))}。证据：{_evidence_text(item.get('evidence', []))}。", "", f"优先核对：{_text(item.get('recommendation'))}"])
    else:
        lines.append("在已声明规则边界内没有条件性疑点。")
    lines.extend(["", "## 证据限制", ""])
    limitations = context.get("testbench_limitations")
    if limitations:
        lines.append(_sentence(limitations))
        lines.append("")
    else:
        lines.extend(["本报告未证明 testbench 全部仿真配置、输入共模和外部负载已与所选历史运行全面核对；没有读取上下文时，也不声称本轮读取了 testbench。", ""])
    run_identity = review.get("source", {}).get("run_identity", {}) if isinstance(review.get("source"), Mapping) else {}
    if run_identity:
        lines.append("")
        lines.append(f"运行证据 manifest：{'确认属于同一清单' if run_identity.get('same_manifest') is True else '尚未确认属于同一清单'}；文件摘要{'校验通过' if run_identity.get('verified') is True else '未确认通过校验'}。清单和配置摘要保留在 JSON 中。")
        lines.append("")
    if unknown:
        for item in unknown:
            lines.append(f"- {_text(item.get('rule_id'))}：{_text(item.get('explanation'))} 待确认：{_text(item.get('recommendation'))}")
    else:
        lines.append("没有额外未确认事项。")
    lines.extend(["", "以上只读报告分别保留原始事实、派生计算和条件性工程解释；建议不构成自动修复承诺，工程师试用及 GUI 确认仍需另行记录。"])
    return "\n".join(lines) + "\n"


def _find_nearby(path: Path, pattern: str) -> Path | None:
    for root in (path.parent, *path.parents):
        try:
            matches = sorted(root.glob(pattern))
        except OSError:
            matches = []
        if matches:
            return matches[0]
    return None


def _find_matching_netlist(path: Path, expected_sha256: str | None = None) -> Path | None:
    """Find the run netlist, preferring the manifest-selected fingerprint."""
    candidates: list[Path] = []
    roots: list[Path] = [path.parent]
    for ancestor in (path.parent, *path.parents):
        if re.fullmatch(r"[0-9a-f]{32}", ancestor.name, re.I):
            # Do not glob from ``/home`` or ``/``: review inputs may live in a
            # large workspace.  A run bundle has a bounded, known layout.
            roots.extend([ancestor / "maestro", ancestor / "maestro" / "run" / "main", ancestor / "maestro" / "run" / "main" / "netlist"])
            break
    for root in roots:
        try:
            candidates.extend(item for item in root.glob("**/*.netlist") if item.is_file())
        except OSError:
            continue
    unique = sorted({item.resolve() for item in candidates})
    if expected_sha256:
        for item in unique:
            try:
                if hashlib.sha256(item.read_bytes()).hexdigest() == expected_sha256:
                    return item
            except OSError:
                continue
    return unique[0] if unique else None


def _run_manifest_identity(manifest_path: Path, op_path: Path | None, netlist_path: Path | None) -> dict[str, Any]:
    """Extract immutable run/file identity without treating filenames as provenance."""
    manifest_path = _safe_file(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ReviewInputError("cannot parse run manifest") from exc
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise ReviewInputError("run manifest has no verifiable file inventory")
    root = manifest_path.parent.resolve()
    entries = {str(item.get("path")): item for item in manifest["files"] if isinstance(item, Mapping) and isinstance(item.get("path"), str)}

    def file_entry(path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
        item = entries.get(relative)
        if not isinstance(item, Mapping):
            return None
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        expected_sha = item.get("sha256")
        return {"path": relative, "sha256": actual_sha, "manifest_sha256": expected_sha,
                "verified": actual_sha == expected_sha}

    op_entry = file_entry(op_path)
    netlist_entry = file_entry(netlist_path)
    content_sha = manifest.get("content_sha256")
    content_verified = bool(content_sha and content_sha == _digest({key: value for key, value in manifest.items() if key != "content_sha256"}))
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "manifest_content_sha256": content_sha,
        "manifest_content_verified": content_verified,
        "run_id": manifest.get("run_id"), "campaign_id": manifest.get("campaign_id"),
        "state_sha256": manifest.get("state_sha256"),
        "op": op_entry, "netlist": netlist_entry,
        "verified": bool(content_verified and op_entry and op_entry.get("verified") and (netlist_entry is None or netlist_entry.get("verified"))),
    }


def _operation_metadata(path: Path) -> dict[str, Any]:
    """Recover run identity and the manifest's netlist hash for an OP file."""
    metadata: dict[str, Any] = {}
    for ancestor in (path.parent, *path.parents):
        if not re.fullmatch(r"[0-9a-f]{32}", ancestor.name, re.I):
            continue
        result_file = ancestor / "result.json"
        if result_file.is_file():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = {}
            readback = payload.get("readback", {}) if isinstance(payload, Mapping) else {}
            runs = readback.get("runs", []) if isinstance(readback, Mapping) else []
            if isinstance(runs, list) and runs and isinstance(runs[0], Mapping):
                run = runs[0]
                netlists = run.get("netlists", [])
                metadata.update({"op_run_id": run.get("run_id"), "op_history": run.get("history"),
                                 "op_campaign_id": run.get("campaign_id"), "op_run_netlist_sha256": netlists[0].get("sha256") if isinstance(netlists, list) and netlists and isinstance(netlists[0], Mapping) else None,
                                 "op_state_sha256": run.get("selected_state_sha256") or readback.get("selected_state_sha256")})
        manifest = ancestor / "maestro" / "run" / "main" / "run-manifest.json"
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = {}
            metadata.setdefault("op_run_id", payload.get("run_id"))
            metadata.setdefault("op_history", payload.get("history"))
            metadata.setdefault("op_campaign_id", payload.get("campaign_id"))
            metadata.setdefault("op_state_sha256", payload.get("state_sha256"))
            files = payload.get("files", [])
            if isinstance(files, list):
                netlists = [item for item in files if isinstance(item, Mapping) and str(item.get("path", "")).endswith(".netlist")]
                if netlists:
                    metadata.setdefault("op_run_netlist_sha256", netlists[0].get("sha256"))
                try:
                    relative = path.resolve().relative_to(manifest.parent.resolve()).as_posix()
                    raw_entry = next((item for item in files if isinstance(item, Mapping) and item.get("path") == relative), None)
                    if raw_entry is not None:
                        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                        metadata["op_raw_sha256_verified"] = actual_sha == raw_entry.get("sha256")
                        metadata["op_manifest_raw_sha256"] = raw_entry.get("sha256")
                except (ValueError, OSError):
                    metadata["op_raw_sha256_verified"] = False
        if metadata:
            metadata["op_operation_id"] = ancestor.name
            return {key: value for key, value in metadata.items() if value is not None}
    return {}


def review_from_files(readback_path: str | Path, op_path: str | Path | None = None, intent_path: str | Path | None = None,
                      *, netlist_path: str | Path | None = None, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Load immutable JSON/OP evidence and optionally save JSON/Markdown output."""
    readback_file = _safe_file(readback_path)
    readback = _json_load(readback_file)
    intent = _json_load(intent_path) if intent_path is not None else None
    # This is the evidence-file hash, not the design content fingerprint.  A
    # caller may provide ``design_sha256`` separately; conflating the two
    # would reject every valid JSON wrapper before the netlist anchor is even
    # checked.
    source: dict[str, Any] = {"readback_path": str(readback_file), "readback_file_sha256": hashlib.sha256(readback_file.read_bytes()).hexdigest()}
    op: Any = None
    op_file: Path | None = None
    op_metadata: dict[str, Any] = {}
    if op_path is not None:
        candidate = Path(op_path).expanduser().absolute()
        if candidate.is_dir():
            candidates = sorted(candidate.rglob("*.op0"))
            if not candidates:
                raise ReviewInputError("operating-point directory contains no .op0 file")
            candidate = candidates[0]
        op_file = _safe_file(candidate)
        source.update({"op_path": str(op_file), "op_sha256": hashlib.sha256(op_file.read_bytes()).hexdigest()})
        op_metadata = _operation_metadata(op_file)
        source.update(op_metadata)
        if op_file.suffix.casefold() == ".json":
            parsed_op = _json_load(op_file)
            # A Harness `maestro.run` result JSON is an operation registry,
            # not the device table itself.  Resolve its immutable sibling
            # `.op0` when present; structured OP JSON remains supported.
            registry = parsed_op.get("readback", parsed_op) if isinstance(parsed_op, Mapping) else {}
            if isinstance(registry, Mapping) and isinstance(registry.get("runs"), list):
                discovered = _find_nearby(op_file, "**/*.op0")
                if discovered is not None:
                    op_file = _safe_file(discovered)
                    source.update({"op_path": str(op_file), "op_sha256": hashlib.sha256(op_file.read_bytes()).hexdigest()})
                    op_metadata = _operation_metadata(op_file)
                    source.update(op_metadata)
                    op = op_file
                else:
                    op = parsed_op
            else:
                op = parsed_op
        else:
            op = op_file
    chosen_netlist = Path(netlist_path).expanduser().absolute() if netlist_path is not None else _find_matching_netlist(op_file, op_metadata.get("op_run_netlist_sha256")) if op_file else None
    if chosen_netlist is not None and chosen_netlist.is_file():
        chosen_netlist = _safe_file(chosen_netlist)
        source.update({"netlist_path": str(chosen_netlist), "netlist_sha256": hashlib.sha256(chosen_netlist.read_bytes()).hexdigest()})
    if op_file is not None:
        op_manifest = _find_nearby(op_file, "run-manifest.json")
        net_manifest = _find_nearby(chosen_netlist, "run-manifest.json") if chosen_netlist is not None else op_manifest
        if op_manifest is not None and net_manifest is not None:
            same_manifest = op_manifest.resolve() == net_manifest.resolve()
            op_identity = _run_manifest_identity(op_manifest, op_file, chosen_netlist if same_manifest else None)
            net_identity = _run_manifest_identity(net_manifest, None, chosen_netlist)
            source["run_identity"] = {
                **op_identity,
                "same_manifest": same_manifest,
                "op": op_identity.get("op"),
                "netlist": op_identity.get("netlist") if same_manifest else net_identity.get("netlist"),
                "op_manifest_path": str(op_manifest),
                "netlist_manifest_path": str(net_manifest),
            }
        else:
            # Synthetic fixtures are explicitly marked and may use a plain
            # text netlist without a run bundle.  A real OP file without a
            # manifest is fail-closed: topology can still be reviewed, but
            # the electrical judgment must not consume it as current data.
            try:
                design_source = _unwrap_readback(readback).get("source", {})
            except ReviewInputError:
                design_source = {}
            if not (isinstance(design_source, Mapping) and design_source.get("kind") == "offline_synthetic_fixture"):
                source["run_identity"] = {"verified": False, "same_manifest": False, "reason": "run manifest is missing"}
    review = review_design(readback, op=op, intent=intent, source=source, netlist=chosen_netlist)
    if output_dir is not None:
        output = Path(output_dir).expanduser().absolute()
        output.mkdir(parents=True, exist_ok=True)
        if (output / "review.json").exists() or (output / "review.md").exists():
            raise ReviewInputError("review output already exists")
        (output / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (output / "review.md").write_text(render_review_markdown(review), encoding="utf-8")
        review["output"] = {"json": str(output / "review.json"), "markdown": str(output / "review.md")}
    return review


__all__ = ["ReviewInputError", "parse_operating_point", "review_design", "review_from_files", "render_review_markdown"]
