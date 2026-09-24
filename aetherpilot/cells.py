"""Whole-cell inventory and explicitly approved, byte-backed native deletion."""

import os
import re
import shutil
import time
from pathlib import Path

from .artifacts import atomic_json, file_digest
from .contract import digest
from .errors import OperationError, ValidationError
from .native import LibraryOps, require
from .snapshot_native import library_path
from .snapshots import inventory
from .transactions import writable_target


class CellOps:
    def __init__(self, api):
        self.api = api
        self.library = LibraryOps(api)

    def _root(self, target):
        writable_target(target)
        root = library_path(self.api, target["library"]) / target["cell"]
        if root.is_symlink() or not root.is_dir():
            raise OperationError("Cell directory is missing or unsafe", stage="ownership", code="unsafe_path")
        return root

    def inspect(self, target, *, max_bytes=256 * 1024 ** 2):
        root = self._root(target)
        views = self.library.views(target["library"], target["cell"])
        if not views:
            raise OperationError("Cell contains no native views", stage="ownership", code="view_inventory_mismatch")
        for view in views:
            if self.api.dbFindCV(target["library"], target["cell"], view) is not None:
                raise OperationError("Open views cannot be deleted", stage="ownership", code="user_view_open")
            self.library.require_unlocked({**target, "view": view})
        files = inventory(root, limit_bytes=max_bytes)
        for entry in files:
            if any(marker in Path(entry["path"]).name.casefold() for marker in (".cdslck", ".oalock", ".lock")):
                raise OperationError("Cell contains a lock file; no lock is removed", stage="ownership", code="native_view_locked")
            entry["sha256"] = file_digest(root / entry["path"])
        directories = []
        for parent, children, filenames in os.walk(root, followlinks=False):
            directories.extend((Path(parent) / name).relative_to(root).as_posix() for name in children)
            if len(directories) > 10000:
                raise OperationError("Cell directory count exceeds its budget", stage="ownership", code="resource_limit")
        facts = {"views": views, "files": files, "directories": sorted(directories)}
        return {"target": target, "scope": "all_cell_files_and_views", "inventory_sha256": digest(facts), **facts}

    def delete(self, target, *, inventory_sha256, approved, artifacts, result):
        if approved is not True:
            raise OperationError("Cell deletion requires explicit approval", stage="ownership", code="approval_required")
        if not isinstance(inventory_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", inventory_sha256):
            raise ValidationError("Cell deletion requires its inventory SHA-256", stage="ownership", code="invalid_baseline")
        captured = self.inspect(target)
        if captured["inventory_sha256"] != inventory_sha256:
            raise OperationError("Cell inventory changed", stage="ownership", code="external_changes")
        source = self._root(target)
        backup = artifacts.path / "cell-backup"
        backup.mkdir(mode=0o700)
        deadline = time.monotonic() + 60
        for directory in captured["directories"]:
            (backup / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        for entry in captured["files"]:
            if time.monotonic() >= deadline:
                raise OperationError("Cell backup exceeded its deadline", stage="backup", code="timeout")
            original, copied = source / entry["path"], backup / entry["path"]
            if original.is_symlink() or not original.is_file():
                raise OperationError("Cell file changed during backup", stage="backup", code="external_changes")
            copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(original, copied, follow_symlinks=False)
            copied.chmod(0o600)
            if file_digest(copied) != entry["sha256"]:
                raise ValidationError("Cell backup content differs", stage="backup", code="backup_mismatch")
        artifacts.write("cell-inventory.json", captured)
        recovery = {"state": "prepared", "target": target, "inventory_sha256": inventory_sha256,
                    "backup": str(backup), "inventory": str(artifacts.path / "cell-inventory.json"),
                    "restoration": "explicit_offline_restore_to_original_cell_path", "original_path": str(source)}
        recovery_path = Path.cwd() / ".aetherpilot" / "deletions" / (result["operation_id"] + ".json")
        atomic_json(recovery_path, recovery)
        try:
            if self.inspect(target)["inventory_sha256"] != inventory_sha256:
                raise OperationError("Cell changed before deletion", stage="ownership", code="external_changes")
            cell = require(self.api.dmGetObj(target["library"], target["cell"]), "dmGetObj", "delete")
            require(self.api.dmDeleteObj(cell), "dmDeleteObj", "delete")
            if source.exists() or self.library.views(target["library"], target["cell"]):
                raise ValidationError("Cell remains after native deletion", stage="readback", code="delete_mismatch")
            recovery["state"] = "deleted"
        except Exception:
            recovery["state"] = "recovery_required"
            raise
        finally:
            atomic_json(recovery_path, recovery)
            artifacts.write("cell-deletion.json", recovery)
        result["changes"].append({"type": "cell_deleted", "target": target, "backup": str(backup)})
        return recovery
