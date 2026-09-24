"""Private, content-addressed evidence with a deliberately narrow export surface."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import socket
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import ArtifactError


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def materialize_evidence_links(directory):
    root = Path(directory).resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        target = path.resolve()
        if root not in target.parents or not target.is_file():
            raise ArtifactError("Evidence symlink escapes its bundle", stage="artifacts", code="unsafe_path")
        descriptor, temporary = tempfile.mkstemp(prefix=".materializing-", dir=path.parent)
        os.close(descriptor)
        try:
            shutil.copyfile(target, temporary)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def redact(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if re.search(r"password|secret|token|license_value", key, re.I) else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    # Only redact recognizable absolute filesystem roots. A slash in normal
    # engineering prose (for example ``角色/器件`` or ``激励/负载``) is not a
    # path and must remain semantically intact.
    text = re.sub(r"(?:[A-Za-z]:[\\/]|/(?:home|opt|tmp|var|mnt|usr|root|workspace)/)(?:[^\s\"'<>]+)", "<private-path>", value)
    text = re.sub(r"\b\d+@[A-Za-z0-9_.-]+\b", "<license-endpoint>", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<address>", text)
    for private in (str(Path.home()), getpass.getuser(), socket.gethostname()):
        if len(private) >= 3:
            text = text.replace(private, "<private>")
    return text


def empty_result(operation, operation_id=None):
    return {
        "schema_version": 1, "ok": False, "operation_id": operation_id or uuid.uuid4().hex,
        "operation": operation, "stage": "plan", "changes": [], "readback": {},
        "metrics": {}, "artifacts": [], "warnings": [], "errors": [],
    }


def _manifested_path(path, root):
    """Return whether a bundle file is immutable evidence.

    Detached Maestro jobs update ``job-status.json`` as their supervisor
    progresses.  That operational record lives beside the immutable bundle,
    but must not be fingerprinted by its manifest.
    """
    relative = path.relative_to(root).as_posix()
    return relative not in {"manifest.json", "job-status.json"}


class ArtifactOps:
    def __init__(self, root, operation_id):
        self.root = Path(root).resolve()
        self.operation_id = operation_id
        self.path = self.root / operation_id
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.mkdir(mode=0o700)
        except OSError as exc:
            raise ArtifactError("Cannot create private artifact directory", stage="artifacts", code="write_failed") from exc

    @classmethod
    def existing(cls, path, operation_id):
        instance = cls.__new__(cls)
        instance.path = Path(path).resolve()
        instance.root = instance.path.parent
        instance.operation_id = operation_id
        if instance.path.name != operation_id or not instance.path.is_dir():
            raise ArtifactError("Invalid worker artifact directory", stage="artifacts", code="unsafe_path")
        return instance

    def write(self, name, value):
        try:
            atomic_json(self.path / name, value)
        except (OSError, ValueError, TypeError) as exc:
            raise ArtifactError("Cannot save artifact", stage="artifacts", code="write_failed", details={"artifact": name}) from exc

    def write_text(self, name, value):
        if not isinstance(value, str):
            raise ArtifactError("Text artifact requires a string", stage="artifacts", code="write_failed", details={"artifact": name})
        try:
            relative = Path(name)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise ValueError("unsafe artifact path")
            path = self.path / relative
            atomic_text(path, value)
        except (OSError, ValueError, TypeError) as exc:
            raise ArtifactError("Cannot save text artifact", stage="artifacts", code="write_failed", details={"artifact": name}) from exc

    def event(self, stage, status, **details):
        record = {"time": timestamp(), "operation_id": self.operation_id, "stage": stage, "status": status, **details}
        try:
            descriptor = os.open(self.path / "journal.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.write("stage.json", record)
        except OSError as exc:
            raise ArtifactError("Cannot append operation journal", stage="artifacts", code="write_failed") from exc

    def finish(self, result):
        self.event(result["stage"], "success" if result["ok"] else "failed")
        materialize_evidence_links(self.path)
        result["artifacts"] = sorted(path.relative_to(self.path).as_posix() for path in self.path.rglob("*") if path.is_file() and _manifested_path(path, self.path) and path.name != "result.json")
        result["artifacts"] += ["result.json", "manifest.json"]
        self.write("result.json", result)
        entries = []
        for path in sorted(self.path.rglob("*")):
            if path.is_file() and _manifested_path(path, self.path):
                os.chmod(path, 0o600)
                entries.append({"path": path.relative_to(self.path).as_posix(), "size": path.stat().st_size, "sha256": file_digest(path)})
        # ``job-status.json`` is deliberately omitted: detached supervisors
        # update it after this immutable evidence bundle is sealed.
        self.write("manifest.json", {"schema_version": 1, "operation_id": self.operation_id, "privacy": "private", "created_at": timestamp(), "files": entries})
        return result

    @staticmethod
    def inspect(root, operation_id):
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ArtifactError("Invalid operation ID", stage="inspect", code="invalid_id")
        directory = Path(root).resolve() / operation_id
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
            if manifest["operation_id"] != operation_id:
                raise ValueError("Operation ID mismatch")
            seen = set()
            for entry in manifest["files"]:
                path = directory / entry["path"]
                if Path(entry["path"]).is_absolute() or ".." in Path(entry["path"]).parts or path.is_symlink() or directory not in path.resolve().parents or entry["path"] in seen:
                    raise ValueError("Unsafe or duplicate manifest entry")
                seen.add(entry["path"])
                if path.stat().st_size != entry["size"] or file_digest(path) != entry["sha256"]:
                    raise ValueError("Artifact content mismatch")
            if "result.json" not in seen:
                raise ValueError("Missing result entry")
            result = json.loads((directory / "result.json").read_text())
            if result["operation_id"] != operation_id:
                raise ValueError("Result ID mismatch")
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ArtifactError("Artifact bundle is missing, incomplete or altered", stage="inspect", code="integrity_failed") from exc

    @staticmethod
    def public_summary(result):
        return {
            "schema_version": 1,
            "ok": result["ok"],
            "operation_id": result["operation_id"],
            "operation": result["operation"],
            "stage": result["stage"],
            "change_count": len(result["changes"]),
            "artifact_count": len(result["artifacts"]),
            "errors": [{key: item[key] for key in ("category", "stage", "code")} for item in result["errors"]],
            "notice": "Status-only summary; design data, metrics, paths and logs intentionally omitted.",
        }
