"""Immutable local snapshots; only unreferenced automatic captures may expire."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import atomic_json, file_digest
from .errors import ArtifactError, ValidationError
from .readback import canonical, validate_snapshot


def inventory(root, *, limit_bytes=None):
    entries = []
    total = 0
    for parent, directories, filenames in os.walk(root, followlinks=False):
        for name in directories + filenames:
            path = Path(parent) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ArtifactError("Snapshot trees must contain only regular files and directories", stage="snapshot", code="unsafe_path")
        for name in filenames:
            path = Path(parent) / name
            total += path.stat().st_size
            if (limit_bytes is not None and total > limit_bytes) or len(entries) >= 10000:
                raise ArtifactError("Snapshot exceeds its local resource budget", stage="snapshot", code="snapshot_quota_exceeded")
            entries.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
    return sorted(entries, key=lambda item: item["path"])


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ArtifactError("Invalid snapshot ID", stage="snapshot", code="invalid_snapshot_id")
    return value


class SnapshotStore:
    def __init__(self, root, *, retention_count=20, retention_hours=24, quota_bytes=10 * 1024 ** 3):
        self.root = Path(root).expanduser().resolve()
        self.retention_count = retention_count
        self.retention_hours = retention_hours
        self.quota_bytes = quota_bytes
        if min(retention_count, retention_hours, quota_bytes) <= 0:
            raise ValidationError("Snapshot retention values must be positive", stage="snapshot", code="invalid_snapshot_policy")

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArtifactError("Snapshot store is busy", stage="snapshot", code="snapshot_store_busy") from exc
            yield
        finally:
            os.close(descriptor)

    def save(self, snapshot, *, native_source=None, verified_reopen=None):
        errors = validate_snapshot(snapshot)
        if errors:
            raise ValidationError("Cannot store an invalid snapshot", stage="snapshot", code="invalid_snapshot", details={"errors": errors})
        verified = verified_reopen is not None and verified_reopen.get("ok") is True and verified_reopen.get("isolated") is True and verified_reopen.get("facts_sha256") == snapshot.get("native_copy", {}).get("facts_sha256")
        if snapshot.get("formal_ready") and (not verified or native_source is None or validate_snapshot(snapshot, require_complete=True)):
            raise ValidationError("Formal snapshots require complete facts and isolated reopen evidence", stage="snapshot", code="snapshot_not_formal")
        snapshot_id = identifier(snapshot["snapshot_id"])
        with self._lock():
            destination = self.root / snapshot_id
            if destination.exists():
                raise ArtifactError("Snapshots cannot be overwritten", stage="snapshot", code="snapshot_exists")
            native_entries = []
            if native_source is not None:
                source = Path(native_source)
                if source.is_symlink() or not source.is_dir():
                    raise ArtifactError("Native snapshot source is not a directory", stage="snapshot", code="native_copy_missing")
                native_entries = inventory(source, limit_bytes=self.quota_bytes)
            required = sum(item["size"] for item in native_entries) + len(canonical(snapshot).encode("utf-8")) * 2 + len(native_entries) * 512 + 65536
            self._cleanup(required)
            used = sum(item["size"] for item in inventory(self.root))
            if required + used > self.quota_bytes or shutil.disk_usage(self.root).free < required + 64 * 1024 ** 2:
                raise ArtifactError("Snapshot quota or disk reserve is exhausted; protected snapshots are retained", stage="snapshot", code="snapshot_quota_exceeded")
            temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=self.root))
            try:
                stored = json.loads(json.dumps(snapshot))
                if native_source is not None:
                    for entry in native_entries:
                        original = source / entry["path"]
                        copied = temporary / "native" / "root_view" / entry["path"]
                        copied.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        before = file_digest(original)
                        shutil.copyfile(original, copied, follow_symlinks=False)
                        os.chmod(copied, 0o600)
                        if before != file_digest(copied) or before != file_digest(original):
                            raise ArtifactError("Native source changed during snapshot capture", stage="snapshot", code="snapshot_source_changed")
                    if inventory(source, limit_bytes=self.quota_bytes) != native_entries:
                        raise ArtifactError("Native source inventory changed during capture", stage="snapshot", code="snapshot_source_changed")
                    for entry in native_entries:
                        if file_digest(source / entry["path"]) != file_digest(temporary / "native" / "root_view" / entry["path"]):
                            raise ArtifactError("Native source changed during capture", stage="snapshot", code="snapshot_source_changed")
                    expected_file = stored.get("native_copy", {}).get("database_file")
                    if expected_file:
                        if expected_file not in {entry["path"] for entry in native_entries} or file_digest(temporary / "native" / "root_view" / expected_file) != stored["native_copy"].get("database_sha256"):
                            raise ArtifactError("Native copy differs from its readback baseline", stage="snapshot", code="snapshot_source_changed")
                    stored["native_copy"] = {**stored.get("native_copy", {}), "path": "native/root_view", "reopen_verified": bool(verified)}
                atomic_json(temporary / "snapshot.json", stored)
                files = [entry for entry in inventory(temporary, limit_bytes=self.quota_bytes) if entry["path"] != "integrity.json"]
                for entry in files:
                    entry["sha256"] = file_digest(temporary / entry["path"])
                atomic_json(temporary / "integrity.json", {"snapshot_id": snapshot_id, "files": files})
                if used + sum(item["size"] for item in inventory(temporary)) > self.quota_bytes:
                    raise ArtifactError("Snapshot exceeds its storage quota", stage="snapshot", code="snapshot_quota_exceeded")
                os.rename(temporary, destination)
                temporary = None
            finally:
                if temporary is not None:
                    shutil.rmtree(temporary)
            self._cleanup()
        return stored

    def inspect(self, snapshot_id):
        path = self.root / identifier(snapshot_id)
        try:
            if path.is_symlink() or not path.is_dir():
                raise ValueError("snapshot path")
            manifest = json.loads((path / "integrity.json").read_text(encoding="utf-8"))
            if manifest["snapshot_id"] != snapshot_id:
                raise ValueError("snapshot identity")
            actual = [item for item in inventory(path, limit_bytes=self.quota_bytes) if item["path"] != "integrity.json"]
            for entry in actual:
                entry["sha256"] = file_digest(path / entry["path"])
            if actual != manifest["files"]:
                raise ValueError("snapshot file integrity")
            snapshot = json.loads((path / "snapshot.json").read_text(encoding="utf-8"))
            if snapshot.get("snapshot_id") != snapshot_id or validate_snapshot(snapshot):
                raise ValueError("snapshot content")
            return snapshot
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ArtifactError("Snapshot is missing, incomplete or altered", stage="snapshot", code="snapshot_integrity_failed") from exc

    def list(self):
        return sorted(path.name for path in self.root.iterdir() if path.is_dir() and not path.is_symlink() and re.fullmatch(r"[0-9a-f]{32}", path.name)) if self.root.is_dir() else []

    def retain(self, snapshot_id, reference):
        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError("Snapshot reference must be nonempty", stage="snapshot", code="invalid_snapshot_reference")
        with self._lock():
            self.inspect(snapshot_id)
            path = self.root / "references" / (snapshot_id + ".json")
            references = json.loads(path.read_text()) if path.exists() else []
            atomic_json(path, sorted(set(references + [reference])))

    def cleanup(self):
        with self._lock():
            self._cleanup()

    def _cleanup(self, required=0):
        now = datetime.now(timezone.utc).timestamp()
        candidates = []
        for snapshot_id in self.list():
            path = self.root / snapshot_id
            try:
                snapshot = self.inspect(snapshot_id)
                options = snapshot.get("options", {})
                if options.get("automatic") is not True or options.get("fixed") is True or (self.root / "references" / (snapshot_id + ".json")).exists():
                    continue
                created = datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")).timestamp()
                design = canonical(snapshot["readback"].get("root", snapshot["readback"].get("target")))
                size = sum(item["size"] for item in inventory(path))
                candidates.append((created, snapshot_id, size, design))
            except (ArtifactError, ValueError, TypeError, KeyError):
                continue
        total = sum(item["size"] for item in inventory(self.root))
        ordered = sorted(candidates, reverse=True)
        for index, (created, snapshot_id, size, design) in enumerate(ordered):
            if index >= self.retention_count or now - created > self.retention_hours * 3600:
                shutil.rmtree(self.root / snapshot_id)
                total -= size
        for created, snapshot_id, size, design in sorted(candidates):
            path = self.root / snapshot_id
            if total + required > self.quota_bytes and path.exists():
                shutil.rmtree(path)
                total -= size
