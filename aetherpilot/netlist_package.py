"""Bounded, relocatable native netlist include closure and atomic publication."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from .artifacts import atomic_json, atomic_text, file_digest
from .errors import OperationError, ValidationError
from .maestro_migration import publish_directory
from .results import safe_result_path


_INCLUDE = re.compile(r'''^\s*(?P<kind>include|ahdl_include|\.include|\.inc|\.lib)\s+(?P<path>"[^"\r\n]+"|'[^'\r\n]+'|[^\s]+)(?P<tail>.*)$''', re.I)
_SEARCH = re.compile(r'''^(\s*\.option\s+search\s*=\s*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s]+)''', re.I)


def include_reference(line):
    match = _INCLUDE.match(line)
    if not match:
        return None
    token = match["path"]
    tail = match["tail"].strip()
    if match["kind"].casefold() == ".lib" and (not tail or tail.startswith(("$", ";", "//", "*"))):
        return None
    if token[0] in "\"'":
        token = token[1:-1]
    if any(character in token for character in "\x00\r\n$`{}"):
        raise ValidationError("Netlist includes require literal paths", stage="netlist", code="unsafe_include")
    return token, match.span("path")


def package_native_netlist(entry, destination, *, allowed_roots, max_bytes=256 * 1024 ** 2, max_files=4096, max_depth=32):
    entry = safe_result_path(entry, max_bytes=max_bytes)
    entry_name = "input.sp" if entry.suffix == ".sp" else "input.scs"
    destination = safe_result_path(destination, must_exist=False)
    roots = [Path(root).resolve() for root in allowed_roots]
    if destination.exists():
        raise ValidationError("Netlist export requires a new destination", stage="netlist", code="target_exists")
    if any(destination == root or destination in root.parents for root in roots):
        raise ValidationError("Netlist export overlaps an input root", stage="netlist", code="unsafe_export_path")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".netlist-export-", dir=destination.parent))
    files, active, total = {}, set(), 0

    def capture(path, depth):
        nonlocal total
        path = safe_result_path(path, max_bytes=max_bytes)
        if not any(path == root or root in path.parents for root in roots):
            raise ValidationError("Native include escapes approved source roots", stage="netlist", code="unsafe_include", details={"path": str(path)})
        if path in active:
            raise ValidationError("Native include closure contains a cycle", stage="netlist", code="include_cycle")
        if path in files:
            return files[path]["path"]
        if depth > max_depth or len(files) >= max_files or total + path.stat().st_size > max_bytes:
            raise ValidationError("Netlist dependency closure exceeds the local budget", stage="netlist", code="resource_budget_exceeded")
        original = path.read_bytes()
        total += len(original)
        if total > max_bytes:
            raise ValidationError("Netlist dependency grew beyond the local budget", stage="netlist", code="resource_budget_exceeded")
        try:
            text = original.decode("utf-8")
        except UnicodeError as error:
            raise ValidationError("Native include is not a supported text source", stage="netlist", code="unsupported_include") from error
        basename = re.sub(r"[^A-Za-z0-9_.-]", "_", path.name)
        relative = entry_name if path == entry else "dependencies/" + hashlib.sha256(str(path).encode()).hexdigest()[:20] + "-" + basename
        record = {"path": relative, "source": str(path), "source_sha256": hashlib.sha256(original).hexdigest(), "source_bytes": len(original), "includes": []}
        files[path] = record
        active.add(path)
        lines = []
        for line in text.splitlines(keepends=True):
            line = _SEARCH.sub(lambda match: match[1] + '"."', line)
            reference = include_reference(line)
            if reference:
                token, (start, stop) = reference
                child = Path(token)
                child = child if child.is_absolute() else path.parent / child
                child_relative = capture(child, depth + 1)
                record["includes"].append(child_relative)
                rewritten = os.path.relpath(temporary / child_relative, (temporary / relative).parent)
                line = line[:start] + '"' + rewritten + '"' + line[stop:]
            lines.append(line)
        atomic_text(temporary / relative, "".join(lines))
        record.update(sha256=file_digest(temporary / relative), bytes=(temporary / relative).stat().st_size)
        active.remove(path)
        return relative

    try:
        capture(entry, 0)
        for path, record in files.items():
            if file_digest(path) != record["source_sha256"]:
                raise OperationError("Netlist source changed during export", stage="netlist", code="baseline_changed")
        manifest = {"schema_version": 1, "kind": "native_hspice_netlist_package" if entry_name == "input.sp" else "native_spectre_netlist_package", "input_file": entry_name, "source_file": str(entry),
                    "files": sorted(files.values(), key=lambda item: item["path"]), "include_closure_complete": True,
                    "source_bytes": total, "file_count": len(files), "paths_rewritten_only": True, "redistribution": "private_local_only"}
        atomic_json(temporary / "manifest.json", manifest)
        verify_netlist_package(temporary)
        publish_directory(temporary, destination)
        return {**manifest, "output_dir": str(destination), "input_file": str(destination / entry_name), "manifest": str(destination / "manifest.json")}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_netlist_package(directory):
    import json

    directory = safe_result_path(directory, must_exist=False)
    try:
        manifest = json.loads(safe_result_path(directory / "manifest.json", max_bytes=8 * 1024 ** 2).read_text())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("Native package manifest is malformed", stage="netlist", code="invalid_netlist_package") from error
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or not isinstance(manifest.get("files"), list) or not 1 <= len(manifest["files"]) <= 4096:
        raise ValidationError("Native package manifest is malformed or over budget", stage="netlist", code="invalid_netlist_package")
    paths = set()
    total = 0
    for record in manifest["files"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not 1 <= len(record["path"]) <= 4096 or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) or type(record.get("bytes")) is not int or not 0 <= record["bytes"] <= 256 * 1024 ** 2 or not isinstance(record.get("includes"), list) or any(not isinstance(item, str) for item in record["includes"]):
            raise ValidationError("Native package file declaration is malformed", stage="netlist", code="invalid_netlist_package")
        total += record["bytes"]
        if total > 256 * 1024 ** 2:
            raise ValidationError("Native package verification exceeds the local budget", stage="netlist", code="resource_budget_exceeded")
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in paths:
            raise ValidationError("Native package has unsafe or duplicate paths", stage="netlist", code="invalid_netlist_package")
        path = safe_result_path(directory / relative)
        paths.add(relative.as_posix())
        if file_digest(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise ValidationError("Native package file differs from its manifest", stage="netlist", code="netlist_package_mismatch")
        includes = []
        for line in path.read_text().splitlines():
            reference = include_reference(line)
            if reference:
                token = reference[0]
                resolved = (path.parent / token).resolve()
                if Path(token).is_absolute() or directory not in resolved.parents or not resolved.is_file():
                    raise ValidationError("Native package has an unresolved or external include", stage="netlist", code="netlist_package_mismatch")
                includes.append(resolved.relative_to(directory).as_posix())
        if includes != record["includes"]:
            raise ValidationError("Native include graph differs from its manifest", stage="netlist", code="netlist_package_mismatch")
    if manifest.get("input_file") not in ("input.scs", "input.sp") or manifest["input_file"] not in paths or any(child not in paths for record in manifest["files"] for child in record["includes"]):
        raise ValidationError("Native package is missing a required source", stage="netlist", code="netlist_package_mismatch")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValidationError("Native package contains a symlink", stage="netlist", code="invalid_netlist_package")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != paths | {"manifest.json"}:
        raise ValidationError("Native package contains untracked or missing files", stage="netlist", code="netlist_package_mismatch")
    return {"ok": True, "file_count": len(paths), "include_closure_complete": True}


def copy_native_package(source, destination):
    source = Path(source).resolve()
    destination = safe_result_path(destination, must_exist=False)
    verify_netlist_package(source)
    original = file_digest(source / "manifest.json")
    if destination.exists() or source == destination or source in destination.parents or destination in source.parents:
        raise ValidationError("Native package destination exists or overlaps its source", stage="netlist", code="target_exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".netlist-copy-", dir=destination.parent))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        verify_netlist_package(staging)
        if file_digest(staging / "manifest.json") != original or file_digest(source / "manifest.json") != original:
            raise OperationError("Native package changed during publication", stage="netlist", code="baseline_changed")
        publish_directory(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
