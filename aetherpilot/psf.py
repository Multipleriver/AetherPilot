"""Strict, bounded PSF ASCII waveforms and local vendor binary conversion."""

from __future__ import annotations

import math
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .artifacts import file_digest
from .errors import OperationError, PreflightError, ValidationError
from .results import normalize_waveform, safe_result_path


def _invalid(message):
    return ValidationError(message, stage="results", code="result_parse")


def _tokens(line):
    try:
        return shlex.split(line, posix=True)
    except ValueError as exc:
        raise _invalid("Malformed PSF string") from exc


def _numeric(tokens):
    text = " ".join(tokens).strip()
    complex_value = text.startswith("(") and text.endswith(")")
    text = text[1:-1] if complex_value else text
    try:
        values = [float(value) for value in text.replace(",", " ").split()]
    except ValueError as exc:
        raise _invalid("PSF contains non-numeric data") from exc
    if len(values) != (2 if complex_value else 1) or not all(math.isfinite(value) for value in values):
        raise _invalid("PSF value has an invalid shape or is non-finite")
    return complex(*values) if complex_value else values[0]


def parse_psf_ascii(path, *, analysis, source=None, fresh=False, max_points=100001, max_values=2000000):
    path = safe_result_path(path, max_bytes=128 * 1024 ** 2)
    sections = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line in {"HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"}:
            if line in sections or current == "END":
                raise _invalid("PSF sections are duplicated or follow END")
            current = line
            sections[current] = []
        elif current is None:
            raise _invalid("PSF does not start with a section")
        else:
            sections[current].append(line)
    if not {"HEADER", "TYPE", "TRACE", "VALUE", "END"} <= sections.keys() or sections["END"]:
        raise _invalid("PSF is incomplete")
    headers = {}
    for line in sections["HEADER"]:
        parts = _tokens(line)
        if len(parts) == 2:
            if parts[0] in headers:
                raise _invalid("PSF header is duplicated")
            headers[parts[0]] = parts[1]
    native_analysis = headers.get("analysis type", analysis).lower()
    if native_analysis != analysis.lower():
        raise _invalid("PSF analysis differs from the requested analysis")
    type_units = {}
    native_type = None
    for line in sections["TYPE"]:
        parts = _tokens(line)
        if len(parts) >= 3 and parts[1] in {"FLOAT", "COMPLEX", "INT", "STRUCT"}:
            native_type = parts[0]
            type_units[native_type] = ""
        elif len(parts) == 2 and parts[0] == "units" and native_type is not None:
            type_units[native_type] = parts[1]
    sweep_lines = [line for line in sections.get("SWEEP", []) if "PROP(" in line]
    if len(sweep_lines) != 1:
        raise _invalid("PSF waveform needs exactly one sweep axis")
    sweep_name = _tokens(sweep_lines[0])[0]
    traces = []
    groups = {}
    pending_group = None
    remaining = 0
    for line in sections["TRACE"]:
        parts = _tokens(line)
        if len(parts) == 3 and parts[1] == "GROUP":
            if remaining or parts[0] in groups:
                raise _invalid("PSF trace group is ambiguous")
            try:
                remaining = int(parts[2])
            except ValueError as exc:
                raise _invalid("PSF trace group count is invalid") from exc
            if not 1 <= remaining <= 4096:
                raise _invalid("PSF trace group exceeds its budget")
            pending_group = parts[0]
            groups[pending_group] = []
        elif len(parts) == 2 and parts[1] in type_units:
            name = parts[0]
            if not name or any(item[0] == name for item in traces):
                raise _invalid("PSF trace names must be unique")
            traces.append((name, parts[1]))
            if remaining:
                groups[pending_group].append(name)
                remaining -= 1
        else:
            raise _invalid("Unsupported PSF trace declaration")
    if remaining or not traces or len(traces) > 4096:
        raise _invalid("PSF trace declaration is incomplete or too large")
    rows = []
    axis_value = None
    values = {}
    queue = []
    trace_names = {item[0] for item in traces}
    for line in sections["VALUE"]:
        parts = _tokens(line)
        if line.startswith('"') and parts and parts[0] == sweep_name:
            if axis_value is not None:
                if queue or set(values) != trace_names:
                    raise _invalid("PSF row is incomplete")
                rows.append([axis_value, *[values[name] for name, _ in traces]])
            axis_value = _numeric(parts[1:])
            if isinstance(axis_value, complex):
                raise _invalid("PSF axis must be real")
            values = {}
            if len(rows) >= max_points or (len(rows) + 1) * len(traces) > max_values:
                raise _invalid("PSF exceeds the waveform resource budget")
            continue
        if axis_value is None:
            raise _invalid("PSF values precede the sweep axis")
        if line.startswith('"'):
            if not parts or queue:
                raise _invalid("PSF group is incomplete")
            name = parts.pop(0)
            queue = list(groups[name]) if name in groups else [name]
        if not queue or queue[0] not in trace_names or queue[0] in values:
            raise _invalid("PSF has unexpected or duplicate trace values")
        if not parts:
            continue
        name = queue.pop(0)
        values[name] = _numeric(parts)
    if axis_value is None or queue or set(values) != trace_names:
        raise _invalid("PSF has no complete final waveform row")
    rows.append([axis_value, *[values[name] for name, _ in traces]])
    columns, units, complex_signals, normalized = [], {}, {}, []
    complex_columns = {index for index in range(len(traces)) if any(isinstance(row[index + 1], complex) for row in rows)}
    for index, (name, native_type) in enumerate(traces):
        names = [name + ".real", name + ".imag"] if index in complex_columns else [name]
        if index in complex_columns:
            complex_signals[name] = names
        columns.extend(names)
        units.update({column: type_units[native_type] for column in names})
    for row in rows:
        converted = [row[0]]
        for index, value in enumerate(row[1:]):
            converted.extend([complex(value).real, complex(value).imag] if index in complex_columns else [value])
        normalized.append(converted)
    provenance = {**(source or {}), "psf_ascii_sha256": file_digest(path)}
    waveform = normalize_waveform(normalized, columns, analysis=analysis, source=provenance, units=units, fresh=fresh)
    return {"waveform": waveform, "headers": headers, "complex_signals": complex_signals, "native_axis": sweep_name}


def convert_psf_binary(source, destination, *, timeout=20):
    source = safe_result_path(source)
    destination = safe_result_path(destination, must_exist=False)
    if destination.exists():
        raise ValidationError("PSF conversion destination exists", stage="results", code="target_exists")
    executable = shutil.which("Psf2PsfAscii")
    if executable is None:
        root = os.environ.get("IWAVE_ROOT")
        candidate = Path(root) / "bin/Psf2PsfAscii" if root else None
        if candidate and candidate.is_file():
            executable = str(candidate)
    if executable is None:
        raise PreflightError("Native PSF conversion tool is unavailable", stage="results", code="native_tool_missing")
    with Path(executable).open("rb") as handle:
        script = handle.read(2) == b"#!"
    command = (["/bin/bash", executable] if script else [executable]) + ["-i", str(source), "-o", str(destination)]
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = file_digest(source)
    log_path = destination.with_suffix(destination.suffix + ".log")
    with log_path.open("xb") as log:
        try:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OperationError("PSF conversion exceeded its deadline", stage="results", code="timeout") from exc
    if process.returncode or not destination.is_file() or not destination.stat().st_size:
        raise OperationError("Native PSF conversion failed", stage="results", code="native_result_conversion_failed")
    if before != file_digest(source):
        raise ValidationError("PSF source changed during conversion", stage="results", code="result_provenance")
    return {"source_sha256": before, "ascii_sha256": file_digest(destination), "log_sha256": file_digest(log_path)}


def read_native_waveform(source, directory, *, analysis, provenance, timeout=20):
    source = safe_result_path(source)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    executable = shutil.which("iWaveExportCurveTool")
    if executable is None:
        raise PreflightError("Native waveform export tool is unavailable", stage="results", code="native_tool_missing")
    destination = directory / "waveform.txt"
    fingerprint = file_digest(source)
    with Path(executable).open("rb") as handle:
        script = handle.read(2) == b"#!"
    command = (["/bin/bash", executable] if script else [executable]) + [str(source), "-o", str(destination), "-filter", ".*", "-format", "text", "-table", "0", "-threadNum", "1"]
    with (directory / "export.log").open("xb") as log:
        try:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OperationError("Native waveform export timed out", stage="results", code="timeout") from exc
    if process.returncode or not destination.is_file():
        raise OperationError("Native waveform export failed", stage="results", code="native_result_conversion_failed")
    if file_digest(source) != fingerprint:
        raise ValidationError("Native waveform changed during export", stage="results", code="result_provenance")
    rows, columns, complex_signals = [], [], {}
    with safe_result_path(destination, max_bytes=128 * 1024 ** 2).open(encoding="utf-8") as handle:
        header = handle.readline().split()
        if not header or header[0] != "XVAL" or len(header) < 2 or len(set(header)) != len(header) or len(header) > 4097:
            raise _invalid("Native waveform text has an invalid header")
        for name in header[1:]:
            if name.startswith(("REAL:", "IMAG:")):
                component, signal = name.split(":", 1)
                column = signal + (".real" if component == "REAL" else ".imag")
                complex_signals.setdefault(signal, {})[component.lower()] = column
            else:
                column = name
            columns.append(column)
        if any(set(value) != {"real", "imag"} for value in complex_signals.values()):
            raise _invalid("Complex waveform is missing a component")
        for line in handle:
            if not line.strip():
                continue
            try:
                row = [float(value) for value in line.split()]
            except ValueError as exc:
                raise _invalid("Native waveform contains non-numeric values") from exc
            rows.append(row)
            if len(rows) > 100001 or len(rows) * len(columns) > 2000000:
                raise _invalid("Native waveform exceeds the resource budget")
    waveform = normalize_waveform(rows, columns, analysis=analysis,
                                  source={**provenance, "native_result_sha256": fingerprint, "text_sha256": file_digest(destination),
                                          "exporter": "iWaveExportCurveTool", "export_significant_digits": 4}, fresh=True)
    return {"waveform": waveform, "complex_signals": complex_signals, "raw_path": str(source),
            "raw_sha256": fingerprint, "raw_result_authoritative": True,
            "conversion": {"format": "iWave_text", "significant_digits": 4, "lossless": False}}
