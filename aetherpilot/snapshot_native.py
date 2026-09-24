"""Native dependency bundles verified in an independent product-Python process."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .artifacts import atomic_json, file_digest
from .capture import CaptureCoordinator
from .contract import digest, identifier
from .errors import OperationError
from .native import LibraryOps, require
from .readback import make_snapshot, validate_snapshot
from .schematic_content import schematic_content
from .snapshots import SnapshotStore, inventory


def design_facts(hierarchy):
    return [{"path": record["path"], "target": record["target"], "status": record["status"],
             "facts": schematic_content(record["readback"]) if "readback" in record else record.get("primitive"),
             "port_bindings": record.get("port_bindings", [])} for record in hierarchy["records"]]


def library_path(api, name):
    library = require(api.dbOpenLib(name, "r"), "dbOpenLib", "snapshot")
    try:
        value = api.emyString()
        library.getPath(value)
        path = Path(str(value)).resolve()
        if not str(value) or not path.is_dir():
            raise OperationError("Native library path is unavailable", stage="snapshot", code="dependency_missing", details={"library": name})
        return path
    finally:
        require(api.dbCloseLib(api.emyString(name)), "dbCloseLib", "snapshot")


def tree_state(paths, *, max_bytes, exclude_locks=False):
    result = []
    total = 0
    for name, path in sorted(paths.items()):
        entries = inventory(path, limit_bytes=max_bytes - total)
        for entry in entries:
            if any(marker in Path(entry["path"]).name for marker in (".cdslck", ".oalock")):
                if exclude_locks:
                    continue
                raise OperationError("Dependency contains a native lock; snapshot does not copy or remove locks", stage="snapshot", code="dependency_locked", details={"library": name})
            result.append({"library": name, **entry, "sha256": file_digest(path / entry["path"])})
            total += entry["size"]
            if len(result) > 10000:
                raise OperationError("Dependency file budget exceeded", stage="snapshot", code="resource_limit")
    return result


def copy_bundle(paths, state, destination, *, deadline):
    for entry in state:
        if time.monotonic() >= deadline:
            raise OperationError("Native dependency copy timed out", stage="snapshot", code="timeout")
        source = paths[entry["library"]] / entry["path"]
        target = destination / "libraries" / entry["library"] / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.is_symlink():
            raise OperationError("Dependency changed into a symbolic link", stage="snapshot", code="unsafe_path")
        shutil.copyfile(source, target, follow_symlinks=False)
        if file_digest(target) != entry["sha256"]:
            raise OperationError("Dependency changed during copy", stage="snapshot", code="snapshot_source_changed")
    definitions = "".join("DEFINE %s libraries/%s\n" % (identifier(name), identifier(name)) for name in sorted(paths))
    (destination / "lib.defs").write_text(definitions, encoding="utf-8")


def isolated_reopen(bundle, expected, *, deadline, evidence_dir, configuration, owned=True, resource_options=None):
    request_path = evidence_dir / "reopen-request.json"
    response_path = evidence_dir / "reopen-response.json"
    atomic_json(request_path, {"bundle": str(bundle), "response": str(response_path), "target": expected["root"],
                              "max_depth": expected["max_depth"], "configuration": configuration,
                              "facts_sha256": digest(design_facts(expected))})
    command = [sys.executable, str(Path(__file__).with_name("snapshot_reopen.py")), str(request_path)]
    process = None
    try:
        with (evidence_dir / "reopen.stdout.log").open("wb") as stdout, (evidence_dir / "reopen.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=bundle, stdout=stdout, stderr=stderr, start_new_session=not owned)
            try:
                if owned:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                else:
                    from .resources import wait_bounded

                    limits = resource_options or {}
                    wait_bounded(process, timeout=max(0.01, deadline - time.monotonic()), artifact_dir=evidence_dir.parent,
                                 max_rss_mb=limits.get("max_worker_rss_mb", 2048), max_bytes=limits.get("max_operation_bytes", 1024 ** 3),
                                 min_free_mb=limits.get("min_free_memory_mb", 512))
            except subprocess.TimeoutExpired as exc:
                raise OperationError("Isolated reopen timed out", stage="snapshot", code="timeout") from exc
        if process.returncode != 0 or not response_path.is_file():
            raise OperationError("Isolated product Python could not verify the bundle", stage="snapshot", code="snapshot_reopen_failed", details={"returncode": process.returncode})
        response = json.loads(response_path.read_text())
        if response.get("facts_sha256") != digest(design_facts(expected)) or response.get("ok") is not True:
            raise OperationError("Isolated database facts differ from the captured source", stage="snapshot", code="snapshot_reopen_mismatch")
        return response
    finally:
        if process is not None and not owned:
            from .runtime import stop_owned_group

            stop_owned_group(process)
        elif process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def memory_overlay(api, hierarchy, bundle, scratch):
    copied = set()
    for record in hierarchy["records"]:
        actual = record.get("readback")
        if actual is None or actual.get("source", {}).get("modified") is not True:
            continue
        target = record["target"]
        identity = tuple(target[key] for key in ("library", "cell", "view"))
        if identity in copied:
            continue
        copied.add(identity)
        source = api.dbFindCV(*identity)
        if source is None or api.dbGetDesignCounter(source) != actual["source"]["design_counter"]:
            raise OperationError("Edited source revision changed before copying", stage="snapshot", code="snapshot_source_changed")
        alias = "APcapture_" + uuid.uuid4().hex
        alias_path = scratch / alias
        native_name = api.emyScalarName(api.emyNativeNS(), alias)
        library = require(api.emyLib.create(native_name, str(alias_path)), "emyLib.create", "snapshot")
        try:
            require(api.dbCopyCV(source, alias, target["cell"], target["view"], False), "dbCopyCV", "snapshot")
        finally:
            require(api.dbCloseLib(alias), "dbCloseLib", "snapshot")
        staged_cell = alias_path / target["cell"]
        staged_view = staged_cell / target["view"]
        destination_cell = bundle / "libraries" / target["library"] / target["cell"]
        if not staged_view.is_dir():
            raise OperationError("Memory copy did not create a native view", stage="snapshot", code="native_copy_missing")
        if not destination_cell.exists():
            shutil.copytree(staged_cell, destination_cell)
        else:
            destination_view = destination_cell / target["view"]
            if destination_view.exists():
                shutil.rmtree(destination_view)
            shutil.copytree(staged_view, destination_view)
        if api.dbGetDesignCounter(source) != actual["source"]["design_counter"]:
            raise OperationError("Edited source changed during native memory copy", stage="snapshot", code="snapshot_source_changed")


def saved_snapshot(adapter, target, *, options, artifacts):
    if options.get("recursive", True) is not True:
        raise OperationError("A formal snapshot requires recursive dependency closure", stage="snapshot", code="dependency_closure_required")
    max_bytes = min(options.get("snapshot_quota_bytes", 10 * 1024 ** 3), options.get("max_operation_bytes", 1024 ** 3))
    configuration = options.get("configuration")
    edited = options.get("source_kind", "saved") == "edited"
    known_paths = {}

    def observe():
        actual = adapter.schematic.read_hierarchy(target, max_depth=options.get("max_depth", 20), configuration=configuration, pdk_root=options.get("pdk_root"))
        errors = validate_snapshot(make_snapshot(actual), require_complete=True)
        if errors:
            raise OperationError("Formal capture needs a complete hierarchy", stage="snapshot", code="incomplete_readback", details={"errors": errors, "completeness": actual["completeness"]})
        names = {"basic"}
        for record in actual["records"]:
            names.add(record["target"]["library"])
            readback = record.get("readback")
            if readback is None:
                continue
            if edited and type(readback.get("source", {}).get("design_counter")) is not int:
                raise OperationError("Edited capture needs native design counters", stage="snapshot", code="source_revision_unavailable")
            if not edited and readback.get("source", {}).get("modified") is not False:
                raise OperationError("Saved capture cannot represent unsaved editor content", stage="snapshot", code="source_state_unconfirmed")
            if readback.get("technology"):
                names.add(readback["technology"])
            names.update(instance["master"]["library"] for instance in readback["instances"])
        paths = {name: library_path(adapter.api, name) for name in sorted(names)}
        known_paths.clear()
        known_paths.update(paths)
        state = tree_state(paths, max_bytes=max_bytes, exclude_locks=edited)
        return {"hierarchy": actual, "files": state, "paths": {name: str(path) for name, path in paths.items()}}

    def copy(revision, deadline):
        stage = Path(tempfile.mkdtemp(prefix="snapshot-bundle-", dir=artifacts.path))
        try:
            copy_bundle(known_paths, revision["files"], stage, deadline=deadline)
            if edited:
                scratch = artifacts.path / (stage.name + "-memory")
                scratch.mkdir(mode=0o700)
                memory_overlay(adapter.api, revision["hierarchy"], stage, scratch)
            frozen_paths = {name: stage / "libraries" / name for name in revision["paths"]}
            return {"path": str(stage), "revision": revision, "files": tree_state(frozen_paths, max_bytes=max_bytes)}
        except Exception:
            shutil.rmtree(stage)
            raise

    def verify(candidate, deadline):
        bundle = Path(candidate["path"])
        evidence = artifacts.path / (bundle.name + "-verification")
        evidence.mkdir(mode=0o700)
        result = isolated_reopen(bundle, candidate["revision"]["hierarchy"], deadline=deadline, evidence_dir=evidence, configuration=configuration,
                                 owned=adapter.owned, resource_options=options)
        frozen_paths = {name: bundle / "libraries" / name for name in candidate["revision"]["paths"]}
        if tree_state(frozen_paths, max_bytes=max_bytes) != candidate["files"]:
            raise OperationError("Isolated reopen modified the native dependency bundle", stage="snapshot", code="snapshot_reopen_mutated")
        return result

    captured = CaptureCoordinator().capture(observe, copy, verify, lambda candidate: shutil.rmtree(candidate["path"]))
    bundle = Path(captured["candidate"]["path"])
    snapshot = make_snapshot(captured["revision"]["hierarchy"], source_kind="edited" if edited else "saved", options=options,
                             dependencies=captured["candidate"]["files"],
                             native_copy={"scope": "native_library_dependency_closure", "reopen_verified": True,
                                          "facts_sha256": captured["verification"]["facts_sha256"], "libraries": sorted(known_paths)})
    snapshot.update(formal_ready=True, capture_attempts=captured["attempts"], limitations=[])
    store = SnapshotStore(options.get("snapshot_root", artifacts.root / "snapshots"),
                          retention_count=options.get("snapshot_retention_count", 20),
                          retention_hours=options.get("snapshot_retention_hours", 24), quota_bytes=options.get("snapshot_quota_bytes", 10 * 1024 ** 3))
    snapshot["storage"] = {"root": str(store.root), "snapshot_id": snapshot["snapshot_id"], "immutable": True}
    return store.save(snapshot, native_source=bundle, verified_reopen=captured["verification"])
