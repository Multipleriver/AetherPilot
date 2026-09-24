"""Validated candidate change sets for schematic evidence and publishing."""

from __future__ import annotations

import copy
import math

from .core.contract import ORIENTATIONS, property_records
from .core.readback import digest, diff_readbacks, readback_is_complete, stable_readback
from .signals import signal_expression_info, bit_connections, expand_signal


ALLOWED = {
    "instance.move",
    "instance.rotate",
    "instance.rename",
    "instance.set_master",
    "instance.set_parameter",
    "connection.set",
    "connection.compose",
    "inherited.create",
    "instance.set_netset",
    "pin.set",
    "wire.replace",
    "label.replace",
}


def validate_change_set(change_set):
    if not isinstance(change_set, dict):
        raise ValueError("change_set must be an object")
    if set(change_set) - {"schema_version", "base_sha256", "changes", "description"}:
        raise ValueError("change_set contains unknown fields")
    if change_set.get("schema_version", 1) != 1:
        raise ValueError("unsupported change_set schema")
    if not isinstance(change_set.get("base_sha256"), str) or len(change_set["base_sha256"]) != 64:
        raise ValueError("base_sha256 must be a SHA-256 digest")
    changes = change_set.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("change_set must contain at least one change")
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"type", "target", "value", "reason"}:
            raise ValueError("invalid change entry")
        if change.get("type") not in ALLOWED:
            raise ValueError("unsupported change type: %s" % change.get("type"))
        if not isinstance(change.get("target"), str) or not change["target"]:
            raise ValueError("change target is required")
        if "value" not in change:
            raise ValueError("change value is required")
    return copy.deepcopy(change_set)


def apply_to_readback(readback, change_set):
    change_set = validate_change_set(change_set)
    current = stable_readback(readback)
    base = digest(current)
    if base != change_set["base_sha256"]:
        raise ValueError("change base does not match readback")
    instances = {item["name"]: item for item in current.get("instances", [])}
    pins = {item["name"]: item for item in current.get("pins", [])}
    wires = list(current.get("wires", []))
    labels = list(current.get("labels", []))
    texts = list(current.get("texts", []))
    nets = set(current.get("nets", []))

    def point(value, where):
        if not isinstance(value, list) or len(value) != 2 or any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
            raise ValueError("%s requires two finite coordinates" % where)
        return [round(float(item), 9) for item in value]

    for change in change_set["changes"]:
        kind = change["type"]
        target = change["target"]
        value = change.get("value")
        if kind in {"instance.move", "instance.rotate", "instance.rename", "instance.set_master", "instance.set_parameter", "connection.set", "connection.compose", "instance.set_netset"}:
            if target not in instances:
                raise ValueError("instance does not exist: %s" % target)
            item = instances[target]
            if kind == "instance.move":
                item["origin"] = point(value, "instance.move")
            elif kind == "instance.rotate":
                if value not in ORIENTATIONS:
                    raise ValueError("instance.rotate requires a supported orientation")
                item["orientation"] = value
            elif kind == "instance.rename":
                if not isinstance(value, str) or not value or value in instances:
                    raise ValueError("invalid or duplicate instance name")
                item["name"] = value
                instances[value] = instances.pop(target)
            elif kind == "instance.set_master":
                if not isinstance(value, dict) or set(value) != {"library", "cell", "view"} or any(not isinstance(value[key], str) or not value[key] for key in value):
                    raise ValueError("instance.set_master requires a master object")
                item["master"] = value
            elif kind == "instance.set_parameter":
                if not isinstance(value, dict) or set(value) != {"name", "value"} or not isinstance(value.get("name"), str) or not value["name"]:
                    raise ValueError("instance.set_parameter requires name and value")
                item.setdefault("parameters", {})[value["name"]] = value.get("value")
            elif kind == "instance.set_netset":
                if not isinstance(value, dict) or set(value) != {"property", "net"} or not isinstance(value["property"], str) or not value["property"] or value["net"] not in nets:
                    raise ValueError("instance.set_netset requires an inherited property and declared net")
                previous_net = (item.get("netset_overrides") or {}).get(value["property"])
                if previous_net and previous_net != value["net"]:
                    attached_nets = set(current.get("nets", []))
                    attached_nets = {pin.get("net") for pin in current.get("pins", [])} | {
                        net for instance in current.get("instances", []) for net in (instance.get("connections") or {}).values()
                    }
                    if previous_net not in attached_nets:
                        wires = [wire for wire in wires if wire.get("net") != previous_net]
                        nets.discard(previous_net)
                        current["nets"] = [net for net in current.get("nets", []) if net != previous_net]
                        current["nets_detail"] = [detail for detail in current.get("nets_detail", []) if detail.get("name") != previous_net]
                        labels = [label for label in labels if label.get("text") != previous_net]
                        current["texts"] = [text for text in current.get("texts", []) if not (
                            text.get("text") == previous_net and text.get("kind") == "Text" and text.get("purpose") == "label"
                        )]
                item.setdefault("netset_overrides", {})[value["property"]] = value["net"]
            elif kind == "connection.compose":
                if isinstance(value, dict) and set(value) <= {"connections", "expected_widths"} and isinstance(value.get("connections"), dict):
                    expressions = value["connections"]
                    expected_widths = value.get("expected_widths", {})
                elif isinstance(value, dict):
                    expressions, expected_widths = value, {}
                else:
                    raise ValueError("connection.compose requires a terminal expression map")
                if not expressions or set(expressions) != set(item.get("connections", {})):
                    raise ValueError("connection.compose requires a complete terminal map")
                if not isinstance(expected_widths, dict) or set(expected_widths) - set(expressions):
                    raise ValueError("connection.compose expected_widths is invalid")
                normalized = {}
                declared_bits = set()
                for declared in nets:
                    try:
                        declared_bits.update(signal_expression_info(declared)["bits"])
                    except ValueError:
                        declared_bits.add(declared)
                for terminal, expression in expressions.items():
                    if not isinstance(terminal, str) or not isinstance(expression, str):
                        raise ValueError("connection.compose terminal and expression must be strings")
                    try:
                        info = signal_expression_info(expression, expected_width=expected_widths.get(terminal, len(expand_signal(terminal))), require_unique=False)
                    except ValueError as exc:
                        raise ValueError(f"invalid connection expression for {terminal}: {exc}") from exc
                    if any(bit not in declared_bits for bit in info["bits"]):
                        raise ValueError(f"connection expression for {terminal} references an undeclared net")
                    normalized[terminal] = info["canonical"]
                # A terminal bit may be mapped only once; an expression may
                # intentionally repeat a net, but a terminal-side duplicate is
                # always ambiguous and is therefore rejected above.
                item["connections"] = normalized
                item["connection_expressions"] = {key: {"expression": value, **signal_expression_info(value)} for key, value in normalized.items()}
            else:
                if not isinstance(value, dict) or set(value) != set(item.get("connections", {})):
                    raise ValueError("connection.set requires a terminal map")
                if any(net not in nets for net in value.values() if net is not None):
                    raise ValueError("connection.set references an undeclared net")
                item["connections"] = value
        elif kind == "inherited.create":
            if not isinstance(value, dict) or set(value) != {"default", "pin"} or not isinstance(value["default"], str) or not isinstance(value["pin"], str):
                raise ValueError("inherited.create requires its default net and attachment pin")
            if target in {item.get("property") for item in current.get("inherited_connections", [])}:
                raise ValueError("inherited property already exists")
            pin = pins.get(value["pin"])
            if pin is None or pin.get("net") != value["default"] or value["default"] not in nets:
                raise ValueError("inherited.create pin must already connect to its default net")
            current.setdefault("inherited_connections", []).append({"property": target, "default": value["default"],
                                                                       "raw": "[@%s:%%:%s]" % (target, value["default"]),
                                                                       "source": "authorized_change"})
        elif kind == "pin.set":
            if target not in pins or not isinstance(value, dict) or set(value) - {"net", "direction", "position", "orientation", "num_bits", "properties"}:
                raise ValueError("pin.set target or value is invalid")
            if "net" in value and value["net"] not in nets:
                raise ValueError("pin.set references an undeclared net")
            if "position" in value:
                value["position"] = point(value["position"], "pin.set.position")
            if "orientation" in value and value["orientation"] not in ORIENTATIONS:
                raise ValueError("pin.set.orientation is invalid")
            if "direction" in value and value["direction"] not in {"input", "output", "inputOutput"}:
                raise ValueError("pin.set.direction is invalid")
            if "num_bits" in value and (type(value["num_bits"]) is not int or value["num_bits"] < 1):
                raise ValueError("pin.set.num_bits is invalid")
            pins[target].update(value)
        elif kind == "wire.replace":
            if target != "*" or not isinstance(value, list):
                raise ValueError("wire.replace requires a wire list")
            for wire in value:
                if not isinstance(wire, dict) or set(wire) - {"wire_id", "net", "points"} or wire.get("net") not in nets:
                    raise ValueError("wire.replace contains an invalid wire")
                points = wire.get("points")
                if not isinstance(points, list) or len(points) < 2:
                    raise ValueError("wire.replace requires at least two points")
                normalized = [point(item, "wire.replace.points") for item in points]
                if any(first[0] != second[0] and first[1] != second[1] for first, second in zip(normalized, normalized[1:])):
                    raise ValueError("wire.replace only supports orthogonal wires")
                wire["points"] = normalized
            wires = value
        elif kind == "label.replace":
            if target != "*" or not isinstance(value, list):
                raise ValueError("label.replace requires a label list")
            for label in value:
                allowed = {"label_id", "kind", "text", "origin", "orientation", "height", "layer", "purpose", "justification", "font", "properties", "net"}
                required = {"text", "origin", "height", "layer", "purpose", "orientation", "justification", "font"}
                if not isinstance(label, dict) or set(label) - allowed or not required <= set(label) or not isinstance(label.get("text"), str) or not label["text"]:
                    raise ValueError("label.replace contains an invalid label")
                label.setdefault("kind", "label")
                if label["kind"] not in {"label", "note", "wire_name"}:
                    raise ValueError("label.replace contains an invalid annotation kind")
                label["origin"] = point(label["origin"], "label.replace.origin")
                if label["orientation"] not in ORIENTATIONS:
                    raise ValueError("label.replace.orientation is invalid")
                if type(label["height"]) not in (int, float) or not math.isfinite(label["height"]) or label["height"] <= 0:
                    raise ValueError("label.replace.height is invalid")
                if not all(isinstance(label[key], str) and label[key] for key in ("layer", "purpose", "justification", "font")):
                    raise ValueError("label.replace text attributes are invalid")
                label["properties"] = property_records(label.get("properties", []), "label.replace properties")
                if label["kind"] != "label" and label["properties"]:
                    raise ValueError("only ordinary labels may carry user properties")
                if label["kind"] == "note" and (label["layer"], label["purpose"]) != ("text", "drawing"):
                    raise ValueError("note annotations require text/drawing")
                if label["kind"] == "label" and (label["layer"], label["purpose"]) == ("text", "drawing"):
                    raise ValueError("ordinary labels cannot use the text/drawing note layer")
                if label["kind"] == "wire_name":
                    if (label["layer"], label["purpose"]) != ("wire", "label") or not isinstance(label.get("net"), str) or label["net"] not in nets or label["text"] != label["net"]:
                        raise ValueError("wire_name annotations require a declared matching net")
                elif (label["layer"], label["purpose"]) == ("wire", "label"):
                    raise ValueError("wire/label annotations must use kind=wire_name")
            labels = []
            replacement_texts = []
            for label in value:
                layer, purpose = ("text", "drawing") if label["kind"] == "note" else (label["layer"], label["purpose"])
                replacement_texts.append({"kind": "Text", "text": label["text"], "origin": label["origin"],
                                          "orientation": label["orientation"], "properties": label.get("properties", []),
                                          "bbox": None, "height": label["height"], "font": label["font"],
                                          "justification": label["justification"], "layer": layer, "purpose": purpose})
                if label["kind"] == "wire_name":
                    replacement_texts[-1]["net"] = label["net"]
            texts = [text for text in texts if text.get("kind", "").casefold() not in {"text", "label"}]
            texts.extend(replacement_texts)
    current["instances"] = sorted(instances.values(), key=lambda item: item["name"])
    current["pins"] = sorted(pins.values(), key=lambda item: item["name"])
    current["wires"] = wires
    current["labels"] = labels
    current["texts"] = texts
    current["change_set_id"] = digest(change_set)[:32]
    current["source"] = {**current.get("source", {}), "kind": "candidate_projection", "saved": False, "modified": True}
    current["database_sha256"] = None
    return stable_readback(current)


def validate_candidate(readback, change_set):
    normalized = stable_readback(readback)
    if not readback_is_complete(normalized):
        raise ValueError("cannot validate changes against incomplete readback")
    if "records" in normalized:
        raise ValueError("hierarchical native candidate editing is not implemented")
    candidate = apply_to_readback(normalized, change_set)
    return {
        "candidate": candidate,
        "diff": diff_readbacks(readback, candidate),
        "base_sha256": digest(stable_readback(readback)),
        "candidate_sha256": digest(candidate),
        "validation_scope": "structured_readback_only",
        "native_verified": False,
        "publishable": False,
    }
