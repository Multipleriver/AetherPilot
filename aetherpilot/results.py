"""Backend-neutral waveform result normalization and freshness checks."""

from __future__ import annotations

import csv
import copy
import hashlib
import io
import math
import os
from pathlib import Path

from .core.contract import digest
from .errors import ValidationError


_AXES = {"op": ("operating_point", ""), "dc": ("dc", ""), "tran": ("time", "s"),
         "ac": ("frequency", "Hz"), "noise": ("frequency", "Hz"), "pss": ("time", "s"),
         "pnoise": ("frequency", "Hz"), "pac": ("frequency", "Hz"), "stb": ("frequency", "Hz"),
         "xf": ("frequency", "Hz")}


def _finite(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{where} must be finite", stage="results", code="result_parse")
    return float(value)


def normalize_waveform(rows, columns, *, analysis, source=None, units=None, fresh=False):
    """Normalize rectangular numeric rows into a result schema shared by backends."""
    kind = str(analysis).casefold()
    if kind not in _AXES:
        raise ValidationError("Unsupported waveform analysis", stage="results", code="result_parse")
    if not isinstance(columns, list) or not columns or any(not isinstance(item, str) or not item for item in columns) or len(set(columns)) != len(columns):
        raise ValidationError("Waveform columns must be unique names", stage="results", code="result_parse")
    if type(fresh) is not bool or (fresh and not isinstance(source, dict)) or (source is not None and not isinstance(source, dict)):
        raise ValidationError("Waveform freshness requires source provenance", stage="results", code="result_provenance")
    if units is not None and (not isinstance(units, dict) or any(not isinstance(value, str) for value in units.values())):
        raise ValidationError("Waveform units must be strings", stage="results", code="result_parse")
    if not isinstance(rows, list) or not rows:
        raise ValidationError("Waveform must contain at least one row", stage="results", code="result_incomplete")
    normalized = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != len(columns) + 1:
            raise ValidationError("Waveform row width is inconsistent", stage="results", code="result_parse")
        normalized.append([_finite(value, "waveform value") for value in row])
    axis = [row[0] for row in normalized]
    if kind != "op" and any(right <= left for left, right in zip(axis, axis[1:])):
        raise ValidationError("Waveform axis must be strictly increasing", stage="results", code="result_provenance")
    axis_name, axis_unit = _AXES[kind]
    signal_units = units or {}
    signals = [{"name": name, "unit": signal_units.get(name, ""), "values": [row[index + 1] for row in normalized]}
               for index, name in enumerate(columns)]
    result = {"schema_version": 1, "analysis": kind, "axis": {"name": axis_name, "unit": axis_unit, "values": axis},
              "signals": signals, "row_count": len(normalized), "source": copy.deepcopy(source or {}), "fresh": fresh}
    result["content_sha256"] = digest({key: value for key, value in result.items() if key != "content_sha256"})
    return result


def safe_result_path(path, *, must_exist=True, max_bytes=256 * 1024 ** 2):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValidationError("Result path contains a symlink", stage="results", code="unsafe_result_path")
    if must_exist and (not path.is_file() or path.stat().st_size > max_bytes):
        raise ValidationError("Waveform CSV is missing or unsafe", stage="results", code="result_missing")
    return path.resolve()


def safe_input_path(path, *, must_exist=True, max_bytes=256 * 1024 ** 2):
    """Validate an input file without imposing a waveform-file extension.

    Native simulator model libraries, netlists, and include files are inputs,
    not waveform CSV results.  They still receive the same symlink, size, and
    regular-file protections as result paths, but must be allowed to use their
    native extensions (for example ``.lib`` or ``.mdl``).
    """
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValidationError("Input path contains a symlink", stage="netlist", code="unsafe_input_path")
    if must_exist and (not path.is_file() or path.stat().st_size > max_bytes):
        raise ValidationError("Input file is missing or unsafe", stage="netlist", code="input_missing")
    return path.resolve()


def read_waveform_csv(path, *, analysis, source=None, units=None):
    path = safe_result_path(path)
    payload = path.read_bytes()
    with io.StringIO(payload.decode("utf-8"), newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header or len(header) < 2:
            raise ValidationError("Waveform CSV header is incomplete", stage="results", code="result_parse")
        rows = []
        for row in reader:
            try:
                rows.append([float(value) for value in row])
            except ValueError as exc:
                raise ValidationError("Waveform CSV contains non-numeric data", stage="results", code="result_parse") from exc
    fingerprint = hashlib.sha256(payload).hexdigest()
    value = normalize_waveform(rows, header[1:], analysis=analysis, source={**(source or {}), "path": str(path), "sha256": fingerprint}, units=units)
    value["file_sha256"] = fingerprint
    return value


def write_waveform_csv(result, path):
    errors = validate_waveform_result(result)
    if errors:
        raise ValidationError("Only fresh normalized results can be exported", stage="results", code="result_provenance")
    destination = safe_result_path(path, must_exist=False)
    if destination.exists():
        raise ValidationError("Waveform destination already exists", stage="results", code="target_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    axis = result["axis"]["values"]
    signals = result["signals"]
    try:
        with destination.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([result["axis"]["name"], *[item["name"] for item in signals]])
            for index, point in enumerate(axis):
                writer.writerow([point, *[item["values"][index] for item in signals]])
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValidationError("Waveform destination already exists", stage="results", code="target_exists") from exc
    return {"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "row_count": len(axis)}


def validate_waveform_result(result, *, expected_analysis=None, expected_source=None):
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result.get("schema_version") != 1 or result.get("fresh") is not True:
        return ["result is not a fresh schema-v1 waveform"]
    errors = []
    if expected_analysis is not None and result.get("analysis") != expected_analysis:
        errors.append("analysis differs from the requested analysis")
    if expected_source is not None and result.get("source") != expected_source:
        errors.append("source provenance differs from the requested run")
    axis = result.get("axis", {})
    values = axis.get("values", []) if isinstance(axis, dict) else []
    if not isinstance(values, list) or not values:
        errors.append("axis is empty")
        values = []
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        errors.append("axis contains non-finite values")
    elif result.get("analysis") != "op" and any(right <= left for left, right in zip(values, values[1:])):
        errors.append("axis is not strictly increasing")
    if result.get("analysis") not in _AXES or type(result.get("row_count")) is not int or result.get("row_count") != len(values):
        errors.append("analysis or row count is invalid")
    if not isinstance(result.get("source"), dict) or not result["source"]:
        errors.append("source provenance is missing")
    signals = result.get("signals", [])
    names = set()
    if not isinstance(signals, list) or not signals:
        errors.append("signals are missing")
        signals = []
    for item in signals:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            errors.append("signal is malformed")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name or name in names or not isinstance(item.get("unit"), str):
            errors.append("signal names or units are invalid")
        else:
            names.add(name)
        if len(item["values"]) != len(values):
            errors.append("signal lengths do not match axis")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in item["values"]):
            errors.append("signal contains non-finite values")
    try:
        expected_hash = digest({key: value for key, value in result.items() if key not in {"content_sha256", "file_sha256"}})
        if result.get("content_sha256") != expected_hash:
            errors.append("content hash does not match waveform content")
    except (ValueError, TypeError, OverflowError):
        errors.append("waveform content is not JSON-safe")
    return errors


__all__ = ["normalize_waveform", "read_waveform_csv", "validate_waveform_result", "write_waveform_csv"]
