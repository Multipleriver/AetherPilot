"""Official PyAether adapter; imported only inside the product runtime."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

from .artifacts import atomic_json, file_digest
from .contract import design_digest, digest, engineering
from .errors import OperationError, PreflightError, SessionError, ValidationError
from .planner import compile_wires, segment_key, transform_point
from .readback import stable_readback
from .layout import compare_layout_plan, layout_content, layout_summary, select_layout_objects, stable_layout_readback, validate_layout_plan
from .signals import netset_overrides, parse_inherited_expression
from .schematic_content import schematic_content


OWNER_PROPERTY = "aetherpilot_owner"

# Product-provided schematic primitives used by the certified OTA fixture.
# These entries are keyed by the native master identity, never by instance
# names.  The live symbol terminal set is checked before a proof is emitted;
# a missing/mismatched master therefore remains an explicit incomplete leaf.
_NATIVE_PRIMITIVE_CATALOG = {
    ("analog", "res"): {"primitive_type": "resistor", "terminal_order": ["P", "N"]},
    ("analog", "cap"): {"primitive_type": "capacitor", "terminal_order": ["P", "N"]},
    ("analog", "vdc"): {"primitive_type": "voltage_source", "terminal_order": ["P", "N"]},
    ("analog", "vsin"): {"primitive_type": "voltage_source", "terminal_order": ["P", "N"]},
    ("basic", "gnd"): {"primitive_type": "ground", "terminal_order": ["gnd!"]},
}


def require(value, call, stage="schematic"):
    if not value:
        raise OperationError(f"{call} did not succeed", stage=stage, code="native_false")
    return value


def content(readback):
    instances = []
    for item in readback.get("instances", []):
        instances.append({key: item.get(key) for key in ("name", "master", "origin", "orientation", "parameters", "connections")})
    pins = []
    for item in readback.get("pins", []):
        pins.append({key: item.get(key) for key in ("name", "net", "direction", "position", "orientation")})
    wires = [{"net": item.get("net"), "points": item.get("points")} for item in readback.get("wires", [])]
    return {"technology": readback.get("technology"), "instances": sorted(instances, key=lambda item: item["name"]),
            "pins": sorted(pins, key=lambda item: item["name"]), "nets": sorted(readback.get("nets", [])),
            "wires": sorted(wires, key=lambda item: (item["net"], item["points"]))}


def _safe_attr(value, name, default=None):
    try:
        return getattr(value, name)
    except Exception:
        return default


def _text(value, default=None):
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _point(value):
    try:
        return [round(float(value[0]), 9), round(float(value[1]), 9)]
    except Exception:
        return None


def _bbox(value):
    try:
        first, second = value
        return [_point(first), _point(second)]
    except Exception:
        return None


def _figure_center(value):
    """Read a figure origin across PyAether shape wrapper variants.

    Some product primitive symbols expose terminal pin figures as ``emyRect``
    objects, which have ``listBbox`` but no ``origin`` attribute.  A terminal
    position is still well-defined by the bbox center; returning ``None`` for
    an opaque figure lets the caller report an incomplete readback instead of
    leaking an AttributeError from the native wrapper.
    """
    origin = _safe_attr(value, "origin")
    if callable(origin):
        try:
            origin = origin()
        except Exception:
            origin = None
    point = _point(origin)
    if point is not None:
        return point
    box = _bbox(_safe_attr(value, "listBbox"))
    if box is None or box[0] is None or box[1] is None:
        return None
    return [round((box[0][axis] + box[1][axis]) / 2, 9) for axis in (0, 1)]


def _native_value(value, depth=0):
    if depth > 4:
        return {"type": type(value).__name__, "unsupported": True}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if type(value).__name__ == "emyVectorF":
        try:
            return [_native_value(value.x(), depth + 1), _native_value(value.y(), depth + 1)]
        except Exception:
            return {"type": type(value).__name__, "unsupported": True}
    if type(value).__name__ in {"emyString", "emyType", "emyOrient", "emyTermType", "emyFont", "emyTextAlign", "emyPlacementStatus", "emyPathStyle", "emyMode"}:
        return str(value)
    if type(value).__name__.startswith(("vector1D_", "vector2D_", "emyCollection_", "emyArray_")):
        import itertools

        items = list(itertools.islice(value, 10001))
        if len(items) > 10000:
            return {"type": type(value).__name__, "unsupported": True, "reason": "collection budget exceeded"}
        return [_native_value(item, depth + 1) for item in items]
    if isinstance(value, (list, tuple)):
        return [_native_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _native_value(item, depth + 1) for key, item in value.items()}
    result = {}
    for name in ("name", "value", "type", "propType", "kind", "isNetSet", "netExpression", "numBits", "direction", "orient", "origin", "objType"):
        item = _safe_attr(value, name)
        if item is not None:
            result[name] = _native_value(item, depth + 1)
    if result:
        return result
    return {"type": type(value).__name__, "unsupported": True}


def _native_properties(value):
    collected = []
    getter = _safe_attr(value, "getProps")
    if callable(getter):
        try:
            collected = _native_value(getter())
        except Exception:
            collected = []
    direct = _native_value(_safe_attr(value, "prop"))
    values = []
    for item in (collected, direct):
        if isinstance(item, list):
            values.extend(item)
        elif isinstance(item, dict) and item:
            values.append(item)
    unique = {}
    for item in values:
        if isinstance(item, dict):
            key = json.dumps({name: item.get(name) for name in ("name", "value", "type", "propType", "kind")}, sort_keys=True, allow_nan=False)
            unique[key] = item
    return list(unique.values())


def _opaque_fields(value, path=()):
    found = []
    if isinstance(value, dict):
        if value.get("unsupported") is True:
            found.append({"kind": "opaque_native_value", "path": list(path), "native_type": value.get("type")})
        else:
            for key, item in value.items():
                found.extend(_opaque_fields(item, path + (key,)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_opaque_fields(item, path + (index,)))
    return found


def _expression_values(value):
    value = _native_value(value)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_expression_values(item))
        return result
    if isinstance(value, dict):
        for key in ("netExpression", "value", "raw"):
            if key in value:
                return _expression_values(value[key])
    return []


def _parse_native_expression(api, raw):
    definition = parse_inherited_expression(raw)
    parser = _safe_attr(api, "dbParseSigNetExpr")
    if callable(parser):
        parsed = _native_value(parser(raw))
        if not isinstance(parsed, list) or len(parsed) != 4 or parsed[0] != "@" or parsed[2] != "%":
            raise ValueError("native parser rejected inherited connection expression")
        if parsed[1] != definition["property"] or parsed[3] != definition["default"]:
            raise ValueError("native parser disagrees with inherited connection expression")
        definition["native_parser"] = "dbParseSigNetExpr"
    return definition


def _native_inherited_expressions(api, design, texts, inherited_objects):
    evidence = []
    getter_specs = []
    for net in _safe_attr(design, "nets", []) or []:
        for signal in _safe_attr(net, "signals", []) or []:
            getter_specs.append(("dbGetSigNetExpr", signal, {"net": _text(_safe_attr(net, "name")), "signal": _text(_safe_attr(signal, "name"))}))
    for terminal in _safe_attr(design, "terminals", []) or []:
        getter_specs.append(("dbGetTermNetExpr", terminal, {"terminal": _text(_safe_attr(terminal, "name"))}))
    for getter_name, obj, location in getter_specs:
        getter = _safe_attr(api, getter_name)
        if not callable(getter):
            continue
        try:
            values = _expression_values(getter(obj))
        except Exception:
            continue
        for raw in values:
            if raw:
                evidence.append({"raw": raw, "source": getter_name, **{key: value for key, value in location.items() if value is not None}})
    for text in texts:
        for prop in text.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name") == "netExpression" and isinstance(prop.get("value"), str):
                evidence.append({"raw": prop["value"], "source": "netExpression_property", "text_id": text.get("text_id")})

    definitions = {}
    issues = []
    for item in evidence:
        raw = item["raw"]
        try:
            definition = _parse_native_expression(api, raw)
        except (TypeError, ValueError) as exc:
            issues.append({"kind": "inherited_connections", "reason": str(exc), "raw": raw, "source": item.get("source")})
            continue
        key = (definition["property"], definition["default"], definition["raw"])
        existing = definitions.setdefault(key, {**definition, "evidence": []})
        existing["evidence"].append({key: value for key, value in item.items() if key != "raw"})
    if inherited_objects and not definitions:
        issues.append({"kind": "inherited_connections", "reason": "native inherited signals have no readable netExpression definition"})
    return list(definitions.values()), issues


def _master_inherited_definitions(api, master, cache):
    key = (master.get("library"), master.get("cell"), "schematic")
    if key in cache:
        return cache[key]
    existing = _safe_attr(api, "dbFindCV")(key[0], key[1], key[2]) if callable(_safe_attr(api, "dbFindCV")) else None
    design = existing
    opened = False
    try:
        if design is None:
            opener = _safe_attr(api, "dbOpenCV")
            design = opener(key[0], key[1], key[2], "r") if callable(opener) else None
            opened = design is not None
        if design is None:
            cache[key] = ([], [])
            return cache[key]
        inherited_objects = _native_value(api.dbGetCellViewInheritedSig(design))
        definitions, issues = _native_inherited_expressions(api, design, [], inherited_objects)
        cache[key] = (definitions, issues)
        return cache[key]
    except Exception as exc:
        cache[key] = ([], [{"kind": "netSet", "reason": type(exc).__name__}])
        return cache[key]
    finally:
        if opened:
            closer = _safe_attr(api, "dbCloseCV")
            if callable(closer):
                closer(design)


def _instance_netset_overrides(api, instance, master, cache):
    definitions, issues = _master_inherited_definitions(api, master, cache)
    getter = _safe_attr(api, "getProp")
    if not callable(getter):
        if definitions:
            issues = list(issues) + [{"kind": "netSet", "object": _text(_safe_attr(instance, "name")), "reason": "native getProp API unavailable"}]
        return {}, issues
    overrides = {}
    for definition in definitions:
        property_name = definition["property"]
        try:
            value = getter(instance, property_name)
        except Exception as exc:
            return {}, list(issues) + [{"kind": "netSet", "object": _text(_safe_attr(instance, "name")), "property": property_name, "reason": type(exc).__name__}]
        if value is not None:
            value = _native_value(value)
            if not isinstance(value, (str, dict)):
                return {}, list(issues) + [{"kind": "netSet", "object": _text(_safe_attr(instance, "name")), "property": property_name, "reason": "native netSet value is not a signal"}]
            overrides[property_name] = value
    return overrides, issues


def _instance_params(api, instance, requested):
    if requested is not None:
        values = {}
        provenance = {}
        for name in requested:
            definition = api.dbGetInstParam(instance, name)
            values[name] = _text(_safe_attr(definition, "value")) if definition is not None else None
            provenance[name] = {"source": "instance_or_default", "resolved": definition is not None}
        return values, provenance, []
    unsupported = []
    values = {}
    provenance = {}
    try:
        params = api.emyParamArray()
        instance.getParams(params)
        count = _safe_attr(params, "getNumElements")
        count = count() if callable(count) else None
        if count is None:
            unsupported.append({"kind": "parameters", "object": _text(_safe_attr(instance, "name")), "reason": "PyAether does not expose parameter names from this instance"})
        else:
            for index in range(int(count)):
                item = params.get(index)
                native_name = api.emyString()
                item.getName(native_name)
                name = str(native_name)
                if not name:
                    unsupported.append({"kind": "parameters", "object": _text(_safe_attr(instance, "name")), "reason": "native parameter has no stable name", "index": index})
                    continue
                value = _safe_attr(params, "getParam")
                value = value(name) if callable(value) else None
                values[name] = _native_value(value)
                provenance[name] = {"source": "native_instance_getParams", "resolved": True}
        cdf_parameters = instance.cdfParameter
        if cdf_parameters is not None:
            for definition in cdf_parameters:
                name = str(definition.name)
                raw = _text(definition.value)
                effective_definition = api.dbGetInstParam(instance, name)
                effective = _text(_safe_attr(effective_definition, "value"), raw)
                override = api.dbGetPropByName(instance, name)
                default = _text(_safe_attr(definition, "defValue"))
                try:
                    engineering(effective)
                    resolved = True
                except ValidationError:
                    from .contract import parameter_literal

                    try:
                        parameter_literal(effective, name)
                        resolved = True
                    except ValidationError:
                        resolved = effective in (None, "")
                source = "instance_property" if override is not None else "master_default" if default == effective else "native_effective_cdf"
                chain = [{"source": "master_cdf", "raw": default}]
                if override is not None:
                    chain.append({"source": "instance_property", "raw": _native_value(_safe_attr(override, "value"))})
                chain.append({"source": "native_effective_cdf", "value": effective})
                values[name] = effective
                provenance[name] = {"source": source, "raw": raw, "default": default, "override_chain": chain,
                                    "override_source": source, "resolved": resolved, "value_state": "unset" if effective in (None, "") else "literal" if resolved else "unresolved_expression",
                                    "scope": "persisted_properties_and_native_effective_cdf"}
                if not resolved:
                    unsupported.append({"kind": "parameter_expression", "object": str(instance.name), "parameter": name, "reason": "native effective value remains an expression or unsupported value"})
        for name, value in values.items():
            if isinstance(value, dict) and value.get("unsupported"):
                unsupported.append({"kind": "parameters", "object": str(instance.name), "parameter": name, "reason": "native parameter value is opaque"})
        from .parameters import resolve_parameter

        for issue in list(unsupported):
            if issue.get("kind") != "parameter_expression":
                continue
            name = issue["parameter"]
            try:
                evaluation = resolve_parameter(name, values)
            except ValueError:
                continue
            values[name] = format(evaluation["value"], ".17g")
            provenance[name].update(resolved=True, value_state="derived_expression", evaluation=evaluation)
            unsupported.remove(issue)
    except Exception as exc:
        unsupported.append({"kind": "parameters", "object": _text(_safe_attr(instance, "name")), "reason": type(exc).__name__})
    return values, provenance, unsupported


class LibraryOps:
    def __init__(self, api):
        self.api = api

    def list(self):
        try:
            libraries = self.api.dbGetAllLibs()
        except Exception as exc:
            raise PreflightError("Cannot enumerate native libraries; check Aether license and session", stage="preflight", code="library_query_failed", details={"exception": type(exc).__name__}) from exc
        if libraries is None:
            raise PreflightError("Native library enumeration returned no result; check Aether license and session", stage="preflight", code="license_unavailable")
        return sorted(str(item) for item in libraries)

    def cells(self, library):
        cells = self.api.dbGetAllCells(library)
        return sorted(str(item) for item in (cells or []))

    def views(self, library, cell):
        views = self.api.dbGetAllViews(library, cell)
        return sorted(str(item) for item in (views or []))

    def exists(self, target):
        return target["library"] in self.list() and target["cell"] in self.cells(target["library"]) and target["view"] in self.views(target["library"], target["cell"])

    def require_unlocked(self, target):
        if self.api.dbCVIsLocked(target["library"], target["cell"], target["view"], "layout" if target["view"] == "layout" else "schematic"):
            raise SessionError("Target has a native database lock; no lock is removed", stage="ownership", code="native_view_locked")

    def ensure(self, target):
        if target["technology"] not in self.list():
            raise PreflightError("Technology library is not available", stage="preflight", code="pdk_missing")
        if target["library"] not in self.list():
            require(self.api.dbCreateLib(lib=target["library"], attachTechLib=target["technology"]), "dbCreateLib")
            return True
        return False


class ValidationOps:
    @staticmethod
    def _annotations(value):
        annotations = []
        for item in value.get("labels", []) + value.get("texts", []):
            kind = str(item.get("kind", "")).casefold()
            if kind in {"attrdisplay", "propdisplay", "instattrdisplay", "evaltext"}:
                continue
            layer, purpose = item.get("layer"), item.get("purpose")
            annotation_kind = "wire_name" if layer == "wire" and purpose == "label" and item.get("net") else "note" if layer == "text" and purpose == "drawing" else "label"
            properties = []
            for prop in item.get("properties", []):
                if not isinstance(prop, dict):
                    continue
                record = {"name": prop.get("name"), "value": str(prop.get("value"))}
                if prop.get("isNetSet") is True:
                    record["isNetSet"] = True
                properties.append(record)
            annotations.append({"kind": annotation_kind, "text": item.get("text"), "layer": layer, "purpose": purpose,
                                "net": item.get("net"), "origin": item.get("origin"), "height": item.get("height"),
                                "orientation": item.get("orientation"), "justification": item.get("justification"), "font": item.get("font"),
                                "properties": properties})
        return annotations

    @staticmethod
    def compare(plan, actual, segments):
        failures = []
        expected_instances = {item["name"]: item for item in plan["instances"]}
        actual_instances = {item["name"]: item for item in actual["instances"]}
        if expected_instances.keys() != actual_instances.keys():
            failures.append("instance set")
        for name in expected_instances.keys() & actual_instances.keys():
            expected, found = expected_instances[name], actual_instances[name]
            for key in ("master", "connections", "orientation", "origin"):
                if expected[key] != found[key]:
                    failures.append(f"instance {name}: {key}")
            for key, value in expected["parameters"].items():
                read_value = found["parameters"].get(key)
                try:
                    matches = read_value is not None and math.isclose(engineering(value), engineering(read_value), rel_tol=1e-9, abs_tol=1e-24)
                except ValidationError:
                    matches = value == read_value
                if not matches:
                    failures.append(f"instance {name}: parameter {key}")
        expected_pins = [{key: item.get(key) for key in ("name", "net", "direction", "position", "orientation")} for item in plan["pins"]]
        actual_pins = [{key: item.get(key) for key in ("name", "net", "direction", "position", "orientation")} for item in actual["pins"]]
        if sorted(expected_pins, key=lambda item: item["name"]) != sorted(actual_pins, key=lambda item: item["name"]):
            failures.append("pins, directions or positions")
        if "port_order" in plan and plan["port_order"] != actual.get("port_order"):
            failures.append("port order")
        if sorted(plan["nets"]) != actual["nets"]:
            failures.append("net set")
        expected_inherited = sorted((item["property"], item["default"]) for item in plan.get("inherited_connections", []))
        actual_inherited = sorted((item.get("property"), item.get("default")) for item in actual.get("inherited_connections", []))
        if expected_inherited != actual_inherited:
            failures.append("inherited network expressions")
        expected_annotations = []
        for item in plan.get("labels", []):
            expected = {key: item.get(key) for key in ("kind", "text", "layer", "purpose", "net", "orientation", "justification", "font")}
            expected["properties"] = []
            for prop in item.get("properties", []):
                record = {"name": prop.get("name"), "value": str(prop.get("value"))}
                if prop.get("isNetSet") is True:
                    record["isNetSet"] = True
                expected["properties"].append(record)
            expected_annotations.append(expected)
        actual_annotations = [{key: item.get(key) for key in ("kind", "text", "layer", "purpose", "net", "orientation", "justification", "font", "properties")}
                              for item in ValidationOps._annotations(actual)]
        if sorted(expected_annotations, key=lambda item: (item["kind"], item["text"] or "")) != sorted(actual_annotations, key=lambda item: (item["kind"], item["text"] or "")):
            failures.append("schematic annotations")
        actual_wires = [{"net": item.get("net"), "points": item.get("points")} for item in actual["wires"]]
        if segments != sorted(actual_wires, key=lambda item: (item["net"], item["points"])):
            failures.append("wire geometry or net membership")
        if actual["technology"] != plan["target"]["technology"]:
            failures.append("technology binding")
        if failures:
            raise ValidationError("Database readback differs from the plan", stage="readback", code="readback_mismatch", details={"mismatches": failures})
        return {"ok": True, "checks": ["instances", "masters", "parameters", "connectivity", "pins", "wires", "technology"]}


class SchematicOps:
    def __init__(self, api):
        self.api = api
        self.library = LibraryOps(api)

    def open(self, target, mode="r"):
        return require(self.api.dbOpenCV(target["library"], target["cell"], target["view"], mode), "dbOpenCV", "readback")

    def close(self, design):
        require(self.api.dbCloseCV(design), "dbCloseCV", "readback")

    def master_contacts(self, plan):
        contacts = []
        libraries = LibraryOps(self.api)
        for instance in plan["instances"]:
            target = instance["master"]
            if not libraries.exists(target):
                raise PreflightError("Instance master is unavailable", stage="preflight", code="master_missing", details={"instance": instance["name"]})
            design = self.open(target)
            try:
                terms = {str(term.name): term for term in design.terminals}
                if set(terms) != set(instance["connections"]):
                    raise ValidationError("Plan must connect exactly the master terminals", stage="plan", code="terminal_mismatch", details={"instance": instance["name"], "master_terminals": sorted(terms)})
                for name, term in terms.items():
                    positions = []
                    for pin in term.pins:
                        for figure in pin.figs:
                            first, second = figure.listBbox
                            positions.append(tuple(round((first[axis] + second[axis]) / 2, 9) for axis in (0, 1)))
                    if not positions:
                        raise ValidationError("Master terminal has no physical pin", stage="plan", code="missing_pin_geometry")
                    position = transform_point(sorted(set(positions))[0], instance["origin"], instance["orientation"])
                    contacts.append((instance["connections"][name], position, instance["name"] + ":" + name))
            finally:
                self.close(design)
        return contacts

    def master_geometry(self, plan):
        geometry = {}
        for instance in plan["instances"]:
            target = instance["master"]
            key = "/".join(target[field] for field in ("library", "cell", "view"))
            if key in geometry:
                continue
            existing = self.api.dbFindCV(target["library"], target["cell"], target["view"])
            design = existing if existing is not None else self.open(target)
            try:
                terminals = {}
                for terminal in design.terminals:
                    boxes = [_bbox(figure.listBbox) for pin in terminal.pins for figure in pin.figs]
                    if not boxes or any(box is None for box in boxes):
                        raise ValidationError("Master has missing terminal geometry", stage="plan", code="master_geometry_missing")
                    positions = sorted(tuple(round((box[0][axis] + box[1][axis]) / 2, 9) for axis in (0, 1)) for box in boxes)
                    terminals[str(terminal.name)] = list(positions[0])
                box = _bbox(design.listBbox)
                file_name = _text(_safe_attr(design, "fileName"))
                if box is None or not file_name or not Path(file_name).is_file():
                    raise ValidationError("Master geometry needs a native source fingerprint", stage="plan", code="master_geometry_missing")
                body_boxes = [_bbox(shape.listBbox) for shape in design.shapes
                              if str(shape.purpose) == "drawing" and str(shape.objType).casefold() in {"line", "path", "rect", "polygon", "ellipse", "arc"}]
                if not body_boxes or any(body is None for body in body_boxes):
                    raise ValidationError("Master has no complete routing body geometry", stage="plan", code="master_geometry_missing")
                body_box = [[min(body[0][axis] for body in body_boxes) for axis in (0, 1)],
                            [max(body[1][axis] for body in body_boxes) for axis in (0, 1)]]
                geometry[key] = {"bbox": box, "body_bbox": body_box, "terminals": terminals, "database_sha256": file_digest(file_name)}
            finally:
                if existing is None:
                    self.close(design)
        return geometry

    def build(self, plan, target, segments, owner, report):
        design = require(self.api.dbNewCV(target["library"], target["cell"], target["view"], "schematic"), "dbNewCV")
        try:
            require(self.api.dbCrtProp(design, OWNER_PROPERTY, owner, "string"), "dbCrtProp")
            instances = {}
            for item in plan["instances"]:
                parameters = [entry for name, value in sorted(item["parameters"].items()) for entry in (name, value)]
                instance = require(self.api.dbCrtInst(
                    design, tuple(item["origin"]), lib=item["master"]["library"], cell=item["master"]["cell"],
                    view=item["master"]["view"], inst=item["name"], orient=item["orientation"], params=parameters,
                ), "dbCrtInst")
                instances[item["name"]] = instance
            nets = {name: require(self.api.dbCrtNet(design, name), "dbCrtNet") for name in plan["nets"]}
            # Bind pins and instance terminals before drawing wires.  For
            # generated hierarchical symbols, PyAether otherwise creates
            # temporary child nets while a wire endpoint is attached and can
            # leave D/S on auto-created net0/net1 objects after save/reopen.
            pins_by_name = {item["name"]: item for item in plan["pins"]}
            for name in plan.get("port_order", list(pins_by_name)):
                item = pins_by_name[name]
                require(self.api.dbCrtSchPin(design, item["name"], tuple(item["position"]), item["direction"], False, item["orientation"]), "dbCrtSchPin")
            for item in plan["instances"]:
                for terminal, net in sorted(item["connections"].items()):
                    require(self.api.dbCrtInstTermByName(nets[net], instances[item["name"]], terminal), "dbCrtInstTermByName")
            segment_figures = {}
            for segment in segments:
                figures = require(self.api.dbCrtSchWire(design, segment["points"]), "dbCrtSchWire")
                for figure in figures:
                    figure.addToNet(nets[segment["net"]])
                    require(self.api.dbAddFigToNet(figure, nets[segment["net"]]), "dbAddFigToNet")
                    segment_figures.setdefault((segment["net"], tuple(tuple(point) for point in segment["points"])), []).append(figure)
            for definition in plan.get("inherited_connections", []):
                terminal = next((item for item in design.terminals if str(item.name) == definition["pin"]), None)
                if terminal is None or terminal.net is None or str(terminal.net.name) != definition["default"]:
                    raise ValidationError("Inherited expression needs one pin on its declared default net", stage="schematic", code="ambiguous_selection")
                figures = [figure for pin in terminal.pins for figure in pin.figs]
                if len(figures) != 1:
                    raise ValidationError("Inherited expression needs one unambiguous pin figure", stage="schematic", code="ambiguous_selection")
                require(self.api.dbCrtNetExpr(cv=design, point=tuple(figures[0].origin), propName=definition["property"],
                                               defNetName=definition["default"], fig=figures[0]), "dbCrtNetExpr")
            for label in plan.get("labels", []):
                if label["kind"] == "note":
                    require(self.api.dbCrtSchNoteText(design, label["text"], tuple(label["origin"]), label["height"],
                                                      label["font"], True, label["justification"], False,
                                                      label["orientation"], False, None), "dbCrtSchNoteText")
                    continue
                if label["kind"] == "wire_name":
                    wire = None
                    for (net, points), figures in segment_figures.items():
                        if net != label["net"]:
                            continue
                        first, second = points
                        on_vertical = first[0] == second[0] == label["origin"][0] and min(first[1], second[1]) <= label["origin"][1] <= max(first[1], second[1])
                        on_horizontal = first[1] == second[1] == label["origin"][1] and min(first[0], second[0]) <= label["origin"][0] <= max(first[0], second[0])
                        if on_vertical or on_horizontal:
                            wire = figures[0]
                            break
                    if wire is None:
                        raise ValidationError("Wire-name annotation must be placed on a generated wire", stage="schematic", code="ambiguous_selection")
                    require(self.api.dbCrtSchWireName(design, wire, label["text"], tuple(label["origin"]), label["height"],
                                                      label["font"], label["justification"], 6, False,
                                                      label["orientation"], False), "dbCrtSchWireName")
                    continue
                figure = require(self.api.dbCrtLabel(design, tuple(label["origin"]), label["text"], label["height"],
                                                     layer=label["layer"], purpose=label["purpose"], orient=label["orientation"],
                                                     justify=label["justification"], font=label["font"]), "dbCrtLabel")
                for prop in label.get("properties", []):
                    creator = getattr(self.api, "dbCrtProp", None)
                    if not callable(creator):
                        raise ValidationError("Native API cannot persist label properties", stage="schematic", code="native_property_unavailable")
                    # PyAether's dbCrtProp binding requires an emyString for
                    # the value even when the declared OA property type is
                    # string.  Keep the public contract scalar and convert at
                    # the native boundary; the original value remains in the
                    # plan/readback evidence.
                    native_value = prop["value"]
                    if not isinstance(native_value, str):
                        native_value = str(native_value)
                    converter = getattr(self.api, "emyString", None)
                    if callable(converter):
                        native_name = converter(prop["name"])
                        native_value = converter(native_value)
                        native_type = converter(prop.get("type", "string"))
                    else:
                        native_name = prop["name"]
                        native_type = prop.get("type", "string")
                    require(creator(figure, native_name, native_value, native_type), "dbCrtProp")
            require(self.api.dbCheckAndSaveDesign(design, str(report)), "dbCheckAndSaveDesign", "check_save")
            if self.api.dbHasMarker(design, "error"):
                raise ValidationError("Native checker reported errors", stage="check_save", code="native_check_failed")
            if self.api.dbHasMarker(design, "warning") and not plan.get("routing_policy", {}).get("allow_check_warnings", False):
                raise ValidationError("Native checker reported warnings; refusing to publish", stage="check_save", code="native_check_warnings")
            require(self.api.dbSaveCV(design), "dbSaveCV", "check_save")
        finally:
            self.close(design)

    def read(self, target, parameters=None, *, include_geometry=True, include_properties=True):
        existing = self.api.dbFindCV(target["library"], target["cell"], target.get("view", "schematic"))
        design = existing if existing is not None else self.open(target)
        try:
            source_file = _text(_safe_attr(design, "fileName"))
            source_path = Path(source_file) if source_file else None
            source_hash = file_digest(source_path) if source_path and source_path.is_file() else None
            counter_api = _safe_attr(self.api, "dbGetDesignCounter")
            initial_counter = counter_api(design) if callable(counter_api) else None
            instances = []
            unsupported = []
            inherited_definition_cache = {}
            for instance in design.instances:
                if instance.pin is not None:
                    continue
                name = str(instance.name)
                values, provenance, parameter_issues = _instance_params(self.api, instance, (parameters or {}).get(name))
                unsupported.extend(parameter_issues)
                item = {
                    "name": name,
                    "master": {"library": str(instance.libName), "cell": str(instance.cellName), "view": str(instance.viewName)},
                    "origin": [round(float(value), 9) for value in instance.origin],
                    "orientation": str(instance.orient),
                    "parameters": values,
                    "parameter_provenance": provenance,
                    "connections": {str(term.name): str(term.net.name) if term.net is not None else None for term in instance.instTerms},
                    "num_bits": int((_safe_attr(instance, "getNumBits")() if callable(_safe_attr(instance, "getNumBits")) else _safe_attr(instance, "numBits", 1)) or 1),
                    "placement_status": _text((_safe_attr(instance, "getPlacementStatus")() if callable(_safe_attr(instance, "getPlacementStatus")) else _safe_attr(instance, "placementStatus"))),
                }
                if include_geometry:
                    item["bbox"] = _bbox(_safe_attr(instance, "listBbox"))
                    item["terminal_geometry"] = {}
                    for native_term in instance.instTerms:
                        method = _safe_attr(native_term, "getTerm")
                        master_term = method() if callable(method) else None
                        boxes = [_bbox(_safe_attr(figure, "listBbox")) for pin in (_safe_attr(master_term, "pins", []) or []) for figure in pin.figs]
                        if not boxes or any(box is None for box in boxes):
                            unsupported.append({"kind": "terminal_geometry", "object": name, "terminal": str(native_term.name), "reason": "native master terminal geometry unavailable"})
                            continue
                        item["terminal_geometry"][str(native_term.name)] = [{"master_bbox": box, "world_center": list(transform_point([(box[0][axis] + box[1][axis]) / 2 for axis in (0, 1)], item["origin"], item["orientation"]))} for box in boxes]
                    if item["bbox"] is None:
                        unsupported.append({"kind": "instance_geometry", "object": name, "reason": "native instance bounding box unavailable"})
                if include_properties:
                    item["properties"] = _native_properties(instance)
                    overrides, netset_issues = _instance_netset_overrides(self.api, instance, item["master"], inherited_definition_cache)
                    unsupported.extend(netset_issues)
                    if overrides:
                        item["netset_overrides"] = overrides
                instances.append(item)
            pins = []
            for terminal in design.terminals:
                figures = []
                if include_geometry:
                    terminal_figures = [figure for pin in terminal.pins for figure in pin.figs]
                    instance_figures = [figure for figure in terminal_figures if callable(_safe_attr(figure, "isInst")) and figure.isInst()]
                    figures = instance_figures or terminal_figures
                if len(figures) != 1:
                    unsupported.append({"kind": "pin_geometry", "object": _text(terminal.name), "reason": "expected one schematic figure per terminal"})
                figure = figures[0] if len(figures) == 1 else None
                pin_position = _figure_center(figure) if figure is not None else None
                if include_geometry and figure is not None and pin_position is None:
                    unsupported.append({"kind": "pin_geometry", "object": _text(terminal.name), "reason": "native terminal figure has no origin or bbox"})
                pins.append({"name": str(terminal.name), "net": str(terminal.net.name) if terminal.net is not None else None,
                             "direction": str(terminal.direction), "position": pin_position,
                             "orientation": _text(_safe_attr(figure, "orient")) if figure is not None else None, "num_bits": int((_safe_attr(terminal, "getNumBits")() if callable(_safe_attr(terminal, "getNumBits")) else _safe_attr(terminal, "numBits", 1)) or 1),
                             "properties": _native_properties(terminal) if include_properties else None})
            segments = []
            labels = []
            texts = []
            figures = []
            for shape_index, figure in enumerate(design.shapes if include_geometry else []):
                object_type = _text(_safe_attr(figure, "objType"), "unknown")
                if object_type.casefold() == "line":
                    points = [tuple(round(float(value), 9) for value in point) for point in figure.points]
                    if figure.net is None:
                        unsupported.append({"kind": "wire", "object": str(shape_index), "reason": "unassigned wire"})
                        continue
                    segments.append(segment_key(str(figure.net.name), *points))
                elif object_type.casefold() in {"label", "text", "attrdisplay", "propdisplay", "evaltext", "instattrdisplay"}:
                    value = {
                        "kind": object_type,
                        "text": _text(_safe_attr(figure, "theLabel", _safe_attr(figure, "text", "")), ""),
                        "origin": _point(_safe_attr(figure, "origin")),
                        "orientation": _text(_safe_attr(figure, "orient")),
                        "properties": _native_properties(figure) if include_properties else None,
                        "bbox": _bbox(_safe_attr(figure, "listBbox")),
                        "height": _safe_attr(figure, "height"), "font": _text(_safe_attr(figure, "font")),
                        "justification": _text(_safe_attr(figure, "justify")),
                        "layer": _text(_safe_attr(figure, "layerName")), "purpose": _text(_safe_attr(figure, "purpose")),
                    }
                    native_net = _safe_attr(figure, "net")
                    if native_net is not None:
                        value["net"] = _text(_safe_attr(native_net, "name"))
                    if object_type.casefold().endswith("display") or object_type.casefold() == "evaltext":
                        owner = _safe_attr(figure, "owner")
                        value["display_owner"] = {"name": _text(_safe_attr(owner, "name")), "type": _text(_safe_attr(owner, "objType"))}
                        value["attribute"] = _text(_safe_attr(figure, "assocType"))
                    if any(value[key] is None for key in ("origin", "orientation", "bbox", "height", "font", "justification", "layer", "purpose")):
                        unsupported.append({"kind": "text_geometry", "object": str(shape_index), "reason": "incomplete native text attributes"})
                    if object_type.casefold() == "label":
                        labels.append(value)
                    else:
                        texts.append(value)
                elif object_type.casefold() in {"ellipse", "rect", "polygon", "arc"}:
                    value = {"kind": object_type, "bbox": _bbox(_safe_attr(figure, "listBbox")),
                             "layer": _text(_safe_attr(figure, "layerName")), "purpose": _text(_safe_attr(figure, "purpose")),
                             "net": _text(_safe_attr(_safe_attr(figure, "net"), "name")),
                             "properties": _native_properties(figure) if include_properties else None}
                    if object_type.casefold() == "polygon":
                        value["points"] = [_point(position) for position in (_safe_attr(figure, "points", []) or [])]
                    if object_type.casefold() == "arc":
                        value.update(start_angle=_safe_attr(figure, "startAngle"), stop_angle=_safe_attr(figure, "stopAngle"))
                        if value["start_angle"] is None or value["stop_angle"] is None:
                            unsupported.append({"kind": "arc_geometry", "reason": "arc angles unavailable"})
                    if value["bbox"] is None or value["layer"] is None or value["purpose"] is None:
                        unsupported.append({"kind": "shape_geometry", "object": str(shape_index), "reason": "incomplete shape attributes"})
                    figures.append(value)
                else:
                    unsupported.append({"kind": "shape", "object": str(shape_index), "reason": "unsupported native shape type", "native_type": object_type})
            nets_detail = []
            for net in design.nets:
                detail = {
                    "name": _text(_safe_attr(net, "name"), ""),
                    "num_bits": int(_safe_attr(net, "numBits", 1) or 1),
                    "terminals": [],
                    "properties": _native_properties(net) if include_properties else None,
                }
                for term in _safe_attr(net, "terms", []) or []:
                    detail["terminals"].append({"name": _text(_safe_attr(term, "name")), "direction": _text(_safe_attr(term, "direction"))})
                nets_detail.append(detail)
            inherited = []
            try:
                inherited_objects = _native_value(self.api.dbGetCellViewInheritedSig(design))
                inherited, inherited_issues = _native_inherited_expressions(self.api, design, texts, inherited_objects)
                unsupported.extend({"object": target, **issue} for issue in inherited_issues)
            except Exception as exc:
                unsupported.append({"kind": "inherited_connections", "object": target, "reason": type(exc).__name__})
            owner = self.api.dbGetPropByName(design, OWNER_PROPERTY)
            modified_method = _safe_attr(design, "isModified")
            mode_method = _safe_attr(design, "getMode")
            modified = modified_method() if callable(modified_method) else None
            if modified is None:
                unsupported.append({"kind": "source", "reason": "native modified state unavailable"})
            mode = mode_method() if callable(mode_method) else _safe_attr(design, "mode")
            counter = counter_api(design) if callable(counter_api) else None
            if initial_counter != counter:
                unsupported.append({"kind": "source", "reason": "native design counter changed during readback"})
            source = {
                "saved": not modified if modified is not None else None,
                "modified": modified,
                "mode": _text(mode),
                "file": _text(_safe_attr(design, "fileName")),
                "design_counter": counter,
            }
            file_name = _text(_safe_attr(design, "fileName"))
            database_path = Path(file_name) if file_name else None
            final_hash = file_digest(database_path) if database_path and database_path.is_file() else None
            if final_hash is None and not (modified is True and type(counter) is int):
                unsupported.append({"kind": "source", "reason": "native database file unavailable"})
            if final_hash != source_hash:
                unsupported.append({"kind": "source", "reason": "native database changed during readback"})
            if parameters is not None:
                unsupported.append({"kind": "parameters", "reason": "legacy readback covers only plan-requested parameter names"})
            for item in instances + pins + nets_detail:
                from .signals import bit_connections, expand_signal

                try:
                    item["bits"] = expand_signal(item["name"])
                    if len(item["bits"]) != item.get("num_bits", 1):
                        raise ValueError("native bit count differs from the signal name")
                    if "connections" in item:
                        item["bit_connections"] = bit_connections(item["connections"], instance_name=item["name"])
                    elif "net" in item:
                        item["bit_connections"] = bit_connections({item["name"]: item["net"]})
                except ValueError as exc:
                    unsupported.append({"kind": "bit_connectivity", "object": item["name"], "reason": str(exc)})
            if not include_geometry:
                unsupported.append({"kind": "geometry", "reason": "excluded by request"})
                for item in instances:
                    for key in ("origin", "orientation", "placement_status"):
                        item.pop(key, None)
                for item in pins:
                    for key in ("position", "orientation"):
                        item.pop(key, None)
            if not include_properties:
                unsupported.append({"kind": "properties", "reason": "excluded by request"})
            result = {
                "schema_version": 2,
                "target": {"library": str(design.libName), "cell": str(design.cellName), "view": str(design.viewName)},
                "technology": str(design.tech.libName) if design.tech is not None else None,
                "owner": str(owner.value) if owner is not None else None,
                "database_sha256": final_hash,
                "port_order": [str(term.name) for term in design.terminals],
                "instances": sorted(instances, key=lambda item: item["name"]),
                "pins": sorted(pins, key=lambda item: item["name"]),
                "nets": sorted(str(net.name) for net in design.nets),
                "wires": [{"net": net, "points": [list(start), list(end)]} for net, start, end in sorted(segments)],
                "nets_detail": sorted(nets_detail, key=lambda item: item["name"]),
                "labels": labels,
                "texts": texts,
                "figures": figures,
                "inherited_connections": inherited,
                "source": source,
                "properties": _native_value(_safe_attr(design, "prop")) if include_properties else None,
                "completeness": {"status": "complete" if not unsupported else "incomplete", "issues": unsupported},
            }
            unsupported.extend(_opaque_fields({key: value for key, value in result.items() if key != "completeness"}))
            result["completeness"]["status"] = "incomplete" if unsupported else "complete"
            return stable_readback(result)
        finally:
            if existing is None:
                self.close(design)

    def read_hierarchy(self, target, *, max_depth=20, include_geometry=True, include_properties=True, configuration=None, pdk_root=None):
        from .hierarchy import read_hierarchy
        from .pdk import DEFAULT_PDK_ROOT, catalog

        devices = None

        def native_primitive(master, instance):
            """Return verified evidence for a product-provided leaf master.

            The catalogue is deliberately small and scoped to the current
            certified testbench.  Identity and terminal names come from the
            live native symbol; a table entry alone never certifies a leaf.
            """
            spec = _NATIVE_PRIMITIVE_CATALOG.get((master.get("library"), master.get("cell")))
            if spec is None:
                return None
            base = {"kind": "native_primitive", "library": master.get("library"), "cell": master.get("cell"),
                    "primitive_type": spec["primitive_type"], "metadata_source": "native_symbol_catalog",
                    "master_exists": False, "terminal_set_verified": False,
                    "terminal_order": list(spec["terminal_order"]),
                    "device_def_sha256": digest({"master": master, "spec": spec})}
            try:
                if not self.library.exists(master):
                    base["reason"] = "native primitive master is unavailable"
                    return base
                existing = self.api.dbFindCV(master["library"], master["cell"], master["view"])
                design = existing if existing is not None else self.open(master)
                opened = existing is None
                try:
                    actual_order = [str(term.name) for term in design.terminals]
                    base["terminal_order"] = actual_order
                    base["master_exists"] = True
                    if set(actual_order) != set(spec["terminal_order"]):
                        base["reason"] = "native primitive terminal metadata differs from certified catalogue"
                        return base
                    if set(instance.get("connections", {})) != set(spec["terminal_order"]):
                        base["reason"] = "instance terminal set differs from certified primitive metadata"
                        return base
                    base["terminal_set_verified"] = True
                    base["device_def_sha256"] = digest({"master": master, "terminal_order": actual_order,
                                                          "primitive_type": spec["primitive_type"],
                                                          "source_file": _text(_safe_attr(design, "fileName"))})
                    return base
                finally:
                    if opened:
                        self.close(design)
            except Exception as exc:
                base["reason"] = "native primitive metadata read failed"
                base["error"] = {"exception": type(exc).__name__}
                return base

        def primitive(instance):
            nonlocal devices
            master = instance["master"]
            if master["library"] == "018um_PDK":
                if not self.library.exists(master):
                    return {"kind": "pdk_device", "library": "018um_PDK", "cell": master["cell"],
                            "device_def_sha256": "0" * 64, "terminal_order": [], "master_exists": False,
                            "terminal_set_verified": False, "reason": "PDK primitive master is unavailable"}
                if devices is None:
                    devices = {device["cell"]: device for device in catalog(pdk_root or DEFAULT_PDK_ROOT)["devices"]}
                device = devices.get(master["cell"])
                if device is None:
                    return None
                proof = {"kind": "pdk_device", "library": "018um_PDK", "cell": master["cell"],
                         "device_def_sha256": device["device_def_sha256"], "terminal_order": device["terminal_order"],
                         "master_exists": True, "terminal_set_verified": set(device["terminal_order"]) == set(instance["connections"]),
                         "metadata_source": "pdk_catalog"}
                if not proof["terminal_set_verified"]:
                    proof["reason"] = "instance terminal set differs from PDK device definition"
                return proof
            return native_primitive(master, instance)

        return read_hierarchy(lambda current: self.read(current, None, include_geometry=include_geometry, include_properties=include_properties),
                              self.library.exists, target, max_depth=max_depth, configuration=configuration, primitive=primitive)

    def copy(self, source, target, *, overwrite=False):
        design = self.open(source)
        try:
            require(self.api.dbCopyCV(design, target["library"], target["cell"], target["view"], overwrite), "dbCopyCV", "publish")
        finally:
            self.close(design)


def _layout_attr(value, *names, default=None):
    for name in names:
        item = _safe_attr(value, name)
        if item is not None:
            try:
                return item() if callable(item) else item
            except Exception:
                continue
    return default


def _layout_list(value, *names):
    result = _layout_attr(value, *names, default=None)
    if result is None:
        return None
    try:
        return list(result)
    except (TypeError, ValueError):
        return None


def _layout_point(value):
    try:
        return [round(float(value[0]), 9), round(float(value[1]), 9)]
    except Exception:
        return None


def _layout_bbox(value):
    try:
        if len(value) == 4:
            return [round(float(number), 9) for number in value]
        first, second = value
        first_point = _layout_point(first)
        second_point = _layout_point(second)
        if first_point is None or second_point is None:
            return None
        return [first_point[0], first_point[1], second_point[0], second_point[1]]
    except Exception:
        return None


def _layout_lpp(item):
    lpp = _layout_attr(item, "lpp", default=None)
    if isinstance(lpp, (list, tuple)) and len(lpp) >= 2:
        return str(lpp[0]), str(lpp[1])
    return (_text(_layout_attr(item, "layerName", "layer", default=None)),
            _text(_layout_attr(item, "purpose", default=None)))


def _layout_native_object(api, item):
    obj_type = _text(_layout_attr(item, "objType", "objectType", "type", default=None), "unknown").lower()
    obj_type = {"text": "label", "rectangle": "rect", "stdvia": "via", "customvia": "via", "arrayinst": "mosaic", "scalarinst": "instance"}.get(obj_type, obj_type)
    layer, purpose = _layout_lpp(item)
    bbox = _layout_bbox(_layout_attr(item, "listBbox", "bBox", "bbox", default=None))
    points_raw = _layout_attr(item, "points", "pointList", default=None)
    points = [_layout_point(point) for point in points_raw] if points_raw is not None else None
    origin = _layout_point(_layout_attr(item, "origin", "xy", "point", default=None))
    name = _layout_property(api, item, "aetherpilot_name") or _text(_layout_attr(item, "name", "objectName", default=None))
    result = {"name": name, "obj_type": obj_type, "layer": layer, "purpose": purpose,
              "bbox": bbox, "points": points, "origin": origin,
              "orientation": _text(_layout_attr(item, "orient", "orientation", default=None)),
              "net": _text(_layout_attr(_layout_attr(item, "net"), "name")),
              "pin": _text(_layout_attr(_layout_attr(item, "pin"), "name")),
              "properties": _layout_properties(item)}
    if obj_type in {"label", "via"}:
        result["position"] = result.pop("origin")
    if obj_type == "path":
        result.update(width=_layout_attr(item, "width"), style=_text(_layout_attr(item, "pathStyle", "style")),
                      begin_extension=_layout_attr(item, "beginExt"), end_extension=_layout_attr(item, "endExt"))
    if obj_type == "label":
        result.update(text=_text(_layout_attr(item, "textName", "theLabel", "text")), height=_layout_attr(item, "height"),
                      justification=_text(_layout_attr(item, "justify")), font=_text(_layout_attr(item, "font")))
    if result["name"] is None:
        result.pop("name")
    return result


def _layout_property(api, item, name):
    try:
        return _text(_safe_attr(api.dbGetPropByName(item, name), "value"))
    except Exception:
        return None


def _layout_properties(item):
    properties = _layout_list(item, "prop", "getProps")
    if properties is None:
        return None
    return {str(prop.name): _native_value(prop.value) for prop in properties if not str(prop.name).startswith("aetherpilot_")}


def _layout_parameters(api, item, issues):
    definitions = _layout_list(item, "cdfParameter")
    if definitions is not None:
        return {str(definition.name): _native_value(definition.value) for definition in definitions}
    try:
        parameters = api.emyParamArray()
        item.getParams(parameters)
        values = {}
        for index in range(parameters.getNumElements()):
            parameter = parameters.get(index)
            name = api.emyString()
            parameter.getName(name)
            value = parameters.getParam(str(name))
            values[str(name)] = _native_value(_layout_attr(value, "value", default=value))
        return values
    except Exception as exc:
        issues.append({"kind": "parameters", "object": _text(_safe_attr(item, "name")), "reason": type(exc).__name__})
        return {}


def _layout_tag(api, item, name):
    require(api.dbCrtProp(item, "aetherpilot_name", name, "string"), "dbCrtProp", "layout")


def _layout_attach_net(api, figure, net):
    if figure is None or net is None:
        return
    require(api.dbAddFigToNet(figure, net), "dbAddFigToNet", "layout")


class LayoutPrimitives:
    """PyAether layout operations with independent database readback gates."""

    def __init__(self, api):
        self.api = api
        self.library = LibraryOps(api)

    @staticmethod
    def capabilities():
        from .layout_surface import layout_surface_report

        report = layout_surface_report()
        return {"available": True, "status": "native_reference_surface_verified", "operations": report["reference"],
                "native_verified": report["native_certified"], "certification_scope": report["certification_scope"],
                "native_evidence": report["native_evidence"]}

    def open(self, target, mode="r"):
        return require(self.api.dbOpenCV(target["library"], target["cell"], target.get("view", "layout"), mode), "dbOpenCV", "layout")

    def close(self, design):
        require(self.api.dbCloseCV(design), "dbCloseCV", "layout")

    def _create(self, method_name, *args, **kwargs):
        method = _safe_attr(self.api, method_name)
        if not callable(method):
            raise OperationError("Required layout native API is unavailable", stage="layout", code="api_missing", details={"api": method_name})
        return require(method(*args, **kwargs), method_name, "layout")

    def build(self, plan, target, owner, report):
        design = require(self.api.dbNewCV(target["library"], target["cell"], "layout"), "dbNewCV", "layout")
        try:
            require(self.api.dbCrtProp(design, OWNER_PROPERTY, owner, "string"), "dbCrtProp", "layout")
            self.populate(design, plan)
            self.save(design)
        finally:
            self.close(design)

    def save(self, design):
        require(design.isConsistent(), "isConsistent", "check_save")
        require(self.api.dbSaveCV(design), "dbSaveCV", "check_save")

    def populate(self, design, plan, *, reuse_nets=False):
        nets = {str(net.name): net for net in design.nets} if reuse_nets else {}
        for net_name in plan["nets"]:
            if net_name not in nets:
                nets[net_name] = self._create("dbCrtNet", design, net_name)
        for item in plan["instances"]:
            params = self.parameter_vector(item["parameters"])
            instance = self._create("dbCrtInst", design, tuple(item["origin"]), lib=item["master"]["library"], cell=item["master"]["cell"], view=item["master"].get("view", "layout"), inst=item["name"], orient=item["orientation"], params=params)
            _layout_tag(self.api, instance, item["name"])
        for item in plan["mosaics"]:
            master = item["master"]
            mosaic = self._create("dbCrtInst", design, tuple(item["origin"]), lib=master["library"], cell=master["cell"], view=master["view"], inst=item["name"], orient=item["orientation"], rows=item["rows"], cols=item["cols"], rowPitch=item["row_pitch"], colPitch=item["col_pitch"], params=self.parameter_vector(item["parameters"]))
            _layout_tag(self.api, mosaic, item["name"])
        for item in plan["paths"]:
            figure = self._create("dbCrtPath", design, layer=item["layer"], purpose=item["purpose"], points=item["points"], width=item["width"], style=item["style"], beginExt=item["begin_extension"], endExt=item["end_extension"])
            _layout_tag(self.api, figure, item["name"])
            _layout_attach_net(self.api, figure, nets.get(item.get("net")))
        for item in plan["rects"]:
            figure = self._create("dbCrtRect", cv=design, bbox=tuple(item["bbox"]), layer=item["layer"], purpose=item["purpose"])
            _layout_tag(self.api, figure, item["name"])
            _layout_attach_net(self.api, figure, nets.get(item.get("net")))
        for item in plan["polygons"]:
            figure = self._create("dbCrtPolygon", cv=design, points=item["points"], layer=item["layer"], purpose=item["purpose"])
            _layout_tag(self.api, figure, item["name"])
            _layout_attach_net(self.api, figure, nets.get(item.get("net")))
        for item in plan["labels"]:
            label = self._create("dbCrtLabel", cv=design, point=tuple(item["position"]), textName=item["text"], height=item["height"], layer=item["layer"], purpose=item["purpose"], orient=item["orientation"], justify=item["justification"], font=item["font"])
            _layout_tag(self.api, label, item["name"])
        for item in plan["vias"]:
            native_keys = {"rows", "cols", "width", "height", "xSpace", "ySpace", "layer1", "layer2", "impL1", "impL2"}
            kwargs = {key: value for key, value in item["parameters"].items() if key in native_keys}
            kwargs["params"] = self.parameter_vector({key: value for key, value in item["parameters"].items() if key not in native_keys})
            via = self._create("dbCrtVia", cv=design, defName=item["definition"], point=tuple(item["position"]), orient=item["orientation"], **kwargs)
            _layout_tag(self.api, via, item["name"])
            _layout_attach_net(self.api, via, nets.get(item.get("net")))
        for item in plan["pins"]:
            net = nets[item["net"]]
            figure = self._create("dbCrtRect", cv=design, bbox=tuple(item["bbox"]), layer=item["layer"], purpose=item["purpose"])
            _layout_tag(self.api, figure, item["name"] + "__pin_figure")
            pin = self._create("dbCrtPin", net=item["net"], netId=net, fig=figure, name=item["name"], dir=item["direction"])
            _layout_tag(self.api, pin, item["name"])

    @staticmethod
    def parameter_vector(parameters):
        return [entry for name, value in sorted(parameters.items()) for entry in (name, str(int(value)) if isinstance(value, bool) else str(value))]


    def copy(self, source, target, *, overwrite=False):
        design = self.open(source)
        try:
            require(self.api.dbCopyCV(design, target["library"], target["cell"], "layout", overwrite), "dbCopyCV", "layout")
        finally:
            self.close(design)



class PyAetherAdapter:
    def __init__(self, api, artifacts, result, *, owned=False):
        from .layout_native import LayoutOps
        from .library_native import CategoryOps, LibraryMetadataOps
        from .maestro_native import MaestroNativeOps
        from .symbol_native import SymbolOps

        self.api = api
        self.artifacts = artifacts
        self.result = result
        self.owned = owned
        self.library = LibraryOps(api)
        self.library_metadata = LibraryMetadataOps(api)
        self.categories = CategoryOps(api)
        self.schematic = SchematicOps(api)
        self.layout = LayoutOps(api)
        self.symbol = SymbolOps(api, workspace=Path.cwd())
        self.maestro = MaestroNativeOps(api, workspace=Path.cwd())

    def stage(self, name, **details):
        self.result["stage"] = name
        self.artifacts.event(name, "started", **details)
        self.artifacts.write("checkpoint.json", self.result)

    def preflight(self, plan=None):
        self.stage("preflight")
        libraries = self.library.list()
        if not libraries:
            raise PreflightError("No libraries are available", stage="preflight", code="libraries_missing")
        for name in ("dbCheckAndSaveDesign", "dbCopyCV", "dbCrtInstTermByName", "dbHasMarker", "dbCVIsLocked"):
            if not hasattr(self.api, name):
                raise PreflightError("Required native API is unavailable", stage="preflight", code="api_missing", details={"api": name})
        if plan and plan["target"]["technology"] not in libraries:
            raise PreflightError("Technology library is unavailable", stage="preflight", code="pdk_missing")
        report = {"native_python": sys.version.split()[0], "libraries": libraries, "owner": "agent" if self.owned else "user",
                  "license_initialized": True, "simulator_configured": bool(os.environ.get("ALPS_ROOT")), "api_checked": True}
        self.artifacts.write("preflight.json", report)
        return report

    def apply(self, plan, *, expected_content_sha256=None, expected_content_version=2):
        from .transactions import MANAGED_REGISTRY_SCHEMA, managed_registry_matches, registry_fingerprint, writable_target

        writable_target(plan["target"])
        self.preflight(plan)
        target = plan["target"]
        if not self.owned and self.api.dbFindCV(target["library"], target["cell"], target["view"]) is not None:
            raise OperationError("Target is already open in the user session", stage="session", code="user_view_open")
        contacts = self.schematic.master_contacts(plan)
        segments = compile_wires(plan, contacts)
        self.artifacts.write("compiled-plan.json", {"segments": segments, "contacts": contacts, "design_sha256": design_digest(plan)})
        parameters = {item["name"]: item["parameters"] for item in plan["instances"]}
        registry = Path.cwd() / ".aetherpilot" / "managed" / (digest({key: target[key] for key in ("library", "cell", "view")}) + ".json")
        from .schematic_recovery import pending_schematic_recovery

        pending = pending_schematic_recovery(Path.cwd() / ".aetherpilot", target)
        if pending is not None:
            raise OperationError("Schematic has an unresolved publication; recover it before editing", stage="ownership",
                                 code="recovery_required", details={"publication_operation_id": pending})
        exists = self.library.exists(target)
        previous = None
        if exists:
            self.stage("ownership")
            self.library.require_unlocked(target)
            if registry.is_symlink() or (registry.exists() and not registry.is_file()):
                raise OperationError("Ownership registry is not a regular local file", stage="ownership", code="ownership_record_incomplete")
            try:
                previous = json.loads(registry.read_text())
            except (OSError, ValueError) as exc:
                raise OperationError("Existing target is not managed; refusing to overwrite", stage="ownership", code="target_exists") from exc
            if previous.get("schema_version") != MANAGED_REGISTRY_SCHEMA or previous.get("domain") != "schematic" or not isinstance(previous.get("fingerprint"), dict):
                raise OperationError("Ownership registry schema is unsupported; use a new target or an explicit migration", stage="ownership", code="ownership_record_incomplete", details={"schema_version": previous.get("schema_version"), "expected": MANAGED_REGISTRY_SCHEMA})
            current = self.schematic.read(target)
            current_fingerprint = registry_fingerprint(current, schematic_content)
            if not managed_registry_matches(previous, current, current_fingerprint, domain="schematic", target=target):
                raise OperationError("Managed target has external changes; refusing to overwrite", stage="ownership", code="external_changes")
            if previous.get("design_sha256") and design_digest(plan) == previous["design_sha256"]:
                ValidationOps.compare(plan, current, segments)
                self.result["readback"] = current
                self.result["warnings"].append("Identical managed design verified; no database writes were needed.")
                self.artifacts.write("readback.json", current)
                self.artifacts.write("validation.json", {"ok": True, "idempotent": True})
                return current
            if plan["mode"] == "create":
                raise OperationError("create cannot change an existing design; use modify", stage="ownership", code="target_exists")
        elif plan["mode"] == "modify":
            raise OperationError("modify requires an existing managed design", stage="ownership", code="target_missing")
        self.stage("library")
        if self.library.ensure(target):
            self.result["changes"].append({"type": "library_created", "library": target["library"]})
        stage_target = {**target, "cell": "APstage_" + self.result["operation_id"]}
        # Each write needs a distinct marker so recovery can identify its own
        # native copy even if dbCopyCV raises before the post-copy checkpoint.
        owner = self.result["operation_id"]
        self.stage("schematic", staging=stage_target["cell"])
        self.schematic.build(plan, stage_target, segments, owner, self.artifacts.path / "check-report.txt")
        self.result["changes"].append({"type": "staging_created", "cell": stage_target["cell"]})
        self.stage("readback")
        candidate = self.schematic.read(stage_target)
        self.artifacts.write("staging-readback.json", candidate)
        validation = ValidationOps.compare(plan, candidate, segments)
        if expected_content_sha256 is not None:
            verified_candidate = candidate if expected_content_version == 2 else {key: value for key, value in candidate.items() if key != "port_order"}
            if digest(schematic_content(verified_candidate)) != expected_content_sha256:
                raise ValidationError("Imported native candidate differs from netlist metadata", stage="readback", code="netlist_content_mismatch")
        backup = None
        recovery = None
        if exists:
            self.stage("backup")
            self.library.require_unlocked(target)
            backup = {**target, "cell": "APbackup_" + self.result["operation_id"]}
            self.schematic.copy(target, backup)
            backup_readback = self.schematic.read(backup)
            backup_fingerprint = registry_fingerprint(backup_readback, schematic_content)
            if (backup_fingerprint["content_sha256"] != previous["fingerprint"]["content_sha256"] or
                    backup_readback.get("owner") != current.get("owner")):
                raise ValidationError("Backup differs from the managed design", stage="backup", code="backup_mismatch")
            self.artifacts.write("backup-readback.json", backup_readback)
            self.result["changes"].append({"type": "backup_created", "cell": backup["cell"]})
            from .schematic_native import SchematicTransactionsBackend
            from .schematic_recovery import SchematicRecovery

            recovery = SchematicRecovery(SchematicTransactionsBackend(self.api), Path.cwd(), self.artifacts, self.result)
        self.stage("publish", target=target["cell"], backup=backup["cell"] if backup else None)
        if exists:
            self.library.require_unlocked(target)
            current = self.schematic.read(target)
            if not managed_registry_matches(previous, current, registry_fingerprint(current, schematic_content), domain="schematic", target=target):
                raise OperationError("Target changed before publication", stage="publish", code="external_changes")
            recovery.begin(self.result["operation_id"], target=target, staging=stage_target, backup=backup,
                           baseline=current, candidate=candidate)
        try:
            self.schematic.copy(stage_target, target, overwrite=exists)
            self.stage("readback")
            actual = self.schematic.read(target)
            if recovery is not None:
                recovery.checkpoint(self.result["operation_id"], actual)
            self.result["readback"] = actual
            self.artifacts.write("readback.json", actual)
            ValidationOps.compare(plan, actual, segments)
            if content(actual) != content(candidate):
                raise ValidationError("Published design differs from staging", stage="readback", code="publish_mismatch")
            fingerprint = registry_fingerprint(actual, schematic_content)
            atomic_json(registry, {"schema_version": MANAGED_REGISTRY_SCHEMA, "domain": "schematic",
                                   "target": {key: target[key] for key in ("library", "cell", "view")}, "owner": owner,
                                   "operation_id": self.result["operation_id"], "plan": plan,
                                   "design_sha256": design_digest(plan), "fingerprint": fingerprint,
                                   # Flat aliases are retained for diagnostics and
                                   # explicit old-record detection; ownership
                                   # decisions always use the nested v2 value.
                                   "readback_sha256": fingerprint["content_sha256"],
                                   "database_sha256": fingerprint["database_sha256"], "backup": backup})
            self.result["changes"].append({"type": "design_modified" if exists else "design_created", "target": target})
            if recovery is not None:
                recovery.published(self.result["operation_id"])
        except Exception as original:
            if recovery is not None:
                try:
                    outcome = recovery.recover(self.result["operation_id"], approved=True, automatic=True)
                except Exception as recovery_error:
                    raise OperationError("Schematic publication requires explicit recovery", stage="recovery", code="recovery_required",
                                         details={"publication_operation_id": self.result["operation_id"], "original": type(original).__name__,
                                                  "recovery": type(recovery_error).__name__,
                                                  "recovery_code": getattr(recovery_error, "code", None)}) from original
                if outcome.get("publication_committed"):
                    raise OperationError("Schematic was published, but publication evidence could not be finalized", stage="artifacts",
                                         code="publication_committed_evidence_failure",
                                         details={"publication_operation_id": self.result["operation_id"], "original": type(original).__name__}) from original
            raise
        try:
            self.artifacts.write("validation.json", validation)
        except Exception as exc:
            if recovery is not None:
                raise OperationError("Schematic was published, but validation evidence could not be finalized", stage="artifacts",
                                     code="publication_committed_evidence_failure",
                                     details={"publication_operation_id": self.result["operation_id"], "original": type(exc).__name__}) from exc
            raise
        return actual

    def screenshot(self, target):
        if not self.owned:
            self.result["warnings"].append("Screenshot skipped: user-owned GUI state is not changed.")
            return
        self.stage("screenshot")
        opened = False
        try:
            from PyQt5.QtWidgets import QApplication

            os.environ["DISP_REG_IMAGE_NEW"] = "1"
            opened = bool(self.api.aeOpenDesign(target["library"], target["cell"], target["view"], "readOnly"))
            if not opened:
                raise RuntimeError("Cannot open screenshot view")
            design = self.api.aeGetCurDesign()
            self.api.aeZoomFit(1, 0.9)
            for iteration in range(5):
                QApplication.processEvents()
            directory = self.artifacts.path / "screenshots"
            directory.mkdir(mode=0o700)
            first, second = design.listBbox
            box = (first[0] - 0.5, first[1] - 0.5, second[0] + 0.5, second[1] + 0.5)
            self.api.aeSaveCart(design, bbox=box, width=1200, height=1000, path=str(directory) + "/")
            if not any(path.stat().st_size > 100 for path in directory.rglob("*.png")):
                self.result["warnings"].append("Native screenshot unavailable; database evidence remains authoritative.")
        except Exception:
            self.result["warnings"].append("Native screenshot failed; see private native logs.")
        finally:
            if opened:
                self.api.aeCloseDesign()
