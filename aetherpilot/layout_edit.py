"""Deterministic layout deletion projections; all implicit cleanup is reported."""

import copy
import re

from .errors import ValidationError
from .layout import layout_content, require_complete_layout, select_layout_objects, stable_layout_readback, validate_layout_plan


def validate_layout_edit(source):
    if not isinstance(source, dict) or set(source) - {"schema_version", "operation", "target", "actions", "base_sha256", "mode"} or not {"schema_version", "operation", "target", "actions", "base_sha256"} <= set(source):
        raise ValidationError("Invalid layout edit request", stage="plan", code="invalid_layout_actions")
    if type(source["schema_version"]) is not int or source["schema_version"] != 1 or source["operation"] != "layout.edit" or source.get("mode", "modify") != "modify":
        raise ValidationError("Invalid layout edit operation", stage="plan", code="invalid_layout_actions")
    empty = {key: [] for key in ("instances", "mosaics", "paths", "rects", "polygons", "labels", "vias", "pins", "nets")}
    target = validate_layout_plan({**empty, "schema_version": 1, "operation": "layout.apply", "target": source["target"]})["target"]
    if not isinstance(source["base_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["base_sha256"]):
        raise ValidationError("Layout edits require a baseline content SHA-256", stage="plan", code="invalid_baseline")
    actions = source["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= 1000:
        raise ValidationError("Layout edits require 1–1000 actions", stage="plan", code="invalid_layout_actions")
    allowed = {"select_box": {"operation", "bbox", "inside"}, "select_net": {"operation", "net"},
               "delete_selected": {"operation"}, "clear_selection": {"operation"}, "clear_routing": {"operation"},
               "clear": {"operation"}, "delete_layer": {"operation", "layer", "purpose"}}
    for action in actions:
        if not isinstance(action, dict) or action.get("operation") not in allowed or set(action) - allowed[action["operation"]]:
            raise ValidationError("Unknown layout editing action or field", stage="plan", code="invalid_layout_actions")
        operation = action["operation"]
        if operation == "select_box":
            empty_readback = {"completeness": {"status": "complete", "issues": []}}
            if "bbox" not in action:
                raise ValidationError("select_box requires a bbox", stage="plan", code="invalid_layout_actions")
            try:
                select_layout_objects(empty_readback, bbox=action["bbox"], inside=action.get("inside", True))
            except ValueError as exc:
                raise ValidationError(str(exc), stage="plan", code="invalid_layout_actions") from exc
        elif operation in {"select_net", "delete_layer"}:
            required = ("net",) if operation == "select_net" else ("layer",)
            if any(not isinstance(action.get(key), str) or not action[key] or "\0" in action[key] for key in required) or "purpose" in action and not isinstance(action["purpose"], str):
                raise ValidationError("Invalid net or layer selector", stage="plan", code="invalid_layout_actions")
    return {**copy.deepcopy(source), "target": target, "mode": "modify"}


def project_layout_edits(readback, actions):
    value = require_complete_layout(readback)
    selected = []
    removed = []
    changes = []
    for action in actions:
        operation = action["operation"]
        if operation == "select_box":
            selected = select_layout_objects(value, bbox=action["bbox"], inside=action.get("inside", True))
        elif operation == "select_net":
            selected = select_layout_objects(value, net=action["net"])
        elif operation == "clear_selection":
            selected = []
        else:
            if operation == "delete_selected":
                deleting = selected
            elif operation == "delete_layer":
                deleting = select_layout_objects(value, layer=action["layer"], purpose=action.get("purpose", "drawing"), collections=["shapes"])
            elif operation == "clear_routing":
                deleting = select_layout_objects(value, collections=["shapes"])
            elif operation == "clear":
                deleting = select_layout_objects(value)
            else:
                raise ValueError("unknown layout edit operation")
            for reference in deleting:
                collection = reference["collection"]
                field = "name" if collection in {"instances", "mosaics", "pins"} else "id"
                matches = [item for item in value[collection] if item[field] == reference["key"]]
                for item in matches:
                    removed.append({"collection": collection, "object": copy.deepcopy(item)})
                value[collection] = [item for item in value[collection] if item[field] != reference["key"]]
            live_pin_shapes = {item.get("pin") for item in value["shapes"] if item.get("pin")}
            for pin in list(value["pins"]):
                if pin["name"] not in live_pin_shapes:
                    value["pins"].remove(pin)
                    changes.append({"type": "empty_pin_removed", "name": pin["name"], "terminal": pin.get("terminal")})
            if operation == "clear":
                changes.extend({"type": "net_removed", "name": item["name"]} for item in value["nets"])
                value["nets"] = []
            selected = [reference for reference in selected if reference not in deleting]
            changes.append({"type": operation, "deleted_figures": len(deleting)})
    for collection in ("shapes", "vias", "pins"):
        for item in value[collection]:
            item.pop("id", None)
    value = stable_layout_readback(value)
    return {"readback": value, "content": layout_content(value), "removed": removed, "changes": changes, "selected": selected}


def object_match_key(item):
    if item.get("name"):
        return ("name", item["name"])
    from .layout import canonical

    return ("geometry", canonical({key: item.get(key) for key in ("obj_type", "bbox", "points", "layer", "purpose", "net", "position", "origin", "orientation", "text")}))
