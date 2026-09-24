"""Local-library write boundaries, ownership and verified pre-mutation backups."""

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .artifacts import atomic_json, file_digest
from .contract import digest, no_duplicate_keys
from .errors import OperationError, ValidationError
from .library import library_name
from .snapshots import inventory
from .transactions import PROTECTED_LIBRARIES


def writable_library_path(name, path, workspace=None):
    library_name(name)
    if name in PROTECTED_LIBRARIES:
        raise OperationError("Technology and system libraries are read-only", stage="ownership", code="protected_library")
    if not path:
        raise OperationError("Native library location cannot be verified", stage="ownership", code="library_path_missing")
    location = Path(path).expanduser().absolute()
    workspace = Path(workspace or Path.cwd()).resolve()
    if location != location.resolve() or workspace not in location.parents or ".aetherpilot" in location.relative_to(workspace).parts:
        raise OperationError("Library writes require a real directory inside the local workspace", stage="ownership", code="protected_library_path")
    return location


class LibraryGuard:
    def __init__(self, workspace=None):
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.root = self.workspace / ".aetherpilot"

    def registry(self, name):
        library_name(name)
        return self.root / "libraries" / (digest({"name": name}) + ".json")

    def register(self, name, path, *, state="active"):
        location = writable_library_path(name, path, self.workspace)
        atomic_json(self.registry(name), {"schema_version": 1, "name": name, "path": str(location), "state": state})

    def require(self, name, path, *, approved=False):
        location = writable_library_path(name, path, self.workspace)
        if not location.is_dir():
            raise OperationError("Native library directory is missing", stage="ownership", code="library_path_missing")
        if approved is True:
            return location
        registry = self.registry(name)
        try:
            if registry.is_symlink() or registry.stat().st_size > 16384:
                raise ValueError("Unsafe ownership registry")
            owner = json.loads(registry.read_text(), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError) as exc:
            raise OperationError("Unmanaged library metadata is read-only without explicit approval", stage="ownership", code="approval_required") from exc
        if owner != {"schema_version": 1, "name": name, "path": str(location), "state": "active"}:
            raise OperationError("Library ownership identity differs", stage="ownership", code="library_identity_mismatch")
        return location

    @staticmethod
    def capture(path):
        path = Path(path)
        deadline = time.monotonic() + 20
        files = inventory(path, limit_bytes=64 * 1024 ** 2)
        for entry in files:
            if any(marker in Path(entry["path"]).name.casefold() for marker in (".cdslck", ".oalock", ".lock")):
                raise OperationError("Library contains a native lock; no lock is removed", stage="ownership", code="native_view_locked")
            if time.monotonic() >= deadline:
                raise OperationError("Library inventory exceeded its deadline", stage="ownership", code="timeout")
            entry["sha256"] = file_digest(path / entry["path"])
        directories = []
        for parent, children, unused in os.walk(path, followlinks=False):
            directories.extend((Path(parent) / name).relative_to(path).as_posix() for name in children)
            if len(directories) > 10000 or time.monotonic() >= deadline:
                raise OperationError("Library directory inventory exceeded its budget", stage="ownership", code="resource_limit")
        facts = {"files": files, "directories": sorted(directories)}
        return {**facts, "sha256": digest(facts)}

    def backup(self, name, path, action):
        location = writable_library_path(name, path, self.workspace)
        baseline = self.capture(location)
        directory = self.root / "library-transactions" / uuid.uuid4().hex
        backup = directory / "backup"
        backup.mkdir(mode=0o700, parents=True)
        for relative in baseline["directories"]:
            (backup / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        for entry in baseline["files"]:
            source, destination = location / entry["path"], backup / entry["path"]
            if source.is_symlink() or not source.is_file():
                raise OperationError("Library changed during backup", stage="backup", code="baseline_changed")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            destination.chmod(0o600)
            if file_digest(destination) != entry["sha256"]:
                raise OperationError("Library backup fingerprint differs", stage="backup", code="backup_mismatch")
        if self.capture(location) != baseline or self.capture(backup) != baseline:
            raise OperationError("Library changed during its verified backup", stage="backup", code="baseline_changed")
        record = {"schema_version": 1, "library": name, "source": str(location), "action": action,
                  "baseline": baseline, "backup": str(backup), "status": "prepared",
                  "restoration": "explicit_offline_restore_of_verified_backup_and_library_mapping"}
        atomic_json(directory / "transaction.json", record)
        return directory, record

    @staticmethod
    def finish(transaction, *, success, details=None):
        directory, record = transaction
        updated = {**record, "status": "verified" if success else "recovery_required", "details": details or {}}
        atomic_json(directory / "transaction.json", updated)
        return {"transaction": str(directory / "transaction.json"), "backup": record["backup"], "baseline_sha256": record["baseline"]["sha256"]}
