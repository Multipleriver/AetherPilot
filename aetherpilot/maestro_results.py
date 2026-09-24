"""Immutable local MDE run manifests, native tables and waveform selection."""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import re
from pathlib import Path

from .artifacts import atomic_json, file_digest, materialize_evidence_links
from .contract import digest, no_duplicate_keys
from .errors import OperationError, ValidationError
from .results import normalize_waveform, safe_result_path, validate_waveform_result


def parse_native_result_table(path):
    path = safe_result_path(path, max_bytes=16 * 1024 ** 2)
    payload = path.read_bytes()
    sections = []
    current = None
    for line, row in enumerate(csv.reader(io.StringIO(payload.decode("utf-8"))), 1):
        if not any(value.strip() for value in row):
            current = None
            continue
        if current is None:
            if len(row) < 2:
                raise ValidationError("Native result table header is incomplete", stage="results", code="result_parse")
            current = {"columns": row, "rows": []}
            sections.append(current)
        else:
            if len(row) != len(current["columns"]):
                raise ValidationError("Native result table row width differs", stage="results", code="result_parse", details={"line": line})
            current["rows"].append(row)
        if line > 100000:
            raise ValidationError("Native result table exceeds its budget", stage="results", code="resource_budget_exceeded")
    if not sections:
        raise ValidationError("Native result table is empty", stage="results", code="result_incomplete")
    return {"path": str(path), "sha256": file_digest(path), "sections": sections}


def _result_number(value):
    if value is None or not str(value).strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def native_output_records(table):
    """Extract native Detail-table output rows without inventing blank results."""
    if not isinstance(table, dict) or not isinstance(table.get("sections"), list):
        raise ValidationError("Native result table structure is invalid", stage="results", code="result_parse")
    records = []
    metadata_columns = {"Spec", "Weight", "Pass/Fail", "Min", "Max"}
    for section in table["sections"]:
        columns = section.get("columns") if isinstance(section, dict) else None
        rows = section.get("rows") if isinstance(section, dict) else None
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValidationError("Native result section is malformed", stage="results", code="result_parse")
        if not columns or columns[0] != "Output" or not metadata_columns <= set(columns):
            continue
        if len(set(columns)) != len(columns):
            raise ValidationError("Native output columns repeat", stage="results", code="ambiguous_result")
        names = set()
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns) or any(not isinstance(value, str) for value in row):
                raise ValidationError("Native output row is malformed", stage="results", code="result_parse")
            if not any(value.strip() for value in row):
                continue
            if not row[0].strip() or row[0] in names:
                raise ValidationError("Native output names are missing or repeated", stage="results", code="ambiguous_result")
            names.add(row[0])
            raw = {str(columns[index]): row[index] for index in range(len(columns))}
            corners = {column: raw[column] for column in columns[1:] if column not in metadata_columns}
            value = _result_number(next(iter(corners.values()))) if len(corners) == 1 else None
            records.append({
                "name": row[0], "corners": corners, "value": value,
                "spec": raw.get("Spec", ""), "weight": _result_number(raw.get("Weight")),
                "status": raw.get("Pass/Fail", ""), "min": _result_number(raw.get("Min")),
                "max": _result_number(raw.get("Max")), "raw": raw,
                "table_sha256": table.get("sha256"),
            })
    return records


def native_result_points(tables, *, test, expected_corners, expected_outputs):
    records = [record for table in tables for record in native_output_records(table)]
    by_name = {record["name"]: record for record in records}
    if len(by_name) != len(records) or set(by_name) != set(expected_outputs):
        raise ValidationError("Native Detail output set differs from the setup", stage="results", code="result_incomplete")
    parameters = {corner: {} for corner in expected_corners}
    parameter_sections = [section for table in tables for section in table["sections"]
                          if section["columns"] and section["columns"][0] == "Parameter"]
    if len(parameter_sections) != 1:
        raise ValidationError("Native Detail parameter section is missing or ambiguous", stage="results", code="result_incomplete")
    section = parameter_sections[0]
    corner_columns = {column: index for index, column in enumerate(section["columns"][1:], 1) if column.strip()}
    if set(corner_columns) != set(expected_corners):
        raise ValidationError("Native Detail corner set differs from the setup", stage="results", code="result_incomplete")
    for row in section["rows"]:
        for corner, index in corner_columns.items():
            if row[0] in parameters[corner]:
                raise ValidationError("Native Detail parameter names repeat", stage="results", code="ambiguous_result")
            parameters[corner][row[0]] = row[index]
    points = []
    for index, corner in enumerate(expected_corners, 1):
        outputs = {}
        for name, record in by_name.items():
            if set(record["corners"]) != set(expected_corners):
                raise ValidationError("Native output corner set differs from the setup", stage="results", code="result_incomplete")
            raw = record["corners"][corner]
            numeric = _result_number(raw)
            if raw.strip() and numeric is None:
                raise ValidationError("Native output evaluation failed or is not scalar", stage="results", code="output_evaluation_failed",
                                      details={"output": name, "corner": corner, "native_value": raw})
            outputs[name] = {"test": test, "value": raw, "numeric_value": numeric, "spec": record["spec"],
                             "weight": record["raw"].get("Weight", ""), "pass_fail": "",
                             "native_aggregate_status": record["status"], "table_sha256": record["table_sha256"]}
        points.append({"point": index, "test": test, "corner": corner, "parameters": parameters[corner], "outputs": outputs})
    return points


def verify_simulator_log(path):
    path = safe_result_path(path, max_bytes=32 * 1024 ** 2)
    text = path.read_text(encoding="utf-8", errors="replace")
    completions = re.findall(r"Simulation completes with (\d+) error\(s\), (\d+) warning\(s\)", text)
    if len(completions) != 1 or int(completions[0][0]) or re.search(r"(?im)^\s*(?:fatal|error)\s*[:(]", text):
        raise OperationError("Native simulator log does not prove successful completion", stage="results", code="simulation_failed")
    return {"path": str(path), "sha256": file_digest(path), "errors": 0, "warnings": int(completions[0][1])}


def save_run_manifest(directory, payload):
    root = Path(directory).resolve()
    materialize_evidence_links(root)
    manifest = copy.deepcopy(payload)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValidationError("Run evidence contains a symlink", stage="results", code="unsafe_result_path")
        if path.is_file():
            files.append({"path": path.relative_to(root).as_posix(), "sha256": file_digest(path), "bytes": path.stat().st_size})
    manifest["files"] = files
    manifest["schema_version"] = 1
    manifest["content_sha256"] = digest(manifest)
    destination = root / "run-manifest.json"
    if destination.exists():
        raise ValidationError("Run manifest already exists", stage="results", code="target_exists")
    atomic_json(destination, manifest)
    entry = {"run_id": manifest["run_id"], "history": manifest["history"], "test": manifest["test"],
             "manifest": str(destination), "manifest_sha256": file_digest(destination), "state_sha256": manifest["state_sha256"]}
    if manifest.get("campaign_id"):
        entry["campaign_id"] = manifest["campaign_id"]
    return entry


def load_run_manifest(entry):
    if not isinstance(entry, dict):
        raise ValidationError("Run registry entry is malformed", stage="results", code="result_provenance")
    path = safe_result_path(entry.get("manifest", ""), max_bytes=32 * 1024 ** 2)
    if file_digest(path) != entry.get("manifest_sha256"):
        raise ValidationError("Run manifest fingerprint changed", stage="results", code="result_provenance")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (ValueError, TypeError) as exc:
        raise ValidationError("Run manifest cannot be read", stage="results", code="result_provenance") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("files"), list):
        raise ValidationError("Run manifest schema is invalid", stage="results", code="result_provenance")
    if value.get("content_sha256") != digest({key: item for key, item in value.items() if key != "content_sha256"}):
        raise ValidationError("Run manifest content changed", stage="results", code="result_provenance")
    for field in ("run_id", "history", "test", "state_sha256", "campaign_id"):
        if entry.get(field) != value.get(field):
            raise ValidationError("Run registry identity differs from the manifest", stage="results", code="result_provenance", details={"field": field})
    seen = set()
    for item in value["files"]:
        relative = item.get("path") if isinstance(item, dict) else None
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts or relative in seen:
            raise ValidationError("Run evidence path is unsafe", stage="results", code="result_provenance")
        seen.add(relative)
        evidence = safe_result_path(path.parent / relative)
        if file_digest(evidence) != item.get("sha256") or evidence.stat().st_size != item.get("bytes"):
            raise ValidationError("Run evidence changed", stage="results", code="result_provenance", details={"file": relative})
    return value


def select_runs(state, *, history="", test=""):
    candidates = [entry for entry in state["results"] if (not history or history in {entry.get("run_id"), entry.get("history")}) and (not test or entry.get("test") == test)]
    if not candidates:
        raise OperationError("No matching successful native run exists", stage="results", code="result_missing")
    campaign = candidates[-1].get("campaign_id") or candidates[-1]["run_id"]
    if history and any((entry.get("campaign_id") or entry["run_id"]) != campaign for entry in candidates):
        raise ValidationError("Native history name is ambiguous; use run_id", stage="results", code="ambiguous_result")
    candidates = [entry for entry in candidates if (entry.get("campaign_id") or entry["run_id"]) == campaign]
    if len({entry["test"] for entry in candidates}) != len(candidates):
        raise ValidationError("Native campaign contains repeated tests", stage="results", code="ambiguous_result")
    return [load_run_manifest(entry) for entry in candidates]


def select_run(state, *, history="", test=""):
    runs = select_runs(state, history=history, test=test)
    if len(runs) != 1:
        raise ValidationError("Select a test for this multi-test history", stage="results", code="ambiguous_result")
    return runs[0]


def select_waveform(run, *, analysis, expression, corner=""):
    candidates = [item for item in run["waveforms"] if item["analysis"] == analysis and (not corner or item["corner"] == corner)]
    if len(candidates) != 1:
        raise ValidationError("Waveform selection must identify exactly one corner and analysis", stage="results", code="ambiguous_result")
    entry = candidates[0]
    bundle = json.loads(safe_result_path(entry["path"]).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    waveform = bundle["waveform"]
    if validate_waveform_result(waveform):
        raise ValidationError("Stored waveform failed integrity checks", stage="results", code="result_provenance")
    from .maestro_specs import resolve_signal_name

    name = resolve_signal_name(bundle, expression)
    complex_components = bundle["complex_signals"].get(name)
    wanted = [complex_components["real"], complex_components["imag"]] if complex_components else [name]
    signals = {item["name"]: item for item in waveform["signals"]}
    if not set(wanted) <= signals.keys():
        raise ValidationError("Requested waveform is absent from the native run", stage="results", code="signal_missing")
    rows = [[point, *[signals[column]["values"][index] for column in wanted]] for index, point in enumerate(waveform["axis"]["values"])]
    result = normalize_waveform(rows, wanted, analysis=analysis, source={**waveform["source"], "expression": expression},
                               units={column: signals[column]["unit"] for column in wanted}, fresh=True)
    return {"waveform": result, "conversion": bundle["conversion"], "complex_signals": {name: complex_components} if complex_components else {}}
