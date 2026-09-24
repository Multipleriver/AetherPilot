"""Vendor-neutral symbol plans, readback normalization, and comparison gates."""

from __future__ import annotations

import copy
import math
import re

from .contract import canonical, digest, fields, identifier, reject
from .signals import ATOM, expand_signal, native_signal_name


SYMBOL_SCHEMA_VERSION = 1
SYMBOL_KINDS = {"line", "rect", "polygon", "ellipse", "circle", "arc", "label", "pin", "selection_box"}
SYMBOL_DIRECTIONS = {"input", "output", "inputOutput", "switch", "jumper", "unused", "tristate"}
SYMBOL_ORIENTATIONS = {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}
SYMBOL_FILLS = {"Outline", "Filled", "outline", "filled"}
SYMBOL_LABEL_TYPES = {"normal", "evaluation"}
SYMBOL_PIN_SHAPES = {"block", "square", "squareWire", "round", "acthi", "commactlo", "ieeeactlo"}
SYMBOL_REVISION_PROPERTIES = {"aetherpilot_owner", "connectivityLastUpdated", "lastSchematicExtraction"}


def validate_symbol_generation(source):
    fields(source, ("schematic_view", "symbol_view", "sides", "symbol_shape", "pin_space", "pin_length", "pin_shape", "pin_origin", "overwrite"), ("sort_pins",), where="symbol generation")
    value = copy.deepcopy(source)
    for key in ("schematic_view", "symbol_view"):
        identifier(value[key], key)
    if value["schematic_view"] == value["symbol_view"]:
        reject("Source schematic and generated symbol views must differ")
    for key, allowed in (("symbol_shape", {"transmission", "and", "or", "buffer", "nand", "nor", "invert"}),
                         ("pin_shape", {"block", "square", "round"}), ("pin_origin", {"topLeft", "centerLeft", "bottomLeft"})):
        if not isinstance(value[key], str) or value[key] not in allowed:
            reject("Unsupported native symbol generation option", option=key)
    value["pin_space"] = _number(value["pin_space"], "pin_space", positive=True)
    value["pin_length"] = _number(value["pin_length"], "pin_length", nonnegative=True)
    if type(value["overwrite"]) is not bool:
        reject("Symbol overwrite must be a boolean")
    value.setdefault("sort_pins", None)
    if value["sort_pins"] is not None and (not isinstance(value["sort_pins"], str) or value["sort_pins"] not in {"alphanumeric", "geometric"}):
        reject("Symbol pin sorting must be alphanumeric, geometric or None")
    if value["sides"] is not None:
        sides = value["sides"]
        if not isinstance(sides, dict) or set(sides) - {"left", "right", "top", "bottom"}:
            reject("Symbol sides must name left, right, top or bottom")
        assigned = set()
        for side, names in sides.items():
            if not isinstance(names, list) or len(names) > 10000:
                reject("Symbol side pins must be a bounded list")
            sides[side] = [_terminal_name(name) for name in names]
            if len(sides[side]) != len(set(sides[side])) or assigned.intersection(sides[side]):
                reject("Symbol pins must not be assigned to multiple sides")
            assigned.update(sides[side])
    return value


def sorted_symbol_pins(terminals, names, side, mode):
    records = {terminal["name"]: terminal for terminal in terminals}

    def natural(name):
        return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r"(\d+)", name)) + ((0, name),)

    if mode is None:
        return list(names)
    if mode == "alphanumeric":
        return sorted(names, key=natural)
    if mode != "geometric":
        reject("Unknown symbol pin sorting mode")
    positions = {name: _point(records[name].get("position"), "source pin position") for name in names}
    return sorted(names, key=lambda name: ((-positions[name][1], positions[name][0]) if side in {"left", "right"} else (positions[name][0], -positions[name][1]), natural(name)))


def default_symbol_side(name, direction):
    """Choose a conventional side without guessing nonstandard supply names."""
    if isinstance(name, str):
        if name.casefold() == "vdd":
            return "top"
        if name.casefold() == "vss":
            return "bottom"
    if direction in {"input", "switch"}:
        return "left"
    if direction in {"output", "inputOutput", "tristate"}:
        return "right"
    return "top"


def symbol_pin_side_issues(readback, grouped):
    """Check actual native pin geometry and pin-name labels after generation."""
    issues = []
    if not isinstance(readback, dict) or not isinstance(grouped, dict):
        return [{"kind": "invalid_readback"}]
    shapes = readback.get("shapes", [])
    bodies = [item["bbox"] for item in shapes if isinstance(item, dict) and item.get("obj_type") == "rect" and
              item.get("layer") == "device" and item.get("purpose") == "drawing" and
              isinstance(item.get("bbox"), (list, tuple)) and len(item["bbox"]) == 4]
    if bodies:
        box = max(bodies, key=lambda item: (item[2] - item[0]) * (item[3] - item[1]))
    else:
        boxes = readback.get("selection_boxes", [])
        box = boxes[0] if isinstance(boxes, list) and boxes and isinstance(boxes[0], (list, tuple)) and len(boxes[0]) == 4 else None
    if box is None or any(type(value) not in (int, float) or not math.isfinite(value) for value in box):
        return [{"kind": "body_bbox_missing"}]
    x0, y0, x1, y1 = box
    if x0 >= x1 or y0 >= y1:
        return [{"kind": "body_bbox_invalid"}]
    terms = readback.get("terms", [])
    if not isinstance(terms, list):
        return [{"kind": "terms_missing"}]
    by_name = {item.get("name"): item for item in terms if isinstance(item, dict) and isinstance(item.get("name"), str)}
    if len(by_name) != len(terms):
        issues.append({"kind": "term_identity_incomplete"})
    tolerance = 1e-7
    for side in ("left", "right", "top", "bottom"):
        for name in grouped.get(side, []):
            term = by_name.get(name)
            point = term.get("position") if term else None
            if (not isinstance(point, (list, tuple)) or len(point) != 2 or
                    any(type(value) not in (int, float) or not math.isfinite(value) for value in point)):
                issues.append({"kind": "pin_position_missing", "name": name, "side": side})
                continue
            x, y = point
            on_side = {"left": x <= x0 + tolerance and y0 - tolerance <= y <= y1 + tolerance,
                       "right": x >= x1 - tolerance and y0 - tolerance <= y <= y1 + tolerance,
                       "top": y >= y1 - tolerance and x0 - tolerance <= x <= x1 + tolerance,
                       "bottom": y <= y0 + tolerance and x0 - tolerance <= x <= x1 + tolerance}[side]
            if not on_side:
                issues.append({"kind": "pin_on_wrong_side", "name": name, "side": side, "position": list(point), "body_bbox": list(box)})
    labels = [item for item in readback.get("labels", []) if isinstance(item, dict) and item.get("semantic") == "pin_name" and
              isinstance(item.get("bbox"), (list, tuple)) and len(item["bbox"]) == 4]
    for index, left in enumerate(labels):
        for right in labels[index + 1:]:
            a, b = left["bbox"], right["bbox"]
            if min(a[2], b[2]) - max(a[0], b[0]) > tolerance and min(a[3], b[3]) - max(a[1], b[1]) > tolerance:
                issues.append({"kind": "pin_label_overlap", "labels": [left.get("text"), right.get("text")]})
    return issues


def _terminal_name(value):
    match = ATOM.fullmatch(value) if isinstance(value, str) else None
    if match is None or match[5] is not None:
        reject("Symbol terminal must be a scalar, bit, or contiguous bus name")
    identifier(match[1], "symbol terminal base name")
    try:
        expand_signal(value, max_bits=4096)
        return native_signal_name(value)
    except ValueError as error:
        reject(str(error))


def _number(value, where, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        reject(f"{where} must be a finite number")
    if positive and value <= 0:
        reject(f"{where} must be positive")
    if nonnegative and value < 0:
        reject(f"{where} must be nonnegative")
    if abs(value) > 1_000_000:
        reject(f"{where} is outside bounds")
    return float(value)


def _point(value, where):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        reject(f"{where} must contain two coordinates")
    return [_number(value[0], f"{where}[0]"), _number(value[1], f"{where}[1]")]


def _bbox(value, where):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        reject(f"{where} must contain four coordinates")
    result = [_number(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if result[0] > result[2] or result[1] > result[3] or result[0] == result[2] or result[1] == result[3]:
        reject(f"{where} must be a nonzero ordered box")
    return result


def _points(value, where, *, minimum=2, maximum=10000):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        reject(f"{where} must contain {minimum}–{maximum} points")
    result = [_point(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if len({tuple(item) for item in result}) < 2:
        reject(f"{where} must contain at least two distinct points")
    return result


def _lpp(value, where):
    if not isinstance(value, str) or not value or len(value) > 256:
        reject(f"{where} must be a nonempty string")
    return value


def _shape(value, index):
    if not isinstance(value, dict):
        reject(f"symbol.shapes[{index}] must be an object")
    kind = value.get("kind")
    if kind not in SYMBOL_KINDS:
        reject(f"Unsupported symbol shape kind at index {index}")
    common = {"kind", "layer", "purpose", "properties"}
    if kind in {"line", "polygon", "arc"}:
        fields(value, ("kind", "layer", "purpose", "points"), ("properties",), where=f"symbol.shapes[{index}]")
        result = {key: copy.deepcopy(value[key]) for key in value}
        result["layer"] = _lpp(result["layer"], f"symbol.shapes[{index}].layer")
        result["purpose"] = _lpp(result["purpose"], f"symbol.shapes[{index}].purpose")
        result["points"] = _points(result["points"], f"symbol.shapes[{index}].points", minimum=2 if kind != "polygon" else 3)
    elif kind in {"rect", "ellipse"}:
        fields(value, ("kind", "layer", "purpose", "bbox"), ("fill_style", "properties"), where=f"symbol.shapes[{index}]")
        result = {key: copy.deepcopy(value[key]) for key in value}
        result["layer"] = _lpp(result["layer"], f"symbol.shapes[{index}].layer")
        result["purpose"] = _lpp(result["purpose"], f"symbol.shapes[{index}].purpose")
        result["bbox"] = _bbox(result["bbox"], f"symbol.shapes[{index}].bbox")
        result.setdefault("fill_style", "Outline")
        if result["fill_style"] not in SYMBOL_FILLS:
            reject(f"Unsupported fill style at symbol.shapes[{index}]")
        result["fill_style"] = result["fill_style"].title()
        if result["fill_style"] != "Outline":
            reject("Native symbol fill is controlled by layer display, not a per-shape Filled flag")
    elif kind == "circle":
        fields(value, ("kind", "layer", "purpose", "center", "radius"), ("circle_type", "quadrant", "sides", "properties"), where=f"symbol.shapes[{index}]")
        result = {key: copy.deepcopy(value[key]) for key in value}
        result["layer"] = _lpp(result["layer"], f"symbol.shapes[{index}].layer")
        result["purpose"] = _lpp(result["purpose"], f"symbol.shapes[{index}].purpose")
        result["center"] = _point(result["center"], f"symbol.shapes[{index}].center")
        result["radius"] = _number(result["radius"], f"symbol.shapes[{index}].radius", positive=True)
        result.setdefault("circle_type", 1)
        result.setdefault("quadrant", 1)
        result.setdefault("sides", 32)
        if type(result["circle_type"]) is not int or result["circle_type"] not in {1, 2, 4}:
            reject(f"symbol.shapes[{index}].circle_type must be 1, 2, or 4")
        if type(result["quadrant"]) is not int or result["quadrant"] not in {1, 2, 3, 4}:
            reject(f"symbol.shapes[{index}].quadrant must be 1–4")
        if type(result["sides"]) is not int or not 4 <= result["sides"] <= 1024:
            reject(f"symbol.shapes[{index}].sides must be 4–1024")
    elif kind == "label":
        fields(value, ("kind", "layer", "purpose", "position", "text"),
               ("label_type", "height", "orientation", "justification", "font", "draft", "overbar", "semantic", "properties"),
               where=f"symbol.shapes[{index}]")
        result = {key: copy.deepcopy(value[key]) for key in value}
        result["layer"] = _lpp(result["layer"], f"symbol.shapes[{index}].layer")
        result["purpose"] = _lpp(result["purpose"], f"symbol.shapes[{index}].purpose")
        result["position"] = _point(result["position"], f"symbol.shapes[{index}].position")
        if not isinstance(result["text"], str) or not result["text"] or len(result["text"]) > 4096:
            reject(f"symbol.shapes[{index}].text must be a nonempty string")
        result.setdefault("label_type", "normal")
        result.setdefault("height", 0.0625)
        result.setdefault("orientation", "R0")
        result.setdefault("justification", "lowerLeft")
        result.setdefault("font", "stick")
        result.setdefault("draft", True)
        result.setdefault("overbar", False)
        if result["label_type"] not in SYMBOL_LABEL_TYPES or result["orientation"] not in SYMBOL_ORIENTATIONS:
            reject(f"Unsupported symbol label type or orientation at index {index}")
        result["height"] = _number(result["height"], f"symbol.shapes[{index}].height", positive=True)
        for key in ("justification", "font"):
            result[key] = _lpp(result[key], f"symbol.shapes[{index}].{key}")
        if type(result["draft"]) is not bool or type(result["overbar"]) is not bool:
            reject(f"symbol.shapes[{index}] draft and overbar must be boolean")
        if result.get("semantic") not in {None, "pin_name", "instance_name", "logical_name"}:
            reject(f"Unsupported symbol label semantic at index {index}")
        if result.get("semantic") in {"instance_name", "logical_name"} and result["label_type"] != "evaluation":
            reject("Instance and logical symbol labels must use evaluation label type")
    elif kind == "pin":
        fields(value, ("kind", "name", "position", "direction"),
               ("shape_mode", "label_offset", "show_name", "overbar", "orientation", "is_changed", "properties", "half_size"),
               where=f"symbol.shapes[{index}]")
        result = {key: copy.deepcopy(value[key]) for key in value}
        result["name"] = _terminal_name(result["name"])
        result["position"] = _point(result["position"], f"symbol.shapes[{index}].position")
        if result["direction"] not in SYMBOL_DIRECTIONS:
            reject(f"Unsupported symbol pin direction at index {index}")
        result.setdefault("shape_mode", "square")
        result.setdefault("label_offset", 1)
        result.setdefault("show_name", True)
        result.setdefault("overbar", False)
        result.setdefault("orientation", "R0")
        result.setdefault("is_changed", False)
        if result["shape_mode"] not in SYMBOL_PIN_SHAPES or result["orientation"] not in SYMBOL_ORIENTATIONS:
            reject(f"Unsupported symbol pin shape or orientation at index {index}")
        if type(result["label_offset"]) is not int or not 0 <= result["label_offset"] <= 1000:
            reject(f"symbol.shapes[{index}].label_offset must be an integer from 0 to 1000")
        if any(type(result[key]) is not bool for key in ("show_name", "overbar", "is_changed")):
            reject(f"symbol.shapes[{index}] pin flags must be boolean")
        if "half_size" in result:
            result["half_size"] = _number(result["half_size"], "pin half_size", positive=True)
            if result["shape_mode"] != "square":
                reject("Explicit pin half_size applies to square pin figures only")
    else:
        fields(value, ("kind", "bbox"), ("properties",), where=f"symbol.shapes[{index}]")
        result = {key: copy.deepcopy(value[key]) for key in value}
        result["bbox"] = _bbox(result["bbox"], f"symbol.shapes[{index}].bbox")
    if "properties" in result and not isinstance(result["properties"], dict):
        reject(f"symbol.shapes[{index}].properties must be an object")
    if result.get("properties"):
        reject("Per-object symbol properties are not projected; they cannot be silently discarded")
    return result


def validate_symbol_plan(plan):
    """Validate and normalize a declarative symbol editor plan."""
    if not isinstance(plan, dict):
        reject("symbol plan must be an object")
    value = copy.deepcopy(plan)
    fields(value, ("schema_version", "operation", "target", "mode", "shapes"), ("term_order", "overwrite", "technology"), where="symbol plan")
    if value["schema_version"] != SYMBOL_SCHEMA_VERSION or value["operation"] != "symbol.apply":
        reject("Unsupported symbol plan schema or operation")
    target = value["target"]
    if not isinstance(target, dict):
        reject("symbol target must be an object")
    target_fields = {"library", "cell", "view", "technology"}
    if set(target) - target_fields or {"library", "cell"} - set(target):
        reject("Invalid fields in symbol target")
    target.setdefault("view", "symbol")
    if target["view"] != "symbol":
        reject("symbol plans write symbol views only")
    identifier(target["library"], "target.library")
    identifier(target["cell"], "target.cell")
    if "technology" in target and target["technology"] != "018um_PDK":
        reject("Only the official 018um_PDK is in the certification scope")
    if value["mode"] not in {"create", "modify"}:
        reject("symbol.mode must be create or modify")
    if type(value.get("overwrite", False)) is not bool:
        reject("symbol.overwrite must be boolean")
    if not isinstance(value["shapes"], list) or len(value["shapes"]) > 10000:
        reject("symbol.shapes must contain at most 10000 entries")
    shapes = [_shape(item, index) for index, item in enumerate(value["shapes"])]
    names = [item["name"] for item in shapes if item["kind"] == "pin"]
    if len(names) != len(set(names)):
        reject("symbol pin names must be unique")
    value["shapes"] = shapes
    pin_names = [item["name"] for item in shapes if item["kind"] == "pin"]
    terminal_bits = [bit for name in pin_names for bit in expand_signal(name, max_bits=4096)]
    if len(terminal_bits) != len(set(terminal_bits)):
        reject("Symbol terminals contain overlapping bits")
    if "term_order" not in value:
        value["term_order"] = pin_names
    if not isinstance(value["term_order"], list) or any(not isinstance(item, str) for item in value["term_order"]):
        reject("symbol.term_order must be a string array")
    value["term_order"] = [_terminal_name(item) for item in value["term_order"]]
    if len(value["term_order"]) != len(set(value["term_order"])) or set(value["term_order"]) != set(pin_names):
        reject("symbol.term_order must contain every declared pin exactly once")
    return value


def symbol_content(readback):
    """Return stable, identity-free symbol content for hashes and diffs."""
    if not isinstance(readback, dict):
        raise ValueError("symbol readback must be an object")
    return {
        "target": {key: readback.get("target", {}).get(key) for key in ("library", "cell", "view")},
        "terms": [
            {key: item.get(key) for key in ("name", "direction", "num_bits", "bbox", "position", "properties")}
            for item in readback.get("terms", [])
        ],
        "labels": [
            {key: item.get(key) for key in ("text", "label_type", "layer", "purpose", "position", "height", "orientation", "justification", "font", "semantic", "properties")}
            for item in readback.get("labels", [])
        ],
        "shapes": [
            {key: item.get(key) for key in ("obj_type", "layer", "purpose", "bbox", "points", "center", "radius", "fill_style", "properties")}
            for item in readback.get("shapes", [])
            if item.get("obj_type") not in {"pin", "label", "selection_box"}
        ],
        "selection_boxes": readback.get("selection_boxes", []),
        "pin_order": list(readback.get("pin_order", [])),
        "port_order": list(readback.get("port_order", [])),
        "term_order": list(readback.get("term_order", [])),
        "properties": sorted((item for item in readback.get("properties", []) if item.get("name") not in SYMBOL_REVISION_PROPERTIES), key=canonical),
    }


def validate_symbol_readback(value):
    """Return errors for a native symbol readback."""
    errors = []
    if not isinstance(value, dict):
        return ["symbol readback must be an object"]
    if value.get("schema_version") != SYMBOL_SCHEMA_VERSION:
        errors.append("symbol readback schema is unsupported")
    if not isinstance(value.get("terms"), list):
        errors.append("symbol terms are missing")
    if not isinstance(value.get("labels"), list):
        errors.append("symbol labels are missing")
    if not isinstance(value.get("shapes"), list):
        errors.append("symbol shapes are missing")
    if not isinstance(value.get("selection_boxes"), list):
        errors.append("symbol selection_boxes are missing")
    if not isinstance(value.get("pin_order"), list) or not isinstance(value.get("port_order"), list) or not isinstance(value.get("term_order"), list):
        errors.append("symbol order fields are malformed")
    completeness = value.get("completeness")
    if not isinstance(completeness, dict) or completeness.get("status") != "complete" or completeness.get("issues"):
        errors.append("symbol readback is incomplete")
    from .readback import readback_is_complete

    if not readback_is_complete(value):
        errors.append("symbol readback contains unknown or unsupported facts")
    try:
        if value.get("content_sha256") != digest(symbol_content(value)):
            errors.append("symbol content hash does not match readback")
    except (TypeError, ValueError):
        errors.append("symbol content is not finite JSON")
    return errors


def _expected_labels(plan):
    return [
        {
            "text": item["text"],
            "label_type": item["label_type"],
            "layer": item["layer"],
            "purpose": item["purpose"],
            "position": item["position"],
            "height": item["height"],
            "orientation": item["orientation"],
            "justification": item["justification"],
            "font": item["font"],
            "semantic": item.get("semantic"),
        }
        for item in plan["shapes"]
        if item["kind"] == "label"
    ]


def _label_matches(expected, actual):
    if expected["text"] != actual.get("text") or expected["label_type"] != actual.get("label_type"):
        return False
    if expected.get("semantic"):
        return expected.get("semantic") == actual.get("semantic") and all(expected.get(key) == actual.get(key) for key in ("position", "height", "orientation", "justification", "font"))
    return all(expected.get(key) == actual.get(key) for key in ("layer", "purpose", "position", "height", "orientation", "justification", "font"))


def compare_symbol_plan(plan, actual):
    """Compare native symbol facts to a manual symbol plan."""
    errors = validate_symbol_readback(actual)
    if errors:
        raise ValueError("cannot compare incomplete symbol readback: " + "; ".join(errors))
    expected_pins = [item for item in plan["shapes"] if item["kind"] == "pin"]
    actual_terms = {item.get("name"): item for item in actual["terms"]}
    missing = [item["name"] for item in expected_pins if item["name"] not in actual_terms]
    if missing:
        raise ValueError(f"symbol terminal set differs; missing {missing}")
    for item in expected_pins:
        term = actual_terms[item["name"]]
        if term.get("direction") != item["direction"]:
            raise ValueError(f"symbol terminal direction differs for {item['name']}")
        if term.get("position") != item["position"] or term.get("num_bits") != len(expand_signal(item["name"], max_bits=4096)):
            raise ValueError(f"symbol terminal geometry or bit width differs for {item['name']}")
        if "half_size" in item:
            horizontal, vertical = item["position"]
            half_size = item["half_size"]
            if term.get("bbox") != [horizontal - half_size, vertical - half_size, horizontal + half_size, vertical + half_size]:
                raise ValueError(f"symbol terminal size differs for {item['name']}")
    if list(actual["term_order"]) != list(plan["term_order"]):
        raise ValueError("symbol terminal order differs")
    expected_labels = _expected_labels(plan)
    unmatched = list(actual["labels"])
    for expected in expected_labels:
        match = next((item for item in unmatched if _label_matches(expected, item)), None)
        if match is None:
            raise ValueError("symbol semantic or drawing labels differ")
        unmatched.remove(match)
    if any(not item.get("automatic") for item in unmatched):
        raise ValueError("symbol contains undeclared user labels")
    expected_boxes = sorted(item["bbox"] for item in plan["shapes"] if item["kind"] == "selection_box")
    if expected_boxes != sorted(actual["selection_boxes"]):
        raise ValueError("symbol selection boxes differ")
    expected_drawings = []
    for item in plan["shapes"]:
        if item["kind"] in {"line", "rect", "polygon", "ellipse", "circle", "arc"}:
            points = item.get("points")
            bbox = item.get("bbox")
            if bbox is None and points:
                coordinates = [coordinate for point in points for coordinate in point]
                bbox = [min(coordinates[0::2]), min(coordinates[1::2]), max(coordinates[0::2]), max(coordinates[1::2])]
            expected_drawings.append({
                "obj_type": item["kind"], "layer": item["layer"], "purpose": item["purpose"],
                "bbox": bbox, "points": points, "center": item.get("center"),
                "radius": item.get("radius"), "fill_style": item.get("fill_style"),
            })
    actual_drawings = [
        {key: item.get(key) for key in ("obj_type", "layer", "purpose", "bbox", "points", "center", "radius", "fill_style")}
        for item in actual["shapes"] if item.get("obj_type") not in {"pin", "label", "selection_box"}
    ]
    for item in actual_drawings:
        if item["obj_type"] in {"rect", "ellipse"} and item["fill_style"] is None:
            item["fill_style"] = "Outline"
        if item["obj_type"] in {"rect", "ellipse"} and item["points"] is not None:
            item["points"] = None
    if sorted(expected_drawings, key=canonical) != sorted(actual_drawings, key=canonical):
        raise ValueError("symbol drawing geometry differs")
    return {"ok": True, "checks": ["terminals", "directions", "term_order", "labels", "selection_boxes", "drawing_geometry"]}
