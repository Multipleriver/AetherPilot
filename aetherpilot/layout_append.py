"""Append validated layout objects while preserving every baseline object."""

import copy
import re

from .errors import OperationError, ValidationError
from .layout import compare_layout_plan, digest, layout_content, require_complete_layout, validate_layout_plan


def validate_layout_append(source):
    if not isinstance(source, dict) or source.get("operation") != "layout.append" or source.get("mode", "modify") != "modify":
        raise ValidationError("Invalid layout append operation", stage="plan", code="invalid_layout_append")
    baseline = source.get("base_sha256")
    if not isinstance(baseline, str) or not re.fullmatch(r"[a-f0-9]{64}", baseline):
        raise ValidationError("Layout append requires a baseline content SHA-256", stage="plan", code="invalid_baseline")
    fields = {key: value for key, value in source.items() if key != "base_sha256"}
    plan = validate_layout_plan({**fields, "operation": "layout.apply", "mode": "modify"})
    if plan.get("layer_visibility"):
        raise ValidationError("Layout append is a database operation; use layout.session for visibility", stage="plan", code="invalid_layout_append")
    return {**plan, "operation": "layout.append", "base_sha256": baseline}


def prepare_layout_append(plan, baseline):
    try:
        current = require_complete_layout(baseline)
    except ValueError as error:
        raise ValidationError("Layout append requires complete baseline readback", stage="plan", code="incomplete_layout_readback") from error
    content = layout_content(current)
    if digest(content) != plan["base_sha256"]:
        raise OperationError("Layout append baseline is stale", stage="plan", code="external_changes")
    if current["technology"] != plan["target"]["technology"]:
        raise ValidationError("Layout append cannot change technology", stage="plan", code="technology_mismatch")
    collections = ("instances", "mosaics", "paths", "rects", "polygons", "labels", "vias", "pins")
    added_names = {item["name"] for collection in collections for item in plan[collection]}
    added_names.update(item["name"] + "__pin_figure" for item in plan["pins"])
    existing_names = {item.get("name") for collection in ("instances", "mosaics", "shapes", "vias", "pins") for item in current[collection]}
    collisions = sorted(added_names & existing_names)
    if collisions:
        raise ValidationError("Layout append cannot replace existing objects", stage="plan", code="layout_name_collision", details={"names": collisions})
    count = sum(len(current[collection]) for collection in ("instances", "mosaics", "shapes", "vias", "pins", "nets"))
    count += sum(len(plan[collection]) for collection in (*collections, "nets"))
    if count > 10000:
        raise ValidationError("Combined layout exceeds the local object budget", stage="plan", code="resource_limit")
    return {**plan, "_baseline_content": content}


def compare_layout_append(plan, actual):
    try:
        value = require_complete_layout(actual)
    except ValueError as error:
        raise ValidationError("Appended layout readback is incomplete", stage="readback", code="incomplete_layout_readback") from error
    added = copy.deepcopy(value)
    remaining = copy.deepcopy(value)
    existing_nets = {item["name"] for item in plan["_baseline_content"]["nets"]}
    shape_names = {item["name"] for collection in ("paths", "rects", "polygons", "labels") for item in plan[collection]}
    pin_names = {item["name"] for item in plan["pins"]}
    for collection in ("instances", "mosaics", "shapes", "vias", "pins", "nets"):
        if collection == "shapes":
            predicate = lambda item: item.get("name") in shape_names or item.get("pin") in pin_names
        elif collection == "nets":
            predicate = lambda item: item["name"] in plan["nets"]
        else:
            names = {item["name"] for item in plan[collection]}
            predicate = lambda item: item.get("name") in names
        added[collection] = [item for item in value[collection] if predicate(item)]
        remaining[collection] = [item for item in value[collection] if (item["name"] in existing_nets if collection == "nets" else not predicate(item))]
    requested = {key: value for key, value in plan.items() if key not in {"base_sha256", "_baseline_content"}}
    compare_layout_plan({**requested, "operation": "layout.apply"}, added)
    if layout_content(remaining) != plan["_baseline_content"]:
        raise ValidationError("Layout append changed or lost baseline objects", stage="readback", code="layout_baseline_mismatch")
    return {"ok": True, "checks": ["appended_objects_native_readback", "all_baseline_objects_preserved", "reused_nets_preserved"]}
