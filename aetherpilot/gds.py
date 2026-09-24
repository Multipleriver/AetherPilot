"""Fresh, bounded native GDS export with structural and atomic-file gates."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .artifacts import file_digest
from .errors import ArtifactError, OperationError, ValidationError


@contextmanager
def gds_tool_environment(stage):
    executable = shutil.which("asOut")
    evidence = {"asout": executable, "shell_compatibility_wrapper": False}
    if executable is None:
        yield evidence
        return
    try:
        with Path(executable).open("rb") as handle:
            source = handle.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        yield evidence
        return
    if not source.startswith("#!/bin/sh") or "function " not in source:
        yield evidence
        return
    shim = stage / "tool-shim"
    shim.mkdir(mode=0o700)
    launcher = shim / "asOut"
    launcher.write_text("#!/bin/sh\nexec /bin/bash %s \"$@\"\n" % shlex.quote(executable), encoding="utf-8")
    launcher.chmod(0o700)
    prior = os.environ.get("PATH")
    os.environ["PATH"] = str(shim) + os.pathsep + (prior if prior is not None else os.defpath)
    evidence["shell_compatibility_wrapper"] = True
    try:
        yield evidence
    finally:
        if prior is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = prior


def parse_export_log(text, *, max_characters=8 * 1024 * 1024):
    if not isinstance(text, str) or not text.strip() or len(text) > max_characters:
        raise ValidationError("Export log is empty or exceeds its budget", stage="gds", code="invalid_export_log")
    errors = []
    warnings = []
    summaries = {"errors": 0, "warnings": 0}
    for line in text.splitlines():
        for summary in re.finditer(r"\b(errors?|warnings?)\s*[:=]\s*(\d+)\b|\b(\d+)\s+(errors?|warnings?)\b", line, re.I):
            kind = (summary[1] or summary[4]).lower().rstrip("s") + "s"
            count = int(summary[2] or summary[3])
            summaries[kind] = max(summaries[kind], count)
        remainder = re.sub(r"\b(?:errors?|warnings?)\s*[:=]\s*0\b|\b0\s+(?:errors?|warnings?)\b", "", line, flags=re.I)
        if re.search(r"(?:^|\s|\[|\*)(?:ERROR|FATAL)(?:\b|:)|\b(?:translation|export)\s+(?:has\s+)?failed\b", remainder, re.I):
            if len(errors) < 100:
                errors.append(line[:2000])
        elif re.search(r"\bWARN(?:ING)?\b", remainder, re.I) and len(warnings) < 100:
            warnings.append(line[:2000])
    return {"errors": errors, "warnings": warnings, "error_count": max(summaries["errors"], len(errors)),
            "warning_count": max(summaries["warnings"], len(warnings)), "nonempty": True}


def inspect_gds(path, *, top_cell=None, max_bytes=512 * 1024 ** 2, max_records=10_000_000):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 6 <= path.stat().st_size <= max_bytes:
        raise ValidationError("GDS output is missing, unsafe, empty, or exceeds its budget", stage="gds", code="invalid_gds")
    structures = set()
    references = set()
    active = None
    in_library = False
    in_structure = False
    in_element = False
    ended = False
    records = 0
    elements = 0
    units_seen = False

    def fail(reason):
        raise ValidationError("GDS structure validation failed", stage="gds", code="invalid_gds", details={"reason": reason, "record": records})

    with path.open("rb") as handle:
        while True:
            header = handle.read(4)
            if not header:
                break
            records += 1
            if records > max_records or len(header) != 4:
                fail("record limit or truncated header")
            length, kind, data_type = struct.unpack(">HBB", header)
            if length < 4 or length % 2 or data_type > 6:
                fail("invalid record length or data type")
            data = handle.read(length - 4)
            if len(data) != length - 4:
                fail("truncated record")
            if data_type in (1, 2) and len(data) % 2 or data_type in (3, 4) and len(data) % 4 or data_type == 5 and len(data) % 8 or data_type == 0 and data:
                fail("invalid typed payload")
            if records == 1 and (kind != 0 or data_type != 2 or length != 6):
                fail("missing HEADER")
            if kind == 1:
                if in_library or length != 28 or data_type != 2:
                    fail("invalid BGNLIB")
                in_library = True
            elif kind == 3:
                if not in_library or in_structure or data_type != 5 or length != 20:
                    fail("invalid UNITS")
                units_seen = True
            elif kind == 5:
                if not in_library or in_structure or length != 28 or data_type != 2:
                    fail("invalid BGNSTR")
                in_structure = True
                active = None
            elif kind in (6, 18):
                if not in_structure or data_type != 6:
                    fail("invalid structure name")
                try:
                    name = data.rstrip(b"\0").decode("ascii")
                except UnicodeDecodeError:
                    fail("non-ASCII structure name")
                if not name or "\0" in name:
                    fail("empty structure name")
                if kind == 6:
                    if active is not None or name in structures:
                        fail("duplicate structure name")
                    active = name
                    structures.add(name)
                else:
                    if not in_element:
                        fail("reference outside element")
                    references.add(name)
            elif kind in (8, 9, 10, 11, 12, 21, 45):
                if not in_structure or active is None or in_element:
                    fail("invalid element nesting")
                in_element = True
                elements += 1
            elif kind == 17:
                if not in_element or length != 4:
                    fail("invalid ENDEL")
                in_element = False
            elif kind == 7:
                if not in_structure or active is None or in_element or length != 4:
                    fail("invalid ENDSTR")
                in_structure = False
            elif kind == 4:
                if not in_library or in_structure or in_element or length != 4 or not structures or not units_seen:
                    fail("invalid ENDLIB")
                ended = True
                while True:
                    padding = handle.read(65536)
                    if not padding:
                        break
                    if any(padding):
                        fail("non-padding bytes after ENDLIB")
                break
    if not ended or references - structures or top_cell is not None and top_cell not in structures:
        fail("missing ENDLIB, top structure, or referenced structure")
    return {"sha256": file_digest(path), "size_bytes": path.stat().st_size, "structures": sorted(structures),
            "references": sorted(references), "records": records, "elements": elements, "structurally_valid": True,
            "scope": "stream framing and structure closure; not DRC/LVS"}


def export_gds(backend, target, output, layer_map, artifacts, *, overwrite=False, max_bytes=512 * 1024 ** 2):
    if type(overwrite) is not bool or type(max_bytes) is not int or not 1024 <= max_bytes <= 4 * 1024 ** 3:
        raise ValidationError("Invalid export limits", stage="plan", code="invalid_export_request")
    output = Path(output).expanduser().absolute()
    layer_map = Path(layer_map).expanduser().resolve()
    if not layer_map.is_file() or not output.parent.is_dir() or output.is_symlink() or output.resolve() == layer_map:
        raise ValidationError("Invalid export destination or layer map", stage="plan", code="invalid_export_request")
    if output.exists() and (not overwrite or not output.is_file()):
        raise OperationError("GDS destination already exists", stage="gds", code="target_exists")
    original = file_digest(output) if output.exists() else None
    baseline = backend.read(target)
    database_file = baseline.get("source", {}).get("file")
    if baseline.get("source", {}).get("modified") is not False or not baseline.get("database_sha256"):
        raise OperationError("GDS export requires a saved, stable view", stage="gds", code="unsaved_baseline")
    if database_file and output.resolve() == Path(database_file).resolve():
        raise ValidationError("GDS output cannot replace its source database", stage="plan", code="invalid_export_request")
    stage = artifacts.path / "gds-export"
    stage.mkdir(mode=0o700)
    stream = stage / "export.gds"
    log = stage / "export.log"
    map_copy = stage / "layers.map"
    shutil.copyfile(layer_map, map_copy)
    map_hash = file_digest(map_copy)
    method = getattr(backend.api, "dbGdsOut", None)
    if not callable(method):
        raise OperationError("Native GDS export API is unavailable", stage="gds", code="api_missing")
    with gds_tool_environment(stage) as runtime:
        artifacts.write("gds-runtime.json", runtime)
        returned = method(lib=target["library"], cell=target["cell"], view=target["view"], techLib=baseline["technology"],
                          gdsfile=str(stream), layerMap=str(map_copy), logFile=str(log), fromMemory=False,
                          noExportRefLibCells=False, outputEvalFailedPCell=False, outputUnboundInst=False,
                          outputUnboundVia=False, outputUnmappedLPP=False)
    if not returned:
        raise OperationError("Native GDS exporter returned failure", stage="gds", code="native_false")
    if log.is_symlink() or not log.is_file() or log.stat().st_size > 8 * 1024 ** 2:
        raise ValidationError("Native export log is missing or exceeds its budget", stage="gds", code="invalid_export_log")
    parsed = parse_export_log(log.read_text(errors="replace"))
    artifacts.write("gds-log-summary.json", parsed)
    if parsed["error_count"]:
        raise ValidationError("Native export log contains errors", stage="gds", code="export_log_errors")
    evidence = inspect_gds(stream, top_cell=target["cell"], max_bytes=max_bytes)
    after = backend.read(target)
    if after.get("database_sha256") != baseline["database_sha256"] or after.get("source", {}).get("modified") is not False or file_digest(map_copy) != map_hash:
        raise OperationError("Source or layer map changed during export", stage="gds", code="external_changes")
    descriptor, temporary = tempfile.mkstemp(prefix=".aetherpilot-gds-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle, stream.open("rb") as source:
            shutil.copyfileobj(source, handle, 1024 * 1024)
            handle.flush()
            os.fsync(handle.fileno())
        if file_digest(temporary) != evidence["sha256"]:
            raise ArtifactError("GDS staging copy changed", stage="gds", code="export_copy_mismatch")
        if original is not None:
            if not output.is_file() or output.is_symlink() or file_digest(output) != original:
                raise OperationError("GDS destination changed before publication", stage="gds", code="external_changes")
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError as exc:
                raise OperationError("GDS destination appeared before publication", stage="gds", code="external_changes") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
    evidence.update(output=str(output), log="gds-export/export.log", log_sha256=file_digest(log), layer_map_sha256=map_hash,
                    source_database_sha256=baseline["database_sha256"], warning_count=parsed["warning_count"], native_returned=True,
                    warnings=parsed["warnings"],
                    shell_compatibility_wrapper=runtime["shell_compatibility_wrapper"])
    artifacts.write("gds-export.json", evidence)
    return evidence
