"""Guarded recovery of a managed, single-cell schematic publication."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .artifacts import atomic_json, file_digest, timestamp
from .contract import digest, no_duplicate_keys
from .errors import OperationError, ValidationError
from .schematic_content import schematic_content
from .transactions import MANAGED_REGISTRY_SCHEMA, managed_registry_matches, registry_fingerprint, writable_target


_OPERATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_PENDING = {"publishing", "post_copy_verified", "recovery_required", "restoring"}


def pending_schematic_recovery(state_root, target):
    """Refuse new writes while a prior publication for this cell is unresolved."""
    directory = Path(state_root) / "recovery"
    if not directory.exists():
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise OperationError("Schematic recovery directory is unsafe", stage="ownership", code="recovery_invalid")
    identity = {key: target[key] for key in ("library", "cell", "view")}
    for path in directory.glob("schematic-*.json"):
        if path.is_symlink() or not path.is_file():
            raise OperationError("Schematic recovery journal is unsafe", stage="ownership", code="recovery_invalid")
        try:
            record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError, TypeError) as exc:
            raise OperationError("Schematic recovery journal is invalid", stage="ownership", code="recovery_invalid") from exc
        if not isinstance(record, dict) or record.get("domain") != "schematic" or not isinstance(record.get("target"), dict):
            raise OperationError("Schematic recovery journal is invalid", stage="ownership", code="recovery_invalid")
        if {key: record["target"].get(key) for key in identity} == identity:
            if record.get("state") in _PENDING:
                return record.get("operation_id")
            if record.get("state") not in {"published", "recovered"}:
                raise OperationError("Schematic recovery state is invalid", stage="ownership", code="recovery_invalid")
    return None


class SchematicRecovery:
    """Keep a durable write-ahead record and restore only a proven owned copy.

    The native database copy is not assumed atomic.  If a target cannot be
    identified as either the exact baseline or this operation's copied view,
    the backup and journal remain for manual investigation instead of being
    copied over an unknown editor change.
    """

    def __init__(self, backend, workspace, artifacts, result):
        self.backend = backend
        self.workspace = Path(workspace)
        self.root = self.workspace / ".aetherpilot"
        self.artifacts = artifacts
        self.result = result

    def _path(self, operation_id):
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise ValidationError("Invalid schematic recovery operation ID", stage="recovery", code="invalid_id")
        directory = self.root / "recovery"
        if directory.is_symlink():
            raise OperationError("Schematic recovery directory is unsafe", stage="recovery", code="recovery_invalid")
        return self.root / "recovery" / ("schematic-" + operation_id + ".json")

    def _registry_path(self, target):
        identity = {key: target[key] for key in ("library", "cell", "view")}
        return self.root / "managed" / (digest(identity) + ".json")

    def _read(self, target):
        value = self.backend.read(target)
        coverage = value.get("completeness", {})
        if (coverage.get("status") != "complete" or coverage.get("issues") or value.get("unsupported") or
                value.get("source", {}).get("saved") is not True or value.get("source", {}).get("modified") is not False):
            raise ValidationError("Schematic recovery requires a complete saved native readback", stage="recovery", code="incomplete_readback")
        return value

    @staticmethod
    def _fingerprint(value):
        return registry_fingerprint(value, schematic_content)

    def _guard(self, target):
        self.backend.library.require_unlocked(target)
        finder = getattr(self.backend.api, "dbFindCV", None)
        if callable(finder) and finder(target["library"], target["cell"], target["view"]) is not None:
            raise OperationError("An open schematic cannot be recovered over", stage="recovery", code="user_view_open")

    def _load(self, operation_id):
        path = self._path(operation_id)
        if path.is_symlink() or not path.is_file():
            raise OperationError("Schematic recovery journal is missing", stage="recovery", code="recovery_missing")
        if path.stat().st_size > 1024 * 1024:
            raise ValidationError("Schematic recovery journal is too large", stage="recovery", code="recovery_invalid")
        try:
            record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError, TypeError) as exc:
            raise ValidationError("Schematic recovery journal is invalid", stage="recovery", code="recovery_invalid") from exc
        if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("operation_id") != operation_id or record.get("domain") != "schematic":
            raise ValidationError("Schematic recovery journal identity differs", stage="recovery", code="recovery_invalid")
        return record

    def begin(self, operation_id, *, target, staging, backup, baseline, candidate):
        """Persist intent after backup verification and before overwriting target."""
        writable_target(target)
        if (target["view"] != "schematic" or backup["library"] != target["library"] or backup["view"] != "schematic" or
                staging["library"] != target["library"] or staging["view"] != "schematic" or
                backup.get("technology", "018um_PDK") != target.get("technology", "018um_PDK") or
                staging.get("technology", "018um_PDK") != target.get("technology", "018um_PDK") or
                not isinstance(backup.get("cell"), str) or not re.fullmatch(r"APbackup_[0-9a-f]{32}", backup["cell"]) or
                not isinstance(staging.get("cell"), str) or not re.fullmatch(r"APstage_[0-9a-f]{32}", staging["cell"]) or
                len({target["cell"], backup["cell"], staging["cell"]}) != 3):
            raise ValidationError("Recovery is limited to one schematic cell", stage="recovery", code="invalid_target")
        if self._path(operation_id).exists():
            raise OperationError("Recovery journal already exists", stage="recovery", code="recovery_exists")
        self._guard(target)
        current = self._read(target)
        baseline_fingerprint = self._fingerprint(baseline)
        if self._fingerprint(current) != baseline_fingerprint or current.get("owner") != baseline.get("owner"):
            raise OperationError("Target changed before recovery journal", stage="recovery", code="external_changes")
        backup_readback = self._read(backup)
        backup_fingerprint = self._fingerprint(backup_readback)
        if (backup_fingerprint["content_sha256"] != baseline_fingerprint["content_sha256"] or
                backup_readback.get("owner") != baseline.get("owner")):
            raise ValidationError("Schematic backup differs from baseline", stage="backup", code="backup_mismatch")
        candidate_fingerprint = self._fingerprint(candidate)
        if not candidate.get("owner"):
            raise ValidationError("Schematic candidate has no owner", stage="recovery", code="owner_missing")
        registry_path = self._registry_path(target)
        if registry_path.is_symlink() or not registry_path.is_file():
            raise OperationError("Managed registry is unavailable", stage="recovery", code="ownership_record_incomplete")
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError, TypeError) as exc:
            raise OperationError("Managed registry is invalid", stage="recovery", code="ownership_record_incomplete") from exc
        if not managed_registry_matches(registry, current, baseline_fingerprint, domain="schematic", target=target):
            raise OperationError("Managed registry differs from baseline", stage="recovery", code="external_changes")
        record = {"schema_version": 1, "domain": "schematic", "operation_id": operation_id, "state": "publishing", "created_at": timestamp(),
                  "target": target, "staging": staging, "backup": backup,
                  "baseline": {**baseline_fingerprint, "owner": baseline["owner"]},
                  "candidate": {**candidate_fingerprint, "owner": candidate["owner"]},
                  "backup_fingerprint": {**backup_fingerprint, "owner": backup_readback["owner"]},
                  "registry_sha256": file_digest(registry_path), "post_copy": None, "restore_checkpoint": None}
        atomic_json(self._path(operation_id), record)
        self.artifacts.write("recovery-intent.json", record)
        return record

    def checkpoint(self, operation_id, actual):
        """Bind the copied target's native fingerprint before further checks."""
        record = self._load(operation_id)
        if record["state"] != "publishing":
            raise OperationError("Recovery journal is not publishing", stage="recovery", code="recovery_state")
        fingerprint = self._fingerprint(actual)
        if fingerprint["content_sha256"] != record["candidate"]["content_sha256"] or actual.get("owner") != record["candidate"]["owner"]:
            raise ValidationError("Copied schematic does not match the candidate", stage="readback", code="publish_mismatch")
        record.update(state="post_copy_verified", post_copy=fingerprint)
        atomic_json(self._path(operation_id), record)
        self.artifacts.write("post-copy-checkpoint.json", record)
        return record

    def published(self, operation_id):
        record = self._load(operation_id)
        if record["state"] != "post_copy_verified":
            raise OperationError("Publication lacks a native copy checkpoint", stage="recovery", code="recovery_state")
        record.update(state="published", published_at=timestamp())
        atomic_json(self._path(operation_id), record)

    def _record_failure(self, record, exc):
        record.update(state="recovery_required", last_error={"type": type(exc).__name__, "code": getattr(exc, "code", None), "time": timestamp()})
        atomic_json(self._path(record["operation_id"]), record)
        self.artifacts.write("publication-recovery.json", record)

    def _settle_candidate(self, record, state, fingerprint):
        """Keep a candidate publication record consistent with recovered OA."""
        cell = record["staging"]["cell"]
        if not re.fullmatch(r"APstage_[0-9a-f]{32}", cell):
            raise ValidationError("Recovery staging identity is invalid", stage="recovery", code="recovery_invalid")
        candidate_id = cell.removeprefix("APstage_")
        path = self.root / "candidates" / (candidate_id + ".json")
        if not path.exists():
            return
        if path.is_symlink() or not path.is_file():
            raise OperationError("Recovery candidate record is unsafe", stage="recovery", code="recovery_invalid")
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError, TypeError) as exc:
            raise OperationError("Recovery candidate record is invalid", stage="recovery", code="recovery_invalid") from exc
        if (not isinstance(candidate, dict) or candidate.get("candidate_id") != candidate_id or
                candidate.get("publication_operation_id") != record["operation_id"] or
                candidate.get("target") != record["target"]):
            raise OperationError("Recovery candidate identity differs", stage="recovery", code="recovery_invalid")
        if state == "published":
            candidate.update(state="published", published=fingerprint, published_at=timestamp(), publishable=False)
        else:
            candidate.update(state="recovered", recovery_operation_id=self.result["operation_id"], publishable=False)
        atomic_json(path, candidate)

    def recover(self, operation_id, *, approved=False, automatic=False):
        """Restore an unchanged owned copy, or refuse without touching it."""
        if approved is not True:
            raise OperationError("Schematic recovery requires explicit approval", stage="recovery", code="approval_required")
        record = self._load(operation_id)
        if record["state"] not in _PENDING:
            raise OperationError("Schematic publication is not pending recovery", stage="recovery", code="recovery_state", details={"state": record["state"]})
        try:
            target = record["target"]
            backup = record["backup"]
            writable_target(target)
            if (target["view"] != "schematic" or backup["library"] != target["library"] or backup["view"] != "schematic" or
                    backup.get("technology", "018um_PDK") != target.get("technology", "018um_PDK") or
                    record["staging"].get("library") != target["library"] or record["staging"].get("view") != "schematic" or
                    not isinstance(backup.get("cell"), str) or not re.fullmatch(r"APbackup_[0-9a-f]{32}", backup["cell"]) or
                    not isinstance(record["staging"].get("cell"), str) or
                    not re.fullmatch(r"APstage_[0-9a-f]{32}", record["staging"]["cell"]) or
                    backup["cell"] in {target["cell"], record["staging"]["cell"]}):
                raise ValidationError("Recovery journal targets differ", stage="recovery", code="recovery_invalid")
            self._guard(target)
            registry_path = self._registry_path(target)
            if registry_path.is_symlink() or not registry_path.is_file():
                raise OperationError("Managed registry is unavailable", stage="recovery", code="external_changes")
            registry_sha256 = file_digest(registry_path)
            current = self._read(target)
            current_fingerprint = self._fingerprint(current)
            if registry_sha256 != record["registry_sha256"]:
                registry = json.loads(registry_path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
                if (record.get("post_copy") == current_fingerprint and current.get("owner") == record["candidate"]["owner"] and
                        registry.get("operation_id") == operation_id and
                        managed_registry_matches(registry, current, current_fingerprint, domain="schematic", target=target)):
                    self._settle_candidate(record, "published", current_fingerprint)
                    record.update(state="published", published_at=timestamp(), reconciled_at=timestamp())
                    atomic_json(self._path(operation_id), record)
                    self.artifacts.write("recovery-result.json", record)
                    self.result["readback"] = current
                    return {"recovered": False, "restored": "published", "publication_committed": True,
                            "target": target, "fingerprint": current_fingerprint, "copied_backup": False}
                if (record.get("restore_checkpoint") == current_fingerprint and current.get("owner") == record["baseline"]["owner"] and
                        registry.get("recovery_operation_id") == record.get("restore_operation_id") and
                        managed_registry_matches(registry, current, current_fingerprint, domain="schematic", target=target)):
                    self._settle_candidate(record, "recovered", current_fingerprint)
                    record.update(state="recovered", recovered_at=timestamp(), reconciled_at=timestamp(), restored=current_fingerprint)
                    atomic_json(self._path(operation_id), record)
                    self.artifacts.write("recovery-result.json", record)
                    self.result["readback"] = current
                    return {"recovered": True, "restored": "baseline", "target": target,
                            "fingerprint": current_fingerprint, "copied_backup": False, "already_restored": True}
                raise OperationError("Managed registry changed after publication began", stage="recovery", code="external_changes")
            backup_readback = self._read(backup)
            backup_fingerprint = self._fingerprint(backup_readback)
            if (backup_fingerprint != {key: record["backup_fingerprint"][key] for key in ("content_sha256", "database_sha256")} or
                    backup_readback.get("owner") != record["baseline"]["owner"]):
                raise OperationError("Verified schematic backup changed", stage="recovery", code="backup_mismatch")
            baseline_fingerprint = {key: record["baseline"][key] for key in ("content_sha256", "database_sha256")}
            copied = record.get("post_copy") == current_fingerprint and current.get("owner") == record["candidate"]["owner"]
            # A unique owner and matching semantic content do not prove that
            # another native process did not save the same cell after copy.
            # Without the native file fingerprint checkpoint, refuse even an
            # explicitly approved overwrite and preserve the backup.
            uncheckpointed_owned = (record.get("post_copy") is None and
                                    record["candidate"]["owner"] != record["baseline"]["owner"] and
                                    current_fingerprint["content_sha256"] == record["candidate"]["content_sha256"] and
                                    current.get("owner") == record["candidate"]["owner"])
            if uncheckpointed_owned:
                raise OperationError("Native copy lacks a durable post-copy fingerprint; recovery cannot exclude an external save",
                                     stage="recovery", code="recovery_unverified")
            checkpointed_restore = (record.get("restore_checkpoint") == current_fingerprint and
                                    current.get("owner") == record["baseline"]["owner"])
            if current_fingerprint == baseline_fingerprint and current.get("owner") == record["baseline"]["owner"]:
                restored = current
                copied = False
            elif checkpointed_restore:
                restored = current
                copied = False
            elif copied:
                record.update(state="restoring", restore_started_at=timestamp(), restore_operation_id=self.result["operation_id"])
                atomic_json(self._path(operation_id), record)
                self._guard(target)
                if self._fingerprint(self._read(target)) != current_fingerprint:
                    raise OperationError("Target changed before backup restoration", stage="recovery", code="external_changes")
                self.backend.copy(backup, target, overwrite=True)
                restored = self._read(target)
                if (self._fingerprint(restored)["content_sha256"] != baseline_fingerprint["content_sha256"] or
                        restored.get("owner") != record["baseline"]["owner"]):
                    raise ValidationError("Restored schematic differs from baseline", stage="recovery", code="restore_mismatch")
                record["restore_checkpoint"] = self._fingerprint(restored)
                atomic_json(self._path(operation_id), record)
            else:
                raise OperationError("Target is neither the baseline nor this operation's owned copy", stage="recovery", code="external_changes")
            restored_fingerprint = self._fingerprint(restored)
            registry = json.loads(registry_path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
            if (registry.get("schema_version") != MANAGED_REGISTRY_SCHEMA or registry.get("owner") != record["baseline"]["owner"] or
                    registry.get("fingerprint") != baseline_fingerprint or file_digest(registry_path) != record["registry_sha256"]):
                raise OperationError("Managed registry changed during recovery", stage="recovery", code="external_changes")
            registry.update(fingerprint=restored_fingerprint, database_sha256=restored_fingerprint["database_sha256"],
                            readback_sha256=restored_fingerprint["content_sha256"], recovery_operation_id=self.result["operation_id"])
            atomic_json(registry_path, registry)
            self._settle_candidate(record, "recovered", restored_fingerprint)
            record.update(state="recovered", recovered_at=timestamp(), restored=restored_fingerprint)
            atomic_json(self._path(operation_id), record)
            self.artifacts.write("recovery-result.json", record)
            if copied:
                self.result["changes"].append({"type": "schematic_baseline_restored", "target": target, "backup": backup})
            self.result["readback"] = restored
            return {"recovered": True, "restored": "baseline", "target": target, "fingerprint": restored_fingerprint,
                    "copied_backup": bool(copied)}
        except Exception as exc:
            self._record_failure(record, exc)
            raise
