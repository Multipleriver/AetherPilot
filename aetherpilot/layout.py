"""Vendor-neutral layout plans, readback normalization, and comparison."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from .errors import ValidationError


NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")
ORIENTATIONS = {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:meg|[TGMKkmunpf])?$")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _reject(message, **details):
    raise ValidationError(message, stage="plan", code="invalid_layout_plan", details=details)


def _identifier(value, where):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        _reject(f"{where} must be an ASCII identifier")
    if value.startswith(("APstage_", "APbackup_")):
        _reject(f"{where} uses a reserved prefix")
    return value


def _finite(value, where, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _reject(f"{where} must be a finite number")
    if positive and value <= 0:
        _reject(f"{where} must be positive")
    return float(value)


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not NUMBER.fullmatch(str(value)):
        _reject(f"{where} must be a numeric value or engineering literal")
    return str(value)


def _parameters(value, where):
    if not isinstance(value, dict) or len(value) > 256:
        _reject(f"{where} must contain at most 256 parameters")
    for name, item in value.items():
        _identifier(name, where)
        if not isinstance(item, (str, int, float, bool)) or isinstance(item, float) and not math.isfinite(item):
            _reject(f"{where}.{name} must be a finite scalar")
        if isinstance(item, str) and (len(item) > 4096 or any(ord(char) < 32 for char in item)):
            _reject(f"{where}.{name} contains an invalid string")
    return value


def _point(value, grid, where):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _reject(f"{where} must contain two coordinates")
    result = [_finite(item, where) for item in value]
    if any(abs(item) > 1_000_000 or not math.isclose(item / grid, round(item / grid), rel_tol=0, abs_tol=1e-7) for item in result):
        _reject(f"{where} is outside bounds or off grid")
    return result


def _bbox(value, grid, where):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _reject(f"{where} must contain x0,y0,x1,y1")
    result = [_finite(item, where) for item in value]
    if result[0] >= result[2] or result[1] >= result[3]:
        _reject(f"{where} must have positive area")
    _point(result[:2], grid, where)
    _point(result[2:], grid, where)
    return result


def _points(value, grid, where, minimum=2):
    if not isinstance(value, list) or not minimum <= len(value) <= 10000:
        _reject(f"{where} must contain {minimum}–10000 points")
    result = [_point(item, grid, f"{where}[{index}]") for index, item in enumerate(value)]
    for first, second in zip(result, result[1:]):
        if first == second:
            _reject(f"{where} contains a zero-length segment")
    return result


def _named(items, where):
    if not isinstance(items, list) or len(items) > 10000:
        _reject(f"{where} must contain at most 10000 items")
    names = []
    for item in items:
        if not isinstance(item, dict) or "name" not in item:
            _reject(f"{where} item requires name")
        names.append(_identifier(item["name"], f"{where}.name"))
    if len(names) != len(set(names)):
        _reject(f"{where} contains duplicate names")


def _layer_fields(item, where, *, net=True):
    for key in ("layer", "purpose"):
        _identifier(item[key], f"{where}.{key}")
    if net and item.get("net") is not None:
        _identifier(item["net"], f"{where}.net")


def validate_layout_plan(source):
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _reject("Cannot read layout plan", reason=type(exc).__name__)
    if not isinstance(source, dict):
        _reject("layout plan must be an object")
    plan = copy.deepcopy(source)
    required = {"schema_version", "operation", "target", "instances", "mosaics", "paths", "rects", "polygons", "labels", "vias", "pins", "nets"}
    if set(plan) - required - {"mode", "grid", "layer_visibility"} or required - set(plan):
        _reject("Invalid layout plan fields")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or plan["operation"] != "layout.apply":
        _reject("Only schema_version=1 and layout.apply are supported")
    plan.setdefault("mode", "create")
    if plan["mode"] not in {"create", "modify"}:
        _reject("layout mode must be create or modify")
    grid = _finite(plan.get("grid", 0.001), "grid", positive=True)
    if not 1e-9 <= grid <= 1000:
        _reject("grid is outside supported bounds")
    plan["grid"] = grid
    target = plan["target"]
    if not isinstance(target, dict) or set(target) - {"library", "cell", "technology", "view"} or not {"library", "cell", "technology"} <= set(target):
        _reject("Invalid layout target")
    target.setdefault("view", "layout")
    if target["view"] != "layout":
        _reject("layout target view must be layout")
    for key, value in target.items():
        _identifier(value, f"target.{key}")
    for key in ("instances", "mosaics", "paths", "rects", "polygons", "labels", "vias", "pins"):
        _named(plan[key], key)
    nets = plan["nets"]
    if not isinstance(nets, list) or len(nets) > 10000 or any(not isinstance(item, str) for item in nets) or len(nets) != len(set(nets)):
        _reject("nets must be a unique array")
    for net in nets:
        _identifier(net, "net")
    fields = {
        "instances": {"name", "master", "origin", "orientation", "parameters"},
        "mosaics": {"name", "master", "origin", "orientation", "parameters", "rows", "cols", "row_pitch", "col_pitch"},
        "paths": {"name", "layer", "purpose", "net", "points", "width", "style", "begin_extension", "end_extension"},
        "rects": {"name", "layer", "purpose", "net", "bbox"},
        "polygons": {"name", "layer", "purpose", "net", "points"},
        "labels": {"name", "layer", "purpose", "position", "text", "height", "orientation", "justification", "font"},
        "vias": {"name", "definition", "position", "orientation", "parameters", "net"},
        "pins": {"name", "net", "layer", "purpose", "bbox", "direction"},
    }
    names = []
    for collection, allowed in fields.items():
        for item in plan[collection]:
            if set(item) - allowed:
                _reject(f"Unknown {collection} fields", fields=sorted(set(item) - allowed))
            names.append(item["name"])
            if item.get("net") is not None and item["net"] not in nets:
                _reject(f"{collection} refers to an undeclared net")
    if len(names) != len(set(names)):
        _reject("Layout object names must be unique across collections")
    if sum(len(plan[key]) for key in fields) > 10000:
        _reject("Layout object count exceeds the local safety limit")
    for item in plan["instances"]:
        for key in ("name", "origin", "master", "parameters"):
            if key not in item:
                _reject(f"instance requires {key}")
        _identifier(item["name"], "instance.name")
        master = item["master"]
        if not isinstance(master, dict) or set(master) - {"library", "cell", "view"} or not {"library", "cell"} <= set(master):
            _reject("Invalid instance master")
        master.setdefault("view", "layout")
        for key, value in master.items():
            _identifier(value, f"instance.master.{key}")
        item["origin"] = _point(item["origin"], grid, "instance.origin")
        item.setdefault("orientation", "R0")
        if item["orientation"] not in ORIENTATIONS:
            _reject("Unsupported instance orientation")
        if not isinstance(item["parameters"], dict):
            _reject("instance.parameters must be an object")
        item["parameters"] = _parameters(item["parameters"], "instance.parameters")
        if item.get("net") is not None and item["net"] not in nets:
            _reject("instance refers to an undeclared net")
    for item in plan["mosaics"]:
        for key in ("name", "master", "origin", "rows", "cols", "row_pitch", "col_pitch"):
            if key not in item:
                _reject(f"mosaic requires {key}")
        item["origin"] = _point(item["origin"], grid, "mosaic.origin")
        for key in ("rows", "cols"):
            if type(item[key]) is not int or not 1 <= item[key] <= 10000:
                _reject(f"mosaic.{key} is invalid")
        for key in ("row_pitch", "col_pitch"):
            item[key] = _finite(item[key], f"mosaic.{key}", positive=True)
            _point([item[key], 0], grid, f"mosaic.{key}")
        if item["rows"] * item["cols"] > 100000:
            _reject("Mosaic exceeds the local expansion limit")
        item.setdefault("orientation", "R0")
        if item["orientation"] not in ORIENTATIONS:
            _reject("Unsupported mosaic orientation")
        item["parameters"] = _parameters(item.get("parameters", {}), "mosaic.parameters")
        master = item["master"]
        if not isinstance(master, dict) or set(master) - {"library", "cell", "view"} or not {"library", "cell"} <= set(master):
            _reject("Invalid mosaic master")
        master.setdefault("view", "layout")
        for key, value in master.items():
            _identifier(value, f"mosaic.master.{key}")
    for item in plan["paths"]:
        if not {"name", "layer", "purpose", "points", "width"} <= set(item):
            _reject("path requires name/layer/purpose/points/width")
        _layer_fields(item, "path")
        item["points"] = _points(item["points"], grid, "path.points")
        item["width"] = _finite(item["width"], "path.width", positive=True)
        item.setdefault("style", "truncate")
        if item["style"] not in {"truncate", "extend", "round", "variable"}:
            _reject("Unsupported path style")
        for key in ("begin_extension", "end_extension"):
            item[key] = _finite(item.get(key, 0), f"path.{key}")
            if item[key] < 0 or item["style"] != "variable" and item[key] != 0:
                _reject("Path extensions require variable style and nonnegative values")
    for item in plan["rects"]:
        if not {"name", "layer", "purpose", "bbox"} <= set(item):
            _reject("rect requires name/layer/purpose/bbox")
        _layer_fields(item, "rect")
        item["bbox"] = _bbox(item["bbox"], grid, "rect.bbox")
    for item in plan["polygons"]:
        if not {"name", "layer", "purpose", "points"} <= set(item):
            _reject("polygon requires name/layer/purpose/points")
        _layer_fields(item, "polygon")
        item["points"] = _points(item["points"], grid, "polygon.points", minimum=3)
        vertices = item["points"]
        area = sum(first[0] * second[1] - second[0] * first[1] for first, second in zip(vertices, vertices[1:] + vertices[:1]))
        if abs(area) < grid * grid / 2:
            _reject("polygon must have nonzero area")
    for item in plan["labels"]:
        if not {"name", "layer", "purpose", "position", "text"} <= set(item):
            _reject("label requires name/layer/purpose/position/text")
        _layer_fields(item, "label")
        item["position"] = _point(item["position"], grid, "label.position")
        if not isinstance(item["text"], str) or not item["text"] or len(item["text"]) > 4096 or "\0" in item["text"]:
            _reject("label.text must be nonempty")
        item.setdefault("orientation", "R0")
        if item["orientation"] not in ORIENTATIONS:
            _reject("Unsupported label orientation")
        item["height"] = _finite(item.get("height", 0.2), "label.height", positive=True)
        item.setdefault("justification", "lowerLeft")
        item.setdefault("font", "stick")
        if item["justification"] not in {vertical + horizontal for vertical in ("lower", "center", "upper") for horizontal in ("Left", "Center", "Right")}:
            _reject("Unsupported label justification")
        _identifier(item["font"], "label.font")
    for item in plan["vias"]:
        if not {"name", "definition", "position"} <= set(item):
            _reject("via requires name/definition/position")
        _identifier(item["definition"], "via.definition")
        item["position"] = _point(item["position"], grid, "via.position")
        item.setdefault("orientation", "R0")
        if item["orientation"] not in ORIENTATIONS:
            _reject("Unsupported via orientation")
        item.setdefault("parameters", {})
        if not isinstance(item["parameters"], dict):
            _reject("via.parameters must be an object")
        for key, value in item["parameters"].items():
            _identifier(key, "via.parameter")
            if key in {"layer1", "layer2", "impL1", "impL2"}:
                if not isinstance(value, list) or len(value) != (4 if key in {"layer1", "layer2"} else 2):
                    _reject("Via enclosure parameter has an invalid size")
                item["parameters"][key] = [_finite(number, "via.enclosure") for number in value]
            elif key in {"rows", "cols"}:
                if type(value) is not int or not 1 <= value <= 10000:
                    _reject("Via rows/cols must be bounded positive integers")
            else:
                _parameters({key: value}, "via.parameters")
        if item.get("net") is not None and item["net"] not in nets:
            _reject("via refers to an undeclared net")
    for item in plan["pins"]:
        if not {"name", "net", "layer", "purpose", "bbox", "direction"} <= set(item):
            _reject("pin requires name/net/layer/purpose/bbox/direction")
        _layer_fields(item, "pin", net=False)
        _identifier(item["net"], "pin.net")
        if item["net"] not in nets:
            _reject("pin requires a declared net")
        if item["direction"] not in {"input", "output", "inputOutput"}:
            _reject("Unsupported pin direction")
        item["bbox"] = _bbox(item["bbox"], grid, "pin.bbox")
    visibility = plan.get("layer_visibility", {})
    if not isinstance(visibility, dict) or set(visibility) - {"active", "visible", "hidden", "only"}:
        _reject("Invalid layer_visibility")
    if visibility.get("active") is not None:
        if isinstance(visibility["active"], str):
            visibility["active"] = [visibility["active"], "drawing"]
        if not isinstance(visibility["active"], list) or len(visibility["active"]) != 2:
            _reject("active layer requires [layer, purpose]")
        _layer_fields({"layer": visibility["active"][0], "purpose": visibility["active"][1]}, "layer_visibility.active", net=False)
    if "only" in visibility and type(visibility["only"]) is not bool:
        _reject("layer_visibility.only must be boolean")
    for key in ("visible", "hidden"):
        if not isinstance(visibility.get(key, []), list):
            _reject(f"layer_visibility.{key} must be an array")
        for pair in visibility.get(key, []):
            if not isinstance(pair, list) or len(pair) != 2:
                _reject(f"layer_visibility.{key} entries require layer and purpose")
            _layer_fields({"layer": pair[0], "purpose": pair[1]}, f"layer_visibility.{key}", net=False)
    if {tuple(pair) for pair in visibility.get("visible", [])} & {tuple(pair) for pair in visibility.get("hidden", [])}:
        _reject("A layer cannot be both visible and hidden")
    for key in ("instances", "mosaics", "paths", "rects", "polygons", "labels", "vias", "pins"):
        plan[key].sort(key=lambda item: item["name"])
    plan["nets"] = sorted(nets)
    return plan


def stable_layout_readback(value):
    if not isinstance(value, dict):
        raise ValueError("layout readback must be an object")
    result = copy.deepcopy(value)
    result.setdefault("schema_version", 1)
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("unsupported layout readback schema")
    result.setdefault("completeness", {"status": "unknown", "issues": [{"kind": "completeness", "reason": "producer did not declare coverage"}]})
    completeness = result["completeness"]
    if not isinstance(completeness, dict) or completeness.get("status") not in {"complete", "incomplete", "unknown"} or not isinstance(completeness.get("issues", []), list):
        raise ValueError("invalid layout completeness")
    for key in ("instances", "mosaics", "shapes", "vias", "pins", "nets", "layers", "unsupported"):
        result.setdefault(key, [])
        if not isinstance(result[key], list):
            raise ValueError(f"layout {key} must be an array")
    result["nets"] = [{"name": item} if isinstance(item, str) else item for item in result["nets"]]
    result.setdefault("source", {})
    for key in ("instances", "mosaics", "shapes", "vias", "pins", "nets", "layers", "unsupported"):
        if any(not isinstance(item, dict) for item in result[key]):
            raise ValueError(f"layout {key} entries must be objects")
        result[key].sort(key=canonical)
    for collection, prefix in (("shapes", "shape"), ("vias", "via"), ("pins", "pin")):
        occurrences = {}
        for item in result[collection]:
            if "id" not in item:
                base = digest({"name": item["name"]} if item.get("name") else item)[:20]
                index = occurrences.get(base, 0)
                occurrences[base] = index + 1
                item["id"] = f"{prefix}-{base}-{index}"
    canonical(result)
    return result


def layout_content(value):
    normalized = stable_layout_readback(value)
    return {key: normalized.get(key) for key in ("technology", "instances", "mosaics", "shapes", "vias", "pins", "nets")}


def require_complete_layout(value):
    normalized = stable_layout_readback(value)
    if normalized["completeness"].get("status") != "complete" or normalized["completeness"].get("issues") or normalized["unsupported"]:
        raise ValueError("cannot use incomplete layout readback as authoritative evidence")
    for collection in ("instances", "mosaics", "pins", "nets"):
        identities = [item.get("name") for item in normalized[collection]]
        if any(not isinstance(name, str) or not name for name in identities) or len(identities) != len(set(identities)):
            raise ValueError(f"ambiguous layout {collection} identity")
    for collection in ("shapes", "vias"):
        identities = [item.get("id") for item in normalized[collection]]
        if len(identities) != len(set(identities)):
            raise ValueError(f"ambiguous layout {collection} identity")
        names = [item["name"] for item in normalized[collection] if item.get("name")]
        if len(names) != len(set(names)):
            raise ValueError(f"ambiguous layout {collection} name")
    return normalized


def _collection_changes(before, after, collection, key):
    left = {canonical(item.get(key)): item for item in before.get(collection, [])}
    right = {canonical(item.get(key)): item for item in after.get(collection, [])}
    if len(left) != len(before.get(collection, [])) or len(right) != len(after.get(collection, [])):
        raise ValueError(f"ambiguous layout {collection} identity")
    changes = []
    for identity in sorted(left.keys() - right.keys()):
        changes.append({"kind": "removed", "collection": collection, "key": identity, "before": left[identity]})
    for identity in sorted(right.keys() - left.keys()):
        changes.append({"kind": "added", "collection": collection, "key": identity, "after": right[identity]})
    for identity in sorted(left.keys() & right.keys()):
        if left[identity] != right[identity]:
            changes.append({"kind": "changed", "collection": collection, "key": identity, "before": left[identity], "after": right[identity]})
    return changes


def diff_layout_readbacks(before, after):
    left = require_complete_layout(before)
    right = require_complete_layout(after)
    geometry = []
    for collection in ("instances", "mosaics", "shapes", "vias"):
        geometry.extend(_collection_changes(left, right, collection, "name" if collection in {"instances", "mosaics"} else "id"))
    electrical = _collection_changes(left, right, "pins", "name") + _collection_changes(left, right, "nets", "name")
    metadata = [] if left.get("technology") == right.get("technology") else [{"kind": "changed", "field": "technology", "before": left.get("technology"), "after": right.get("technology")}]
    return {"schema_version": 1, "before_sha256": digest(left), "after_sha256": digest(right), "geometry": geometry, "electrical": electrical, "metadata": metadata,
            "counts": {"geometry": len(geometry), "electrical": len(electrical), "metadata": len(metadata)}, "complete": True}


def compare_layout_plan(plan, actual):
    expected = validate_layout_plan(plan)
    try:
        found = require_complete_layout(actual)
    except ValueError as exc:
        raise ValidationError("Layout readback is incomplete or ambiguous", stage="readback", code="incomplete_layout_readback", details={"reason": str(exc)}) from exc
    failures = []
    if found.get("technology") != expected["target"]["technology"]:
        failures.append("technology")
    for collection in ("instances", "mosaics", "vias", "pins"):
        actual_items = {item.get("name"): item for item in found[collection]}
        if set(actual_items) != {item["name"] for item in expected[collection]}:
            failures.append(f"{collection}:names")
        for item in expected[collection]:
            actual_item = actual_items.get(item["name"], {})
            for key, value in item.items():
                if key == "parameters":
                    for parameter, desired in value.items():
                        if not equivalent_parameter(actual_item.get("parameters", {}).get(parameter), desired):
                            failures.append(f"{collection}:{item['name']}:parameters:{parameter}")
                elif actual_item.get(key) != value:
                    failures.append(f"{collection}:{item['name']}:{key}")
    actual_shapes = {item.get("name"): item for item in found.get("shapes", [])}
    expected_names = {item["name"] for key in ("paths", "rects", "polygons", "labels") for item in expected[key]}
    actual_names = {item.get("name") for item in found["shapes"] if not item.get("pin")}
    if expected_names != actual_names:
        failures.append("shapes:names")
    expected_pins = {item["name"] for item in expected["pins"]}
    if any(item.get("pin") and item["pin"] not in expected_pins for item in found["shapes"]):
        failures.append("shapes:unexpected_pin_figure")
    for pin in expected["pins"]:
        figures = [item for item in found["shapes"] if item.get("pin") == pin["name"]]
        if len(figures) != 1 or any(figures[0].get(key) != pin[key] for key in ("bbox", "layer", "purpose", "net")):
            failures.append(f"pin:{pin['name']}:figure_geometry")
    for collection, shape_type in (("paths", "path"), ("rects", "rect"), ("polygons", "polygon"), ("labels", "label")):
        for item in expected[collection]:
            found_item = actual_shapes.get(item["name"])
            if found_item is None or found_item.get("obj_type", found_item.get("type")) != shape_type:
                failures.append(f"{shape_type} {item['name']}")
                continue
            for key, value in item.items():
                if found_item.get(key) != value:
                    failures.append(f"{shape_type}:{item['name']}:{key}")
    if {item["name"] for item in found["nets"]} != set(expected["nets"]):
        failures.append("nets:names")
    if failures:
        raise ValidationError("Layout database readback differs from the plan", stage="readback", code="layout_readback_mismatch", details={"mismatches": failures})
    return {"ok": True, "checks": ["completeness", "technology", "instances", "masters", "parameters", "mosaics", "shapes", "vias", "pins", "nets", "geometry"]}


def equivalent_parameter(actual, expected):
    if actual == expected:
        return True
    if isinstance(actual, bool) or isinstance(expected, bool):
        return str(actual).lower() == str(expected).lower()
    try:
        from .core.contract import engineering

        return math.isclose(engineering(actual), engineering(expected), rel_tol=1e-9, abs_tol=1e-18)
    except (TypeError, ValueError, ValidationError):
        return str(actual) == str(expected)


def layout_summary(readback):
    value = stable_layout_readback(readback)
    counts = {key: len(value[key]) for key in ("instances", "mosaics", "shapes", "vias", "pins", "nets")}
    layers = {}
    for item in value["shapes"]:
        key = (item.get("layer"), item.get("purpose"))
        layers[key] = layers.get(key, 0) + 1
    boxes = [item["bbox"] for key in ("instances", "mosaics", "shapes", "vias") for item in value[key] if isinstance(item.get("bbox"), list) and len(item["bbox"]) == 4]
    bbox = [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)] if boxes else None
    return {"target": value.get("target"), "technology": value.get("technology"), "counts": counts, "bbox": bbox,
            "layers": [{"layer": key[0], "purpose": key[1], "shapes": count} for key, count in sorted(layers.items(), key=lambda item: canonical(item[0]))],
            "completeness": value["completeness"], "content_sha256": digest(layout_content(value))}


def select_layout_objects(readback, *, bbox=None, layer=None, purpose=None, net=None, inside=True, collections=None):
    value = require_complete_layout(readback)
    if bbox is not None:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) for number in bbox) or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
            raise ValueError("selection requires an ordered finite bbox")
    if type(inside) is not bool:
        raise ValueError("selection inside must be boolean")
    collections = collections if collections is not None else ("instances", "mosaics", "shapes", "vias")
    if not isinstance(collections, (list, tuple)) or set(collections) - {"instances", "mosaics", "shapes", "vias", "pins"}:
        raise ValueError("invalid selection collections")
    selected = []
    for collection in collections:
        for item in value[collection]:
            if any(expected is not None and item.get(key) != expected for key, expected in (("layer", layer), ("purpose", purpose), ("net", net))):
                continue
            if bbox is not None:
                box = item.get("bbox")
                if not isinstance(box, list) or len(box) != 4:
                    raise ValueError("cannot select objects with missing geometry")
                match = bbox[0] <= box[0] <= box[2] <= bbox[2] and bbox[1] <= box[1] <= box[3] <= bbox[3] if inside else box[0] <= bbox[2] and box[2] >= bbox[0] and box[1] <= bbox[3] and box[3] >= bbox[1]
                if not match:
                    continue
            selected.append({"collection": collection, "key": item["name"] if collection in {"instances", "mosaics", "pins"} else item["id"]})
    return selected


__all__ = ["compare_layout_plan", "diff_layout_readbacks", "digest", "layout_content", "layout_summary", "require_complete_layout", "select_layout_objects", "stable_layout_readback", "validate_layout_plan"]
