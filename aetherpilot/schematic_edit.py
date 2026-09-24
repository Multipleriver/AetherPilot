"""Scoped schematic deltas with full unchanged-content verification."""

from __future__ import annotations

import copy
import re

from .changes import apply_to_readback, validate_change_set
from .contract import ORIENTATIONS, digest, engineering, fields, identifier, parameter_literal, point, property_records, signal_identifier
from .errors import ValidationError
from .planner import compile_wires, transform_point
from .parameters import resolve_parameter
from .readback import digest as readback_digest, readback_is_complete, stable_readback
from .schematic_content import schematic_content
from .signals import bit_connections, expand_signal, signal_expression_info
from .transactions import writable_target


def _safe_figure_type(figure):
    try:
        return str(getattr(figure, "objType", "")).casefold()
    except Exception:
        return ""


# Keep the historical V1 constant stable for clients that enumerate it.  The
# composition operation is an additive, capability-gated extension exposed in
# ``delta_matrix_surface`` and accepted by validation below.
SUPPORTED = {"instance.move", "instance.rotate", "instance.rename", "instance.set_master", "instance.set_parameter", "instance.set_netset", "connection.set", "inherited.create", "pin.set", "wire.replace", "label.replace"}
ANNOTATION_KINDS = {"label", "note", "wire_name"}


def delta_matrix_surface() -> dict[str, object]:
    """Describe the complete bounded delta matrix exposed to an executor.

    Keeping this as data makes it possible for a test harness or deterministic planner to
    enumerate cases instead of inventing unsupported operation names.
    """
    return {
        "schema_version": 1,
        "instance": ["move", "rotate", "rename", "set_master", "set_parameter", "set_netset"],
        "connectivity": ["connection.set", "connection.compose", "inherited.create", "pin.set", "wire.replace"],
        "annotation": ["label.replace:label", "label.replace:note", "label.replace:wire_name"],
        "batch_rules": ["one whole-surface wire replacement", "one whole-surface label replacement",
                        "unique rename destinations", "complete baseline and native readback required"],
        "native_policy": "unknown native objects remain incomplete and cannot be published",
    }


def validate_schematic_edit(source):
    fields(source, ("schema_version", "operation", "target", "base_sha256", "changes"), ("mode", "grid", "description"), where="schematic.edit")
    value = copy.deepcopy(source)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["operation"] != "schematic.edit" or value.get("mode", "modify") != "modify":
        raise ValidationError("Invalid schematic edit operation", stage="plan", code="invalid_change_input")
    target = {**value["target"], "view": value["target"].get("view", "schematic"), "technology": value["target"].get("technology", "018um_PDK")}
    writable_target(target)
    if target["view"] != "schematic" or not isinstance(value["base_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["base_sha256"]):
        raise ValidationError("Schematic edits require a schematic target and baseline SHA-256", stage="plan", code="invalid_change_input")
    value.update(target=target, mode="modify", grid=value.get("grid", 0.0625))
    if type(value["grid"]) not in (int, float) or not 1e-6 <= value["grid"] <= 1e6:
        raise ValidationError("Invalid edit grid", stage="plan", code="invalid_change_input")
    try:
        changes = validate_change_set({"schema_version": 1, "base_sha256": value["base_sha256"], "changes": value["changes"]})["changes"]
    except ValueError as exc:
        raise ValidationError(str(exc), stage="plan", code="invalid_change_input") from exc
    if len(changes) > 2000:
        raise ValidationError("Schematic edit budget exceeded", stage="plan", code="resource_limit")
    # Replacement operations are whole-surface operations.  Accepting two in
    # one request would make ordering an accidental semantic channel, so reject
    # it before touching a native database.  Rename targets are likewise
    # checked as a batch to keep generated deltas deterministic.
    if sum(change["type"] == "wire.replace" for change in changes) > 1 or sum(change["type"] == "label.replace" for change in changes) > 1:
        raise ValidationError("A schematic edit may contain at most one wire and one label replacement", stage="plan", code="ambiguous_change_sequence")
    rename_values = [change["value"] for change in changes if change["type"] == "instance.rename"]
    if len(rename_values) != len(set(rename_values)):
        raise ValidationError("Schematic rename destinations must be unique", stage="plan", code="ambiguous_change_sequence")
    for change in changes:
        kind, replacement = change["type"], change["value"]
        if kind not in SUPPORTED and kind != "connection.compose":
            raise ValidationError("This native schematic delta is not yet supported", stage="plan", code="unsupported_change", details={"type": kind})
        if kind.startswith("instance.") or kind in {"connection.set", "connection.compose"}:
            change["target"] = signal_identifier(change["target"], "instance")
        if kind == "instance.move":
            change["value"] = point(replacement, value["grid"], "instance.move")
        elif kind == "instance.rotate" and replacement not in ORIENTATIONS:
            raise ValidationError("Invalid instance orientation", stage="plan", code="invalid_change_input")
        elif kind == "instance.rename":
            change["value"] = identifier(replacement, "scalar instance name")
            if len(expand_signal(change["target"])) != 1 or "<" in change["target"]:
                raise ValidationError("Vector instance renaming needs an explicit occurrence operation", stage="plan", code="unsupported_change")
        elif kind == "instance.set_master":
            fields(replacement, ("library", "cell", "view"), where="master")
            for name in replacement.values():
                identifier(name, "master")
        elif kind == "instance.set_parameter":
            fields(replacement, ("name", "value"), where="parameter")
            identifier(replacement["name"], "parameter")
            replacement["value"] = parameter_literal(replacement["value"], "parameter")
        elif kind == "instance.set_netset":
            fields(replacement, ("property", "net"), where="netSet")
            identifier(replacement["property"], "netSet property")
            replacement["net"] = signal_identifier(replacement["net"], "netSet net")
        elif kind == "connection.set":
            if not isinstance(replacement, dict) or not replacement:
                raise ValidationError("Connection change requires a complete terminal map", stage="plan", code="invalid_change_input")
            normalized = {signal_identifier(name): signal_identifier(net) for name, net in replacement.items()}
            if len(normalized) != len(replacement):
                raise ValidationError("Ambiguous terminal map", stage="plan", code="invalid_change_input")
            change["value"] = normalized
        elif kind == "connection.compose":
            if isinstance(replacement, dict) and set(replacement) <= {"connections", "expected_widths"} and isinstance(replacement.get("connections"), dict):
                expressions, expected_widths = replacement["connections"], replacement.get("expected_widths", {})
            elif isinstance(replacement, dict):
                expressions, expected_widths = replacement, {}
            else:
                raise ValidationError("Connection composition requires a terminal expression map", stage="plan", code="invalid_change_input")
            if not expressions or not isinstance(expected_widths, dict) or set(expected_widths) - set(expressions):
                raise ValidationError("Connection composition map is invalid", stage="plan", code="invalid_change_input")
            normalized = {}
            for terminal, expression in expressions.items():
                if not isinstance(terminal, str) or not isinstance(expression, str):
                    raise ValidationError("Connection composition terminal and expression must be strings", stage="plan", code="invalid_change_input")
                try:
                    info = signal_expression_info(expression, expected_width=expected_widths.get(terminal), require_unique=False)
                except ValueError as exc:
                    raise ValidationError(str(exc), stage="plan", code="invalid_change_input") from exc
                normalized[terminal] = info["canonical"]
            change["value"] = {"connections": normalized, "expected_widths": expected_widths}
        elif kind == "inherited.create":
            change["target"] = identifier(change["target"], "inherited property")
            fields(replacement, ("default", "pin"), where="inherited connection")
            replacement["default"] = signal_identifier(replacement["default"], "inherited default net")
            replacement["pin"] = signal_identifier(replacement["pin"], "inherited attachment pin")
        elif kind == "pin.set":
            fields(replacement, (), ("net", "direction", "position", "orientation"), where="pin change")
            if not replacement:
                raise ValidationError("pin.set must change at least one field", stage="plan", code="invalid_change_input")
            if "net" in replacement:
                replacement["net"] = signal_identifier(replacement["net"])
            if "position" in replacement:
                replacement["position"] = point(replacement["position"], value["grid"], "pin.position")
        elif kind == "wire.replace":
            if change["target"] != "*" or not isinstance(replacement, list) or len(replacement) > 3000:
                raise ValidationError("Wire replacement requires a bounded complete wire list", stage="plan", code="invalid_change_input")
            for wire in replacement:
                fields(wire, ("net", "points"), where="wire")
                wire["net"] = signal_identifier(wire["net"])
                if not isinstance(wire["points"], list) or not 2 <= len(wire["points"]) <= 1000:
                    raise ValidationError("Invalid wire path", stage="plan", code="invalid_change_input")
                wire["points"] = [point(position, value["grid"], "wire point") for position in wire["points"]]
        elif kind == "label.replace":
            if change["target"] != "*" or not isinstance(replacement, list) or len(replacement) > 1000:
                raise ValidationError("Label replacement requires a bounded complete label list", stage="plan", code="invalid_change_input")
            for label in replacement:
                if not isinstance(label, dict) or set(label) - {"label_id", "kind", "text", "origin", "orientation", "height", "layer", "purpose", "justification", "font", "properties", "net"}:
                    raise ValidationError("Invalid schematic label", stage="plan", code="invalid_change_input")
                for key in ("text", "layer", "purpose", "justification", "font"):
                    if not isinstance(label.get(key), str) or not label[key]:
                        raise ValidationError("Schematic label text attributes are required", stage="plan", code="invalid_change_input")
                label["origin"] = point(label.get("origin"), value["grid"], "label.origin")
                if label.get("orientation", "R0") not in ORIENTATIONS:
                    raise ValidationError("Invalid schematic label orientation", stage="plan", code="invalid_change_input")
                label.setdefault("orientation", "R0")
                if type(label.get("height")) not in (int, float) or not 1e-6 <= label["height"] <= 1e6:
                    raise ValidationError("Invalid schematic label height", stage="plan", code="invalid_change_input")
                label.setdefault("properties", [])
                label.setdefault("kind", "label")
                if label["kind"] not in ANNOTATION_KINDS:
                    raise ValidationError("Invalid schematic annotation kind", stage="plan", code="invalid_change_input")
                label["properties"] = property_records(label.get("properties", []), "Schematic annotation properties")
                if label["kind"] != "label" and label["properties"]:
                    raise ValidationError("Only ordinary labels may carry user properties", stage="plan", code="unsupported_change")
                if label["kind"] == "note":
                    if (label["layer"], label["purpose"]) != ("text", "drawing"):
                        raise ValidationError("Note annotations require the text/drawing layer purpose", stage="plan", code="invalid_change_input")
                elif (label["layer"], label["purpose"]) == ("text", "drawing"):
                    raise ValidationError("Ordinary labels cannot use the text/drawing note layer; use kind=note", stage="plan", code="invalid_change_input")
                elif label["kind"] == "wire_name":
                    if (label["layer"], label["purpose"]) != ("wire", "label") or not isinstance(label.get("net"), str):
                        raise ValidationError("Wire-name annotations require wire/label and a declared net", stage="plan", code="invalid_change_input")
                    label["net"] = signal_identifier(label["net"], "wire-name net")
                    if label["text"] != label["net"]:
                        raise ValidationError("Wire-name text must equal its attached net", stage="plan", code="invalid_change_input")
                elif (label["layer"], label["purpose"]) == ("wire", "label"):
                    raise ValidationError("wire/label annotations must declare kind=wire_name", stage="plan", code="invalid_change_input")
    value["changes"] = changes
    return value


def transformed_box(box, before, after):
    basis = [transform_point(vector, [0, 0], before["orientation"]) for vector in ([1, 0], [0, 1])]
    transformed = []
    for horizontal in (box[0][0], box[1][0]):
        for vertical in (box[0][1], box[1][1]):
            relative = [horizontal - before["origin"][0], vertical - before["origin"][1]]
            local = [sum(relative[axis] * vector[axis] for axis in (0, 1)) for vector in basis]
            transformed.append(transform_point(local, after["origin"], after["orientation"]))
    return [[min(position[axis] for position in transformed) for axis in (0, 1)], [max(position[axis] for position in transformed) for axis in (0, 1)]]


def has_geometry_delta(before, after, changes):
    """Return whether a geometry-capable change actually moved native geometry."""
    geometry_types = {"instance.move", "instance.rotate", "instance.set_master", "pin.set"}
    for change in changes:
        if change["type"] not in geometry_types:
            continue
        collection = "pins" if change["type"] == "pin.set" else "instances"
        old = next((item for item in before.get(collection, []) if item.get("name") == change["target"]), None)
        new = next((item for item in after.get(collection, []) if item.get("name") == change["target"]), None)
        if old is None or new is None:
            # A missing object will be rejected by the normal edit validator;
            # conservatively route rather than silently preserve stale wires.
            return True
        fields_to_compare = ("position", "orientation") if collection == "pins" else ("origin", "orientation", "master")
        if any(old.get(field) != new.get(field) for field in fields_to_compare):
            return True
    return False


def project_schematic_edit(before, plan, masters=None):
    if not readback_is_complete(before) or digest(schematic_content(before)) != plan["base_sha256"]:
        raise ValidationError("Schematic edit requires its exact complete baseline", stage="plan", code="external_changes")
    current = stable_readback(before)
    for change in plan["changes"]:
        current = stable_readback(current)
        prior = copy.deepcopy(current)
        try:
            current = apply_to_readback(current, {"base_sha256": readback_digest(current), "changes": [change]})
        except ValueError as exc:
            raise ValidationError(str(exc), stage="plan", code="invalid_change_input") from exc
        if change["type"].startswith("instance."):
            old = next(item for item in prior["instances"] if item["name"] == change["target"])
            new_name = change["value"] if change["type"] == "instance.rename" else change["target"]
            new = next(item for item in current["instances"] if item["name"] == new_name)
            if change["type"] == "instance.set_parameter":
                for prop in new.get("properties") or []:
                    if prop.get("name") == change["value"]["name"]:
                        prop["value"] = change["value"]["value"]
            if old.get("bbox") is not None:
                new["bbox"] = transformed_box(old["bbox"], old, new)
            if change["type"] == "instance.set_master":
                key = "/".join(new["master"][field] for field in ("library", "cell", "view"))
                geometry = (masters or {}).get(key)
                if geometry is None or set(geometry["terminals"]) != set(new["connections"]):
                    raise ValidationError("New master needs verified matching terminals", stage="plan", code="terminal_mismatch")
                new["bbox"] = transformed_box(geometry["bbox"], {"origin": [0, 0], "orientation": "R0"}, new)
                new.pop("terminal_geometry", None)
            for records in new.get("terminal_geometry", {}).values():
                for record in records:
                    box = record["master_bbox"]
                    record["world_center"] = list(transform_point([(box[0][axis] + box[1][axis]) / 2 for axis in (0, 1)], new["origin"], new["orientation"]))
        for item in current["instances"]:
            item["bits"] = expand_signal(item["name"])
            item["bit_connections"] = bit_connections(item["connections"], instance_name=item["name"])
        for pin in current["pins"]:
            pin["bits"] = expand_signal(pin["name"])
            pin["bit_connections"] = bit_connections({pin["name"]: pin["net"]})
        if change["type"] == "pin.set":
            old = next(pin for pin in prior["pins"] if pin["name"] == change["target"])
            new = next(pin for pin in current["pins"] if pin["name"] == change["target"])
            offset = [new["position"][axis] - old["position"][axis] for axis in (0, 1)]
            for text in current["texts"]:
                owner = text.get("display_owner", {})
                if owner.get("name") == change["target"] and owner.get("type") in {"ScalarTerm", "VectorTerm", "BusTerm"}:
                    if text.get("origin") is not None:
                        text["origin"] = [text["origin"][axis] + offset[axis] for axis in (0, 1)]
                    if text.get("bbox") is not None:
                        text["bbox"] = [[point[axis] + offset[axis] for axis in (0, 1)] for point in text["bbox"]]
            for net in current["nets_detail"]:
                net["terminals"] = [{"name": pin["name"], "direction": pin["direction"]} for pin in current["pins"] if pin["net"] == net["name"]]
    routing_plan = {"nets": current["nets"], "pins": current["pins"], "wires": current["wires"]}
    contacts = [(item["connections"][terminal], tuple(records[0]["world_center"]), item["name"] + ":" + terminal)
                for item in current["instances"] for terminal, records in item.get("terminal_geometry", {}).items()
                if item.get("connections", {}).get(terminal) is not None]
    # A geometry delta invalidates every old segment endpoint.  Re-running the
    # bounded deterministic router from an empty wire set gives the native
    # candidate and its projection identical, complete physical connectivity.
    # Pure metadata/electrical deltas retain the existing segments and only
    # run the inexpensive electrical compiler.
    geometry_delta = has_geometry_delta(before, current, plan["changes"])
    explicit_wire_replacement = any(change["type"] == "wire.replace" for change in plan["changes"])
    if geometry_delta and not explicit_wire_replacement:
        from .routing import route_schematic

        route_source = {"grid": plan.get("grid", 0.0625), "instances": current["instances"], "nets": current["nets"], "pins": current["pins"], "wires": []}
        obstacles = [{"bbox": item["bbox"]} for item in current["instances"] if item.get("bbox") is not None]
        routed_result = route_schematic(route_source, contacts, obstacles)
        # Native readback exposes one line figure per segment.  Keep the
        # projection in that same canonical split form so comparison is exact.
        current["wires"] = routed_result["segments"]
    else:
        current["wires"] = compile_wires(routing_plan, contacts)
    return stable_readback(current)


def edit_comparison_content(readback, changes, *, native=False):
    value = stable_readback(readback)
    updated_parameters = {}
    changed_masters = set()
    replaces_labels = any(change["type"] == "label.replace" for change in changes)
    replaces_wires = any(change["type"] == "wire.replace" for change in changes)
    for change in changes:
        if change["type"] == "instance.set_parameter":
            updated_parameters.setdefault(change["target"], set()).add(change["value"]["name"])
        elif change["type"] == "instance.rename":
            updated_parameters[change["value"]] = updated_parameters.pop(change["target"], set())
            if change["target"] in changed_masters:
                changed_masters.remove(change["target"])
                changed_masters.add(change["value"])
        elif change["type"] == "instance.set_master":
            changed_masters.add(change["target"])
    for instance in value["instances"]:
        changed = updated_parameters.get(instance["name"], set())
        for name, parameter in instance.get("parameters", {}).items():
            try:
                instance["parameters"][name] = engineering(parameter)
            except ValidationError:
                pass
        provenance = instance.get("parameter_provenance")
        if isinstance(provenance, dict):
            parameters = instance.get("parameters", {})
            context = {}
            for name, parameter in parameters.items():
                entry = provenance.get(name, {})
                context[name] = entry.get("raw") if entry.get("value_state") == "derived_expression" else parameter
            for name in changed:
                context[name] = parameters[name]
            for name, entry in provenance.items():
                if entry.get("value_state") != "derived_expression" or name not in parameters:
                    continue
                try:
                    parameters[name] = resolve_parameter(name, context)["value"]
                    context[name] = parameters[name]
                except (ValueError, KeyError):
                    raise ValidationError("Derived parameter cannot be deterministically re-evaluated", stage="readback", code="parameter_provenance_mismatch")
            for name in changed:
                entry = provenance.get(name)
                if native and (not isinstance(entry, dict) or entry.get("resolved") is not True):
                    raise ValidationError("Changed parameter lacks resolved native provenance", stage="readback", code="parameter_provenance_mismatch")
                try:
                    matches = engineering(entry.get("raw")) == engineering(instance["parameters"][name])
                except ValidationError:
                    matches = entry.get("raw") == instance["parameters"][name]
                if native and (not matches or entry.get("override_source") != "instance_property"):
                    raise ValidationError("Changed parameter was not persisted as an instance property", stage="readback", code="parameter_provenance_mismatch")
                provenance[name] = {"source": "authorized_instance_property", "value": instance["parameters"][name]}
        if isinstance(instance.get("properties"), list):
            for prop in instance["properties"]:
                if prop.get("name") in changed:
                    parameter = instance["parameters"][prop["name"]]
                    try:
                        matches = engineering(prop.get("value")) == engineering(parameter)
                    except ValidationError:
                        matches = prop.get("value") == parameter
                    if not matches:
                        raise ValidationError("Parameter property disagrees with its native effective value", stage="readback", code="parameter_provenance_mismatch")
            instance["properties"] = [prop for prop in instance["properties"] if prop.get("name") not in changed]
            instance["properties"] = [{key: prop.get(key) for key in ("name", "value")} for prop in instance["properties"]]
        if instance["name"] in changed_masters:
            instance.pop("terminal_geometry", None)
            # Native Aether builds may retain the old instance bbox after a
            # master replacement even though terminal geometry and
            # connectivity are refreshed. The master and terminal geometry
            # are verified separately; the stale derived bbox is excluded
            # from the semantic comparison.
            instance.pop("bbox", None)
    if replaces_labels:
        for text in value.get("texts", []):
            if str(text.get("kind", "")).casefold() in {"text", "label"}:
                text["bbox"] = None
                if text.get("layer") == "wire" and text.get("purpose") == "label" and text.get("net"):
                    candidates = []
                    for wire in value.get("wires", []):
                        if wire.get("net") != text["net"]:
                            continue
                        for first, second in zip(wire.get("points", []), wire.get("points", [])[1:]):
                            if first[0] == second[0]:
                                projected = [first[0], min(max(text["origin"][1], min(first[1], second[1])), max(first[1], second[1]))]
                            elif first[1] == second[1]:
                                projected = [min(max(text["origin"][0], min(first[0], second[0])), max(first[0], second[0])), first[1]]
                            else:
                                continue
                            distance = sum((text["origin"][axis] - projected[axis]) ** 2 for axis in (0, 1))
                            candidates.append((distance, projected))
                    if candidates:
                        text["origin"] = min(candidates, key=lambda item: item[0])[1]
    content = schematic_content(value)
    if replaces_wires:
        # Aether may split a submitted orthogonal path at junctions and
        # materialize extra connectivity markers on reopen. The native
        # comparison separately runs compile_wires on the actual readback,
        # so semantic fields remain strict while segmentation is normalized.
        content["wires"] = []
    return content


def validate_connection_composition(readback, instance, connections, *, expected_widths=None):
    """Validate one vector/concat/repeat connection atomically.

    This is intentionally a pure helper used by planners and GUI front ends;
    it never opens a database.  The returned structure is suitable for an
    audit artifact and can be embedded directly as a ``connection.compose``
    change value.
    """
    expected_widths = expected_widths or {}
    source = next((item for item in readback.get("instances", []) if item.get("name") == instance), None)
    if source is None:
        raise ValidationError("Composition target instance does not exist", stage="plan", code="target_missing")
    if not isinstance(connections, dict) or set(connections) != set(source.get("connections", {})):
        raise ValidationError("Composition must provide every instance terminal", stage="plan", code="terminal_mismatch")
    normalized = {}
    expressions = {}
    for terminal, expression in connections.items():
        try:
            info = signal_expression_info(expression, expected_width=expected_widths.get(terminal), require_unique=False)
        except ValueError as exc:
            raise ValidationError(str(exc), stage="plan", code="invalid_signal_expression") from exc
        normalized[terminal] = info["canonical"]
        expressions[terminal] = info
    return {"type": "connection.compose", "target": instance,
            "value": {"connections": normalized, "expected_widths": dict(expected_widths)},
            "expressions": expressions, "native_scalar_only": any(item["width"] != 1 for item in expressions.values()),
            "publish_policy": "native_readback_required"}


def execute_schematic_edits(backend, design, plan):
    from .native import require

    api = backend.api
    for change in plan["changes"]:
        kind, target, value = change["type"], change["target"], change["value"]
        instances = {str(instance.name): instance for instance in design.instances if instance.pin is None}
        nets = {str(net.name): net for net in design.nets}
        if kind.startswith("instance.") or kind in {"connection.set", "connection.compose"}:
            native = instances[target]
            if kind == "instance.move":
                offset = [value[axis] - float(native.origin[axis]) for axis in (0, 1)]
                require(api.dbMoveFig(native, tuple(offset)), "dbMoveFig", "schematic")
            elif kind == "instance.rotate":
                original = str(native.orient)
                relative = next(orientation for orientation in sorted(ORIENTATIONS) if all(transform_point(transform_point(vector, [0, 0], original), [0, 0], orientation) == transform_point(vector, [0, 0], value) for vector in ([1, 0], [0, 1])))
                rotated = transform_point(native.origin, [0, 0], relative)
                offset = tuple(float(native.origin[axis]) - rotated[axis] for axis in (0, 1))
                require(api.dbMoveFig(native, offset, orient=relative), "dbMoveFig", "schematic")
            elif kind == "instance.rename":
                native.setName(api.emyScalarName(api.emyCdbaNS(), value))
            elif kind == "instance.set_master":
                names = [api.emyScalarName(api.emyCdbaNS(), value[field]) for field in ("library", "cell", "view")]
                native.setMaster(*names)
            elif kind == "instance.set_parameter":
                require(api.dbSetInstParam(native, value["name"], value["value"], mode=1), "dbSetInstParam", "schematic")
            elif kind == "instance.set_netset":
                require(api.addProp(native, value["net"], value["property"], True), "addProp", "schematic")
            else:
                requested = value.get("connections", {}) if kind == "connection.compose" else value
                for terminal in native.instTerms:
                    requested_net = requested[str(terminal.name)]
                    current_net = getattr(terminal, "net", None)
                    # Native terminals accept flattened signal names.  A
                    # composed expression is expanded and each bit is checked
                    # against the exact terminal occurrence; the expression is
                    # retained in the readback metadata by the projection gate.
                    requested_bits = expand_signal(requested_net)
                    if len(requested_bits) != 1:
                        raise ValidationError("Native connection composition needs a scalar terminal occurrence", stage="schematic", code="native_composition_unavailable")
                    requested_net = requested_bits[0]
                    if current_net is not None and str(current_net.name) == requested_net:
                        continue
                    require(terminal.addToNet(nets[requested_net]), "instTerm.addToNet", "schematic")
        elif kind == "inherited.create":
            terminal = next(item for item in design.terminals if str(item.name) == value["pin"])
            figures = [figure for pin in terminal.pins for figure in pin.figs]
            if len(figures) != 1 or terminal.net is None or str(terminal.net.name) != value["default"]:
                raise ValidationError("Inherited expression needs one pin on its declared default net", stage="schematic", code="ambiguous_selection")
            require(api.dbCrtNetExpr(cv=design, point=tuple(figures[0].origin), propName=target, defNetName=value["default"], fig=figures[0]), "dbCrtNetExpr", "schematic")
        elif kind == "pin.set":
            terminal = next(item for item in design.terminals if str(item.name) == target)
            figures = [figure for pin in terminal.pins for figure in pin.figs]
            if len(figures) != 1:
                raise ValidationError("Pin edit needs one unambiguous native figure", stage="schematic", code="ambiguous_selection")
            figure = figures[0]
            if "position" in value:
                offset = tuple(value["position"][axis] - float(figure.origin[axis]) for axis in (0, 1))
                require(api.dbMoveFig(figure, offset), "dbMoveFig", "schematic")
            if "orientation" in value:
                figure.setOrient(api.emyOrient(value["orientation"]))
            if "direction" in value:
                terminal.direction = value["direction"]
            if "net" in value:
                for pin in list(terminal.pins):
                    require(api.dbMovePinToNet(pin, nets[value["net"]]), "dbMovePinToNet", "schematic")
        elif kind == "wire.replace":
            for figure in list(design.shapes):
                if _safe_figure_type(figure) in {"line", "ellipse"}:
                    require(api.dbDelete(figure), "dbDelete", "schematic")
            # Split at same-net interior tees before creating native figures;
            # otherwise a top-level pin touching the middle of a long line is
            # reported as floating by Aether even though the abstract graph is
            # connected.
            target = {"library": str(design.libName), "cell": str(design.cellName), "view": str(design.viewName)}
            actual = backend.read(target)
            contacts = [(item["connections"][terminal], tuple(records[0]["world_center"]), item["name"] + ":" + terminal)
                        for item in actual["instances"] for terminal, records in item.get("terminal_geometry", {}).items()
                        if records and item.get("connections", {}).get(terminal) is not None]
            segments = compile_wires({"nets": sorted(nets), "pins": actual["pins"], "wires": value}, contacts)
            for wire in segments:
                for figure in require(api.dbCrtSchWire(design, wire["points"]), "dbCrtSchWire", "schematic"):
                    figure.addToNet(nets[wire["net"]])
                    require(api.dbAddFigToNet(figure, nets[wire["net"]]), "dbAddFigToNet", "schematic")
            # Re-assert top-level pin/net membership after replacing all wire
            # figures.  Some Aether builds detach a pin's native figure when
            # its last connected line is deleted; the logical net name alone
            # is not enough for Check&Save to recover it.
            for terminal in design.terminals:
                net = getattr(terminal, "net", None)
                if net is None or str(net.name) not in nets:
                    continue
                for pin in terminal.pins:
                    require(api.dbMovePinToNet(pin, nets[str(net.name)]), "dbMovePinToNet", "schematic")
        elif kind == "label.replace":
            for figure in list(design.shapes):
                object_type = str(getattr(figure, "objType", "")).casefold()
                if object_type in {"text", "label"}:
                    require(api.dbDelete(figure), "dbDelete", "schematic")
            for label in value:
                if label["kind"] == "note":
                    require(api.dbCrtSchNoteText(design, label["text"], tuple(label["origin"]), label["height"],
                                                 label["font"], True, label["justification"], False,
                                                 label["orientation"], False, None), "dbCrtSchNoteText", "schematic")
                    continue
                if label["kind"] == "wire_name":
                    wire = None
                    for figure in design.shapes:
                        if _safe_figure_type(figure) != "line" or getattr(figure, "net", None) is None:
                            continue
                        if str(figure.net.name) != label["net"]:
                            continue
                        points = [tuple(float(coordinate) for coordinate in position) for position in figure.points]
                        for first, second in zip(points, points[1:]):
                            on_vertical = first[0] == second[0] == label["origin"][0] and min(first[1], second[1]) <= label["origin"][1] <= max(first[1], second[1])
                            on_horizontal = first[1] == second[1] == label["origin"][1] and min(first[0], second[0]) <= label["origin"][0] <= max(first[0], second[0])
                            if on_vertical or on_horizontal:
                                wire = figure
                                break
                        if wire is not None:
                            break
                    if wire is None:
                        raise ValidationError("Wire-name annotation must be placed on a wire of its declared net", stage="schematic", code="ambiguous_selection")
                    require(api.dbCrtSchWireName(design, wire, label["text"], tuple(label["origin"]), label["height"],
                                                 label["font"], label["justification"], 6, False, label["orientation"], False),
                            "dbCrtSchWireName", "schematic")
                    continue
                figure = require(api.dbCrtLabel(design, tuple(label["origin"]), label["text"], label["height"],
                                                layer=label["layer"], purpose=label["purpose"], orient=label["orientation"],
                                                justify=label["justification"], font=label["font"]), "dbCrtLabel", "schematic")
                for prop in label.get("properties", []):
                    creator = getattr(api, "dbCrtProp", None)
                    if not callable(creator):
                        raise ValidationError("Native API cannot persist label properties", stage="schematic", code="native_property_unavailable")
                    native_value = prop["value"] if isinstance(prop["value"], str) else str(prop["value"])
                    converter = getattr(api, "emyString", None)
                    if callable(converter):
                        native_name = converter(prop["name"])
                        native_value = converter(native_value)
                        native_type = converter(prop.get("type", "string"))
                    else:
                        native_name = prop["name"]
                        native_type = prop.get("type", "string")
                    require(creator(figure, native_name, native_value, native_type), "dbCrtProp", "schematic")
