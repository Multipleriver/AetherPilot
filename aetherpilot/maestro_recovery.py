"""Verified rollback of interrupted native/logical MDE publications."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .artifacts import atomic_text, file_digest
from .contract import no_duplicate_keys
from .errors import OperationError, ValidationError
from .maestro import _normalize_state, state_digest
from .results import safe_result_path


def recover_transaction(store, target, verify_native):
    journal_path = safe_result_path(store.directory / ".aetherpilot-transaction.json", must_exist=False)
    if not journal_path.exists():
        return {"action": "recover", "target": target, "recovered": False, "reason": "no_pending_transaction"}
    safe_result_path(journal_path, max_bytes=1024 * 1024)
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("MDE recovery journal cannot be parsed", stage="maestro", code="recovery_journal_invalid") from exc
    if not isinstance(journal, dict) or journal.get("target") != target or type(journal.get("created_state")) is not bool:
        raise ValidationError("MDE recovery journal target differs", stage="maestro", code="recovery_journal_invalid")
    name = journal.get("backup")
    if not isinstance(name, str) or not re.fullmatch(r"\.setting-backup-[0-9a-f]{32}", name):
        raise ValidationError("MDE recovery backup name is unsafe", stage="maestro", code="recovery_journal_invalid")
    backup = safe_result_path(store.directory / name, max_bytes=8 * 1024 ** 2)
    if file_digest(backup) != journal.get("native_sha256"):
        raise ValidationError("MDE recovery native backup changed", stage="maestro", code="result_provenance")
    logical = None
    if journal.get("logical_backup") is not None:
        if journal["logical_backup"] != name + ".json":
            raise ValidationError("MDE recovery logical backup name is unsafe", stage="maestro", code="recovery_journal_invalid")
        logical = safe_result_path(store.directory / journal["logical_backup"], max_bytes=16 * 1024 ** 2)
        if file_digest(logical) != journal.get("logical_sha256"):
            raise ValidationError("MDE recovery logical backup changed", stage="maestro", code="result_provenance")
        state = _normalize_state(json.loads(logical.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys))
        if state_digest(state) != journal.get("state_sha256") or state["metadata"].get("native_setting_sha256") != journal["native_sha256"]:
            raise ValidationError("MDE recovery backups describe different states", stage="maestro", code="result_provenance")
    elif not journal["created_state"]:
        raise ValidationError("Existing MDE state lacks its logical recovery backup", stage="maestro", code="recovery_journal_invalid")
    native_path = safe_result_path(store.directory / "setting", max_bytes=8 * 1024 ** 2)
    expected_native = {journal["native_sha256"], journal.get("next_native_sha256")}
    if file_digest(native_path) not in expected_native:
        raise OperationError("Native state changed outside the recorded transaction; recovery will not overwrite it",
                             stage="maestro", code="baseline_changed")
    current_digest = state_digest(store.load())
    if current_digest not in {journal.get("state_sha256"), journal.get("next_state_sha256")}:
        raise OperationError("Logical state changed outside the recorded transaction; recovery will not overwrite it",
                             stage="maestro", code="baseline_changed")
    if journal["created_state"]:
        quarantine = store.directory.with_name(".recovered-" + store.directory.name + "-" + uuid.uuid4().hex)
        store.directory.rename(quarantine)
        return {"action": "recover", "target": target, "recovered": True, "restored": "absent_view", "quarantine": str(quarantine)}
    atomic_text(native_path, backup.read_text(encoding="utf-8"))
    atomic_text(store.path, logical.read_text(encoding="utf-8"))
    if file_digest(native_path) != journal["native_sha256"] or state_digest(store.load()) != journal["state_sha256"]:
        raise OperationError("MDE rollback fingerprints differ", stage="maestro", code="recovery_required")
    native = verify_native()
    if not native.get("valid") or native.get("setting_sha256") != journal["native_sha256"]:
        raise OperationError("MDE rollback failed native reopen verification", stage="maestro", code="recovery_required")
    journal_path.unlink()
    return {"action": "recover", "target": target, "recovered": True, "restored": "baseline",
            "native_setting_sha256": journal["native_sha256"], "state_sha256": journal["state_sha256"]}
