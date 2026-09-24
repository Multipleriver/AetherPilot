"""Local native-view candidates, optimistic concurrency, and explicit publication."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .artifacts import atomic_json, timestamp
from .contract import digest, identifier
from .errors import OperationError, ValidationError


PROTECTED_LIBRARIES = {"018um_PDK", "reference_pdk", "basic", "analog", "analogLib", "amsLib", "symbol", "aetherLib", "hes"}

# The first implementation used ``schema_version=1`` for two incompatible
# registry shapes: the direct native apply path stored flat
# ``readback_sha256``/``database_sha256`` fields, while candidate publication
# stored a nested ``fingerprint`` object.  A registry version must describe a
# single wire format; otherwise a freshly created, managed view is mistaken
# for an unmanaged import on the next change operation.  Version 1 is kept as
# a historical format only and is never silently adopted.
MANAGED_REGISTRY_SCHEMA = 2
# Candidate journals are a separate, per-operation envelope.  Keep their
# schema explicit so it cannot be confused with the ownership registry wire
# format above; changing registry compatibility must not silently reinterpret
# an in-flight candidate.
CANDIDATE_SCHEMA = 1


def registry_fingerprint(readback, content):
    """Return the canonical ownership fingerprint for a saved readback.

    ``content`` is supplied by the domain backend so schematic and layout
    semantic projections remain distinct.  The database hash is deliberately
    retained alongside the semantic hash: the former detects native file
    replacement while the latter detects meaningful design changes.
    """
    database_sha256 = readback.get("database_sha256") if isinstance(readback, dict) else None
    if not isinstance(database_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", database_sha256):
        raise ValidationError("A managed registry requires a native database fingerprint", stage="ownership", code="ownership_record_incomplete")
    return {"content_sha256": digest(content(readback)), "database_sha256": database_sha256}


def managed_registry_matches(record, readback, fingerprint, *, domain=None, target=None):
    """Validate the shared v2 registry shape and current readback identity."""
    if not isinstance(record, dict) or record.get("schema_version") != MANAGED_REGISTRY_SCHEMA:
        return False
    stored = record.get("fingerprint")
    if not isinstance(stored, dict) or set(stored) != {"content_sha256", "database_sha256"} or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in stored.values()):
        return False
    if domain is not None and record.get("domain") != domain:
        return False
    if target is not None:
        expected_target = {key: target[key] for key in ("library", "cell", "view")}
        if record.get("target") != expected_target:
            return False
    owner = readback.get("owner") if isinstance(readback, dict) else None
    return bool(owner) and record.get("owner") == owner and stored == fingerprint


def writable_target(target):
    if not isinstance(target, dict) or not {"library", "cell", "view"} <= target.keys() or target.keys() - {"library", "cell", "view", "technology"}:
        raise ValidationError("Invalid publication target", stage="plan", code="invalid_target")
    for name in target.values():
        identifier(name)
    if target["library"] in PROTECTED_LIBRARIES or target.get("technology", "018um_PDK") != "018um_PDK":
        raise OperationError("Technology and system libraries are read-only", stage="ownership", code="protected_library")


class ViewTransactions:
    def __init__(self, backend, artifacts, result, *, root=None):
        self.backend = backend
        self.artifacts = artifacts
        self.result = result
        self.root = Path(root) if root is not None else Path.cwd() / ".aetherpilot"

    def _event(self, stage, **details):
        self.result["stage"] = stage
        self.artifacts.event(stage, "started", **details)
        self.artifacts.write("checkpoint.json", self.result)

    def _candidate_path(self, candidate_id):
        if not isinstance(candidate_id, str) or not re.fullmatch(r"[a-f0-9]{32}", candidate_id):
            raise ValidationError("Invalid candidate ID", stage="plan", code="invalid_candidate_id")
        return self.root / "candidates" / (candidate_id + ".json")

    def _registry_path(self, target):
        return self.root / "managed" / (digest({key: target[key] for key in ("library", "cell", "view")}) + ".json")

    def _read(self, target):
        value = self.backend.read(target)
        coverage = value.get("completeness", {})
        if coverage.get("status") != "complete" or coverage.get("issues") or value.get("unsupported"):
            raise ValidationError("A native transaction requires complete readback", stage="readback", code="incomplete_readback")
        if not value.get("database_sha256") or value.get("source", {}).get("modified") is not False:
            raise ValidationError("A native transaction requires an immutable saved baseline", stage="readback", code="unsaved_baseline")
        return value

    def fingerprint(self, value):
        return registry_fingerprint(value, self.backend.content)

    def _guard(self, target):
        if self.backend.domain == "schematic":
            from .schematic_recovery import pending_schematic_recovery

            pending = pending_schematic_recovery(self.root, target)
            if pending is not None:
                raise OperationError("Schematic has an unresolved publication; recover it before editing", stage="ownership",
                                     code="recovery_required", details={"publication_operation_id": pending})
        self.backend.library.require_unlocked(target)
        finder = getattr(self.backend.api, "dbFindCV", None)
        if callable(finder) and finder(target["library"], target["cell"], target["view"]) is not None:
            raise OperationError("An open editor view cannot be published over", stage="ownership", code="user_view_open")

    def _assert_baseline(self, target, expected):
        if self.backend.domain == "schematic":
            from .schematic_recovery import pending_schematic_recovery

            pending = pending_schematic_recovery(self.root, target)
            if pending is not None:
                raise OperationError("Schematic has an unresolved publication; recover it before editing", stage="ownership",
                                     code="recovery_required", details={"publication_operation_id": pending})
        exists = self.backend.library.exists(target)
        if expected is None:
            if exists:
                raise OperationError("Publication target appeared after preparation", stage="publish", code="external_changes")
            return None
        if not exists:
            raise OperationError("Baseline disappeared after preparation", stage="publish", code="external_changes")
        self._guard(target)
        current = self._read(target)
        if self.fingerprint(current) != expected:
            raise OperationError("Baseline changed after preparation", stage="publish", code="external_changes")
        return current

    def _is_managed(self, target, current):
        try:
            record = json.loads(self._registry_path(target).read_text())
        except (OSError, ValueError):
            return False
        return managed_registry_matches(record, current, self.fingerprint(current), domain=self.backend.domain, target=target)

    def _registry_schema(self, target):
        """Return the on-disk registry schema, or ``None`` only when absent."""
        path = self._registry_path(target)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            return "invalid"
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return "invalid"
        if not isinstance(value, dict):
            return "invalid"
        schema = value.get("schema_version", "invalid")
        if schema != MANAGED_REGISTRY_SCHEMA:
            return schema
        fingerprint = value.get("fingerprint")
        expected_target = {key: target[key] for key in ("library", "cell", "view")}
        if (value.get("domain") != self.backend.domain or value.get("target") != expected_target or not value.get("owner") or
                not isinstance(fingerprint, dict) or set(fingerprint) != {"content_sha256", "database_sha256"} or
                any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in fingerprint.values())):
            return "invalid"
        return schema

    def prepare(self, plan, *, base_sha256=None):
        plan = self.backend.validate(plan)
        target = plan["target"]
        writable_target(target)
        if self.backend.domain == "schematic":
            from .schematic_recovery import pending_schematic_recovery

            pending = pending_schematic_recovery(self.root, target)
            if pending is not None:
                raise OperationError("Schematic has an unresolved publication; recover it before editing", stage="ownership",
                                     code="recovery_required", details={"publication_operation_id": pending})
        self._event("candidate_baseline")
        before = None
        if self.backend.library.exists(target):
            self._guard(target)
            before = self._read(target)
        elif plan["mode"] == "modify":
            raise OperationError("modify requires an existing view", stage="ownership", code="target_missing")
        baseline = self.fingerprint(before) if before is not None else None
        if base_sha256 is not None and (baseline is None or base_sha256 != baseline["content_sha256"]):
            raise OperationError("Requested baseline fingerprint is stale", stage="candidate_baseline", code="external_changes")
        self.backend.library.ensure(target)
        candidate_id = uuid.uuid4().hex
        staging = {**target, "cell": "APstage_" + candidate_id}
        self._event("candidate_build", candidate_id=candidate_id)
        self.backend.materialize(plan, staging, candidate_id, self.artifacts)
        self.result["changes"].append({"type": "candidate_view_created", "target": staging})
        self._event("candidate_readback")
        readback = self._read(staging)
        self.artifacts.write("candidate-readback.json", readback)
        validation = self.backend.compare(plan, readback)
        self._assert_baseline(target, baseline)
        record = {"schema_version": CANDIDATE_SCHEMA, "candidate_id": candidate_id, "state": "prepared", "created_at": timestamp(),
                  "domain": target["view"], "target": target, "staging": staging, "plan": plan,
                  "baseline": baseline, "candidate": self.fingerprint(readback), "validation": validation,
                  "native_verified": True, "publishable": True, "base_managed": before is not None and self._is_managed(target, before)}
        atomic_json(self._candidate_path(candidate_id), record)
        self.artifacts.write("candidate.json", record)
        if before is not None:
            self.artifacts.write("baseline-readback.json", before)
            self.artifacts.write("candidate-diff.json", self.backend.diff(before, readback))
        return record

    def load(self, candidate_id):
        try:
            record = json.loads(self._candidate_path(candidate_id).read_text())
        except (OSError, ValueError) as exc:
            raise OperationError("Candidate is missing or invalid", stage="candidate", code="candidate_missing") from exc
        if record.get("schema_version") != CANDIDATE_SCHEMA or record.get("candidate_id") != candidate_id or record.get("domain") != self.backend.domain:
            raise ValidationError("Candidate identity or domain mismatch", stage="candidate", code="invalid_candidate")
        return record

    def publish(self, candidate_id, *, approved, candidate_sha256, base_sha256=None, destination=None, allow_unmanaged=False):
        if approved is not True or type(allow_unmanaged) is not bool:
            raise OperationError("Publication requires explicit approval", stage="publish", code="approval_required")
        record = self.load(candidate_id)
        if record["state"] != "prepared":
            raise OperationError("Candidate cannot be published in its current state", stage="publish", code="candidate_state", details={"state": record["state"]})
        expected_base = record["baseline"]["content_sha256"] if record["baseline"] else None
        if candidate_sha256 != record["candidate"]["content_sha256"] or base_sha256 != expected_base:
            raise OperationError("Approval does not bind the current candidate and baseline", stage="publish", code="approval_mismatch")
        self._event("publish_preflight")
        current = self._assert_baseline(record["target"], record["baseline"])
        candidate = self._read(record["staging"])
        if self.fingerprint(candidate) != record["candidate"] or candidate.get("owner") != candidate_id:
            raise OperationError("Candidate changed after validation", stage="publish", code="candidate_changed")
        target = dict(destination or record["target"])
        writable_target(target)
        if target.get("view") != record["domain"] or target.get("technology", "018um_PDK") != record["target"]["technology"]:
            raise ValidationError("Publication cannot change view type or technology", stage="publish", code="invalid_target")
        target.setdefault("technology", record["target"]["technology"])
        same_target = all(target[key] == record["target"][key] for key in ("library", "cell", "view"))
        if same_target and current is not None and not self._is_managed(target, current) and not allow_unmanaged:
            # A v1 registry is not an unmanaged import: it is an ambiguous
            # historical record written by an incompatible implementation.
            # Refuse rather than silently creating a version or taking
            # ownership without a verified baseline.
            schema = self._registry_schema(target)
            if schema is not None and schema != MANAGED_REGISTRY_SCHEMA:
                raise OperationError("Ownership registry schema is unsupported; use a new target or an explicit migration", stage="ownership", code="ownership_record_incomplete", details={"schema_version": schema, "expected": MANAGED_REGISTRY_SCHEMA})
            target["cell"] = target["cell"][:60] + "_v_" + candidate_id[:12]
            same_target = False
        if not same_target and self.backend.library.exists(target):
            raise OperationError("New-version destination already exists", stage="publish", code="target_exists")
        if self.backend.domain == "schematic":
            from .schematic_recovery import pending_schematic_recovery

            pending = pending_schematic_recovery(self.root, target)
            if pending is not None:
                raise OperationError("Schematic destination has an unresolved publication", stage="ownership",
                                     code="recovery_required", details={"publication_operation_id": pending})
        self.backend.library.ensure(target)
        backup = None
        if same_target and current is not None:
            backup = {**target, "cell": "APbackup_" + candidate_id}
            self.backend.copy(target, backup)
            if digest(self.backend.content(self._read(backup))) != record["baseline"]["content_sha256"]:
                raise ValidationError("Backup differs from the baseline", stage="backup", code="backup_mismatch")
            self.result["changes"].append({"type": "backup_created", "target": backup})
        self._assert_baseline(record["target"], record["baseline"])
        if not same_target:
            self._assert_baseline(target, None)
        recovery = None
        if self.backend.domain == "schematic" and same_target and current is not None and record.get("base_managed"):
            from .schematic_recovery import SchematicRecovery

            recovery = SchematicRecovery(self.backend, Path.cwd(), self.artifacts, self.result)
            recovery.begin(self.result["operation_id"], target=target, staging=record["staging"], backup=backup,
                           baseline=current, candidate=candidate)
        record.update(state="publishing", destination=target, backup=backup, publication_operation_id=self.result["operation_id"])
        atomic_json(self._candidate_path(candidate_id), record)
        self._event("publish", target=target, backup=backup)
        try:
            self.backend.copy(record["staging"], target, overwrite=same_target and current is not None)
            actual = self._read(target)
            if recovery is not None:
                recovery.checkpoint(self.result["operation_id"], actual)
            self.backend.compare(record["plan"], actual)
            if self.backend.content(actual) != self.backend.content(candidate):
                raise ValidationError("Published view differs from the validated candidate", stage="publish", code="publish_mismatch")
        except Exception as original:
            if recovery is not None:
                try:
                    recovery.recover(self.result["operation_id"], approved=True, automatic=True)
                except Exception as recovery_error:
                    record.update(state="recovery_required", recovery_operation_id=self.result["operation_id"], publishable=False)
                    atomic_json(self._candidate_path(candidate_id), record)
                    raise OperationError("Schematic publication requires explicit recovery", stage="recovery", code="recovery_required",
                                         details={"publication_operation_id": self.result["operation_id"], "original": type(original).__name__,
                                                  "recovery": type(recovery_error).__name__,
                                                  "recovery_code": getattr(recovery_error, "code", None)}) from original
                record.update(state="recovered", recovery_operation_id=self.result["operation_id"], publishable=False)
                atomic_json(self._candidate_path(candidate_id), record)
                raise
            record.update(state="recovery_required", publishable=False)
            atomic_json(self._candidate_path(candidate_id), record)
            self.artifacts.write("publication-recovery.json", record)
            raise
        try:
            atomic_json(self._registry_path(target), {"schema_version": MANAGED_REGISTRY_SCHEMA, "domain": self.backend.domain,
                        "target": {key: target[key] for key in ("library", "cell", "view")}, "owner": actual["owner"],
                        "plan": record["plan"], "fingerprint": self.fingerprint(actual), "candidate_id": candidate_id,
                        "operation_id": self.result["operation_id"], "backup": backup})
            self.result["changes"].append({"type": "view_published", "target": target, "candidate_id": candidate_id})
            record.update(state="published", published_at=timestamp(), published=self.fingerprint(actual), publishable=False)
            atomic_json(self._candidate_path(candidate_id), record)
            if recovery is not None:
                recovery.published(self.result["operation_id"])
            self.artifacts.write("publication.json", record)
            self.artifacts.write("readback.json", actual)
        except Exception as original:
            if recovery is None:
                raise
            try:
                outcome = ({"publication_committed": True} if recovery._load(self.result["operation_id"])["state"] == "published" else
                           recovery.recover(self.result["operation_id"], approved=True, automatic=True))
            except Exception as recovery_error:
                record.update(state="recovery_required", recovery_operation_id=self.result["operation_id"], publishable=False)
                atomic_json(self._candidate_path(candidate_id), record)
                raise OperationError("Schematic publication requires explicit recovery", stage="recovery", code="recovery_required",
                                     details={"publication_operation_id": self.result["operation_id"], "original": type(original).__name__,
                                              "recovery": type(recovery_error).__name__,
                                              "recovery_code": getattr(recovery_error, "code", None)}) from original
            if outcome.get("publication_committed"):
                raise OperationError("Schematic was published, but publication evidence could not be finalized", stage="artifacts",
                                     code="publication_committed_evidence_failure",
                                     details={"publication_operation_id": self.result["operation_id"], "original": type(original).__name__}) from original
            record.update(state="recovered", recovery_operation_id=self.result["operation_id"], publishable=False)
            atomic_json(self._candidate_path(candidate_id), record)
            raise
        return actual
