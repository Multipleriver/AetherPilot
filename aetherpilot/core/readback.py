"""Stable, vendor-neutral readback normalization and comparison helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


PRIMITIVE_KINDS = {"pdk_device", "native_primitive"}


def primitive_proof_complete(proof, *, validator=None, primitive_kinds=None, require_terminal_order=None):
    """Return whether a primitive leaf has independently checkable evidence.

    The public core validates evidence shape without naming a particular PDK:
    a producer may use any nonempty ``kind`` as long as it proves the master,
    terminal set and a stable definition fingerprint.  Native callers can
    retain the historical allow-list by passing ``primitive_kinds`` (usually
    :data:`PRIMITIVE_KINDS`), or provide a backend-specific ``validator``.
    """
    if validator is not None:
        try:
            return bool(validator(proof))
        except Exception:
            return False
    if not isinstance(proof, dict):
        return False
    kind = proof.get("kind")
    if not isinstance(kind, str) or not kind:
        return False
    if isinstance(primitive_kinds, str):
        allowed_kinds = {primitive_kinds}
    else:
        allowed_kinds = set(primitive_kinds) if primitive_kinds is not None else None
    if allowed_kinds is not None and kind not in allowed_kinds:
        return False
    if proof.get("master_exists") is not True or proof.get("terminal_set_verified") is not True:
        return False
    fingerprint = proof.get("device_def_sha256") or proof.get("primitive_def_sha256")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        return False
    terminal_order = proof.get("terminal_order")
    # Validate an order whenever a producer supplies one.  The generic core
    # does not require an order because some backends prove terminals through
    # a different stable representation.  A strict native policy can request
    # the historical requirement explicitly (or by selecting only the
    # ``native_primitive`` kind).
    if require_terminal_order is None:
        require_terminal_order = allowed_kinds == {"native_primitive"}
    if require_terminal_order or terminal_order is not None:
        if not isinstance(terminal_order, list) or not terminal_order or any(not isinstance(item, str) or not item for item in terminal_order):
            return False
    return True


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def readback_is_complete(value, *, validator=None, primitive_kinds=None, require_terminal_order=None):
    if not isinstance(value, dict):
        return False
    coverage = value.get("completeness", {})
    if not isinstance(coverage, dict) or coverage.get("status") != "complete" or coverage.get("scope", "full") != "full" or coverage.get("issues") or value.get("unsupported"):
        return False
    if "records" in value:
        if not isinstance(value["records"], list) or not value["records"]:
            return False
        paths = []
        for record in value["records"]:
            if not isinstance(record, dict) or not isinstance(record.get("path"), list):
                return False
            paths.append(canonical(record["path"]))
            if record.get("status") == "complete":
                if not readback_is_complete(record.get("readback"), validator=validator, primitive_kinds=primitive_kinds, require_terminal_order=require_terminal_order):
                    return False
            elif record.get("status") == "primitive":
                proof = record.get("primitive")
                if not primitive_proof_complete(proof, validator=validator, primitive_kinds=primitive_kinds, require_terminal_order=require_terminal_order):
                    return False
            else:
                return False
        if "[]" not in paths or len(paths) != len(set(paths)):
            return False
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("unsupported") is True:
                return False
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return True


def stable_readback(value):
    if isinstance(value, dict) and isinstance(value.get("readback"), dict) and "snapshot_id" in value:
        errors = validate_snapshot(value)
        if errors:
            raise ValueError("snapshot integrity validation failed: " + "; ".join(errors))
        value = value["readback"]
    if not isinstance(value, dict):
        raise ValueError("readback must be an object")
    result = copy.deepcopy(value)
    result.setdefault("schema_version", 2)
    result.setdefault("completeness", {"status": "unknown", "issues": [{"kind": "completeness", "reason": "producer did not declare coverage"}]})
    result.setdefault("hierarchy", {"path": [], "children": []})
    result.setdefault("nets_detail", [])
    result.setdefault("labels", [])
    result.setdefault("texts", [])
    result.setdefault("figures", [])
    result.setdefault("unsupported", [])
    result.setdefault("source", {})
    for collection in ("instances", "pins", "nets", "wires"):
        result.setdefault(collection, [])
    if "records" in result:
        if not isinstance(result["records"], list):
            raise ValueError("hierarchy records must be an array")
        for record in result["records"]:
            if not isinstance(record, dict) or not isinstance(record.get("path"), list):
                raise ValueError("hierarchy record requires an instance path")
            if "readback" in record:
                record["readback"] = stable_readback(record["readback"])
        result["records"].sort(key=lambda record: canonical(record["path"]))
    for key in ("instances", "pins", "wires", "nets", "nets_detail", "labels", "texts", "unsupported"):
        if isinstance(result.get(key), list):
            result[key] = sorted(result[key], key=lambda item: canonical(item))
    for collection, field, prefix in (("wires", "wire_id", "wire"), ("labels", "label_id", "label"), ("texts", "text_id", "text"), ("figures", "figure_id", "figure")):
        occurrences = {}
        for item in result.get(collection, []):
            if not isinstance(item, dict) or field in item:
                continue
            base = digest({key: value for key, value in item.items() if key != field})[:20]
            occurrence = occurrences.get(base, 0)
            occurrences[base] = occurrence + 1
            item[field] = "%s-%s-%d" % (prefix, base, occurrence)
    for key in ("instances", "pins", "wires", "nets", "nets_detail", "labels", "texts", "figures", "unsupported"):
        if isinstance(result.get(key), list):
            result[key].sort(key=canonical)
    return result


def make_snapshot(readback, *, source_kind="saved", native_copy=None, dependencies=None, options=None):
    normalized = stable_readback(readback)
    payload = {
        "schema_version": 1,
        "snapshot_id": uuid.uuid4().hex,
        "created_at": timestamp(),
        "source_kind": source_kind,
        "content_sha256": digest(normalized),
        "readback": normalized,
        "native_copy": native_copy or {},
        "dependencies": dependencies or [],
        "options": options or {},
    }
    return payload


def _index(items, key):
    if not isinstance(items, list):
        raise ValueError("readback collections must be arrays")
    result = {}
    for item in items:
        if not isinstance(item, dict) or key not in item:
            raise ValueError("readback object lacks identity: %s" % key)
        identity = canonical(item[key])
        if identity in result:
            raise ValueError("readback object identity is ambiguous: %s" % key)
        result[identity] = item
    return result


def _changes(before, after, collection, key):
    left = _index(before.get(collection, []), key)
    right = _index(after.get(collection, []), key)
    changes = []
    for item_key in sorted(left.keys() - right.keys()):
        changes.append({"kind": "removed", "collection": collection, "key": item_key, "before": left[item_key]})
    for item_key in sorted(right.keys() - left.keys()):
        changes.append({"kind": "added", "collection": collection, "key": item_key, "after": right[item_key]})
    for item_key in sorted(left.keys() & right.keys()):
        if left[item_key] != right[item_key]:
            changes.append({"kind": "changed", "collection": collection, "key": item_key, "before": left[item_key], "after": right[item_key]})
    return changes


def diff_readbacks(before, after, *, validator=None, primitive_kinds=None, require_terminal_order=None):
    left = stable_readback(before)
    right = stable_readback(after)
    categories = {key: [] for key in ("electrical", "parameters", "geometry", "text", "metadata")}
    field_categories = {"master": "electrical", "connections": "electrical", "net": "electrical", "num_bits": "electrical",
                        "direction": "electrical", "parameters": "parameters", "parameter_provenance": "parameters", "netset_overrides": "electrical",
                        "origin": "geometry", "position": "geometry", "orientation": "geometry", "points": "geometry",
                        "placement_status": "geometry", "text": "text"}
    collections = {"instances": ("name", "electrical"), "pins": ("name", "electrical"), "nets_detail": ("name", "electrical"),
                   "wires": ("wire_id", "geometry"), "labels": ("label_id", "text"), "texts": ("text_id", "text"), "figures": ("figure_id", "geometry")}
    for collection, (identity, default) in collections.items():
        for change in _changes(left, right, collection, identity):
            if change["kind"] != "changed":
                categories[default].append(change)
                continue
            previous, current = change["before"], change["after"]
            for field in sorted(previous.keys() | current.keys()):
                if previous.get(field) != current.get(field) or (field in previous) != (field in current):
                    categories[field_categories.get(field, default if collection == "nets_detail" else "metadata")].append({
                        "kind": "changed", "collection": collection, "key": previous[identity], "field": field,
                        "before": previous.get(field), "after": current.get(field)})
    left_records = _index(left.get("records", []), "path")
    right_records = _index(right.get("records", []), "path")
    for path in sorted(left_records.keys() | right_records.keys()):
        previous, current = left_records.get(path), right_records.get(path)
        if previous == current:
            continue
        if previous and current and isinstance(previous.get("readback"), dict) and isinstance(current.get("readback"), dict):
            nested = diff_readbacks(previous["readback"], current["readback"], validator=validator, primitive_kinds=primitive_kinds, require_terminal_order=require_terminal_order)
            for category in categories:
                categories[category].extend({**change, "hierarchy_path": current["path"]} for change in nested[category])
            previous = {key: item for key, item in previous.items() if key != "readback"}
            current = {key: item for key, item in current.items() if key != "readback"}
        if previous != current:
            categories["metadata"].append({"kind": "changed", "collection": "hierarchy_record", "key": json.loads(path), "before": previous, "after": current})
    for key in sorted((left.keys() | right.keys()) - set(collections) - {"records"}):
        if left.get(key) != right.get(key) or (key in left) != (key in right):
            category = "electrical" if key in {"nets", "inherited_connections"} else "metadata"
            categories[category].append({"kind": "changed", "collection": key, "before": left.get(key), "after": right.get(key)})
    result = {
        "schema_version": 1,
        "before_sha256": digest(left),
        "after_sha256": digest(right),
        **categories,
        "counts": {key: len(items) for key, items in categories.items()},
        "complete": all(readback_is_complete(item, validator=validator, primitive_kinds=primitive_kinds, require_terminal_order=require_terminal_order) for item in (left, right)),
        "matching": "names and hierarchy paths; anonymous objects use content IDs, not native identity",
    }
    result["changed"] = any(result["counts"].values())
    return result


def validate_snapshot(snapshot, *, require_complete=False, validator=None, primitive_kinds=None,
                      require_terminal_order=None, source_kinds=None):
    errors = []
    if not isinstance(snapshot, dict):
        return ["snapshot must be an object"]
    if snapshot.get("schema_version") != 1:
        errors.append("snapshot schema is unsupported")
    if not isinstance(snapshot.get("snapshot_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot["snapshot_id"]):
        errors.append("snapshot_id is invalid")
    source_kind = snapshot.get("source_kind")
    if not isinstance(source_kind, str) or not source_kind:
        errors.append("source_kind is invalid")
    elif source_kinds is not None and source_kind not in ({source_kinds} if isinstance(source_kinds, str) else set(source_kinds)):
        errors.append("source_kind is not accepted by the configured backend")
    try:
        created = datetime.fromisoformat(snapshot["created_at"])
        if created.tzinfo is None:
            errors.append("created_at requires a timezone")
    except (ValueError, TypeError, KeyError):
        errors.append("created_at is invalid")
    readback = snapshot.get("readback")
    if not isinstance(readback, dict):
        errors.append("readback is missing")
    elif require_complete:
        if not readback_is_complete(readback, validator=validator, primitive_kinds=primitive_kinds, require_terminal_order=require_terminal_order):
            errors.append("readback is incomplete")
    if isinstance(readback, dict) and isinstance(readback.get("records"), list):
        incomplete = [item for item in readback["records"] if not isinstance(item, dict) or not (
            item.get("status") == "complete" or item.get("status") == "primitive" and primitive_proof_complete(item.get("primitive"), validator=validator, primitive_kinds=primitive_kinds, require_terminal_order=require_terminal_order))]
        if require_complete and incomplete:
            errors.append("hierarchy contains incomplete records")
    try:
        if not isinstance(snapshot.get("content_sha256"), str) or snapshot["content_sha256"] != digest(readback):
            errors.append("snapshot content hash does not match readback")
    except (ValueError, TypeError):
        errors.append("readback is not finite JSON")
    if not isinstance(snapshot.get("options"), dict) or not isinstance(snapshot.get("native_copy"), dict) or not isinstance(snapshot.get("dependencies"), list):
        errors.append("snapshot metadata is malformed")
    return errors


__all__ = [
    "PRIMITIVE_KINDS", "canonical", "digest", "diff_readbacks", "make_snapshot",
    "primitive_proof_complete", "readback_is_complete", "stable_readback",
    "timestamp", "validate_snapshot",
]
