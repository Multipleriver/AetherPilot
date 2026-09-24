"""Vendor-neutral declarative contracts.

This module deliberately contains no database, simulator, or PDK imports.
Technology and simulation backend policy is supplied by the caller when a
native adapter needs it; the default validator only checks the data shape and
deterministic safety limits.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from ..errors import ValidationError


NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")
NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:meg|[TGMKkmunpf])?$")
ORIENTATIONS = {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reject(message, **details):
    raise ValidationError(message, stage="plan", code="invalid_plan", details=details)


def fields(value, required, optional=(), *, where="plan"):
    if not isinstance(value, dict):
        reject(f"{where} must be an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        reject(f"Invalid fields in {where}", missing=sorted(missing), unknown=sorted(extra))


def identifier(value, where="name"):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        reject(f"{where} must be an ASCII identifier (1–80 characters)")
    if value.startswith("APstage_") or value.startswith("APbackup_"):
        reject(f"{where} uses a reserved prefix")
    return value


def signal_identifier(value, where="signal"):
    # Keep signal parsing optional at import time.  ``signals`` is another
    # public, backend-independent module, but lazy loading also lets callers
    # use scalar contract helpers in very small installations.
    from ..signals import native_signal_name

    try:
        if not isinstance(value, str) or len(value) > 4096 or value.startswith(("APstage_", "APbackup_")):
            raise ValueError("invalid signal name")
        return native_signal_name(value)
    except ValueError as exc:
        reject(f"Invalid {where}", reason=str(exc))


def property_records(value, where="properties", *, max_items=64):
    """Validate deterministic scalar OpenAccess user properties."""
    if not isinstance(value, list) or len(value) > max_items:
        reject(f"{where} must be an array of at most {max_items} entries")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "value", "type", "isNetSet"} or "name" not in item or "value" not in item:
            reject(f"Invalid {where} entry")
        name = identifier(item["name"], f"{where}.name")
        if name in seen:
            reject(f"Duplicate property in {where}")
        seen.add(name)
        value_item = item["value"]
        if not isinstance(value_item, (str, int, float, bool)) or (isinstance(value_item, float) and not math.isfinite(value_item)):
            reject(f"{where}.value must be a scalar")
        if isinstance(value_item, str) and len(value_item) > 4096:
            reject(f"{where}.value is too long")
        record = {"name": name, "value": value_item}
        if "type" in item:
            if not isinstance(item["type"], str) or item["type"].casefold() not in {"bool", "int", "float", "double", "string", "time"}:
                reject(f"{where}.type is invalid")
            record["type"] = item["type"].casefold()
        if "isNetSet" in item:
            if type(item["isNetSet"]) is not bool:
                reject(f"{where}.isNetSet must be boolean")
            if item["isNetSet"]:
                reject(f"{where}.isNetSet is unsupported for schematic annotations")
            record["isNetSet"] = item["isNetSet"]
        result.append(record)
    return result


def parameter_literal(value, where):
    if isinstance(value, bool):
        return "1" if value else "0"
    try:
        result = numeric_text(value, where)
        engineering(result)
        return result
    except ValidationError:
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ .-]{0,255}", value):
            return value
        reject(f"{where} must be a finite numeric literal, boolean, or plain CDF choice")


def finite(value, where, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        reject(f"{where} must be a finite number")
    if positive and value <= 0:
        reject(f"{where} must be positive")
    return value


def point(value, grid, where):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        reject(f"{where} must contain two coordinates")
    for coordinate in value:
        finite(coordinate, where)
        if abs(coordinate) > 1_000_000 or not math.isclose(coordinate / grid, round(coordinate / grid), rel_tol=0, abs_tol=1e-7):
            reject(f"{where} is outside bounds or off grid")
    return [float(coordinate) for coordinate in value]


def numeric_text(value, where):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        reject(f"{where} must be a numeric value or engineering literal")
    text = str(value)
    if not NUMBER.fullmatch(text):
        reject(f"{where} cannot contain an expression or directive")
    return text


def engineering(value):
    text = numeric_text(value, "parameter")
    match = re.search(r"(meg|[TGMKkmunpf])$", text)
    factor = 1.0
    if match:
        factor = {"T": 1e12, "G": 1e9, "M": 1e-3, "meg": 1e6, "K": 1e3, "k": 1e3,
                  "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}[match.group()]
        text = text[:match.start()]
    result = float(text) * factor
    if not math.isfinite(result):
        reject("Parameter is not finite")
    return result


def unique(items, where, name_validator=identifier):
    if not isinstance(items, list) or len(items) > 10000:
        reject(f"{where} must be an array of at most 10000 entries")
    names = [name_validator(item.get("name"), where) if isinstance(item, dict) else reject(f"Invalid {where}") for item in items]
    if len(set(names)) != len(names):
        reject(f"Duplicate names in {where}")


def _matches_gate(value, expected):
    """Return whether *value* satisfies an optional policy selector.

    A selector may be a literal, a collection of literals, or a callable.  A
    callable receives the actual value and may return a truthy decision.  The
    small helper keeps policy out of the generic schema validator while still
    allowing existing adapters to pass a strict gate without reimplementing
    plan validation.
    """
    if expected is None:
        return True
    if callable(expected):
        try:
            return bool(expected(value))
        except Exception:
            return False
    if isinstance(expected, (set, frozenset, list, tuple)):
        return value in expected
    return value == expected


def _gate_value(selector, *, default=None):
    """Normalize the backwards-compatible ``enforce_*`` selector."""
    if selector is None or selector is False:
        return None
    if selector is True:
        return default
    return selector


def validate_plan(source, *, technology=None, enforce_technology=None, simulation_backend=None,
                  enforce_backend=None):
    """Validate and normalize a schematic plan.

    ``technology`` and ``simulation_backend`` are optional policy selectors.
    They can be literals, collections, or callables.  ``enforce_technology``
    and ``enforce_backend`` are compatibility switches: ``True`` selects the
    historical certified values (``018um_PDK`` and ``alps`` respectively), a
    literal/callable supplies another selector, and ``False``/``None`` leaves
    the generic validator unbound.  This keeps the public core useful for
    arbitrary adapters while preserving a one-line strict gate for the native
    path.
    """
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(Path(source).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError) as exc:
            reject("Cannot read plan JSON", reason=type(exc).__name__)
    plan = copy.deepcopy(source)
    fields(plan, ("schema_version", "operation", "target", "instances", "nets", "pins", "wires"), ("mode", "grid", "simulation", "labels", "inherited_connections", "port_order", "routing_policy", "source_contract"))
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1 or plan["operation"] != "schematic.apply":
        reject("Only schema_version=1 and schematic.apply are supported")
    plan.setdefault("mode", "create")
    if plan["mode"] not in ("create", "modify"):
        reject("mode must be create or modify")
    plan.setdefault("grid", 0.0625)
    grid = finite(plan["grid"], "grid", positive=True)
    if grid < 1e-6:
        reject("grid must be at least 1e-6")
    target = plan["target"]
    fields(target, ("library", "cell", "technology"), ("view",), where="target")
    target.setdefault("view", "schematic")
    for key, value in target.items():
        identifier(value, f"target.{key}")
    if target["view"] != "schematic":
        reject("v1 only writes schematic views")
    technology_gate = technology
    if technology_gate is None:
        technology_gate = _gate_value(enforce_technology, default="018um_PDK")
    if not _matches_gate(target["technology"], technology_gate):
        reject("Plan technology is not accepted by the configured backend", technology=target["technology"])
    nets = plan["nets"]
    if not isinstance(nets, list) or not nets or len(nets) > 10000:
        reject("nets must be a nonempty array of at most 10000 names")
    nets = [signal_identifier(name, "net") for name in nets]
    if len(set(nets)) != len(nets):
        reject("Duplicate nets")
    plan["nets"] = sorted(nets)
    for collection in ("instances", "pins"):
        unique(plan[collection], collection, signal_identifier)
        for item in plan[collection]:
            item["name"] = signal_identifier(item["name"], collection)
    if not plan["instances"]:
        reject("At least one instance is required")
    for instance in plan["instances"]:
        fields(instance, ("name", "master", "origin", "parameters", "connections"), ("orientation",), where="instance")
        fields(instance["master"], ("library", "cell"), ("view",), where="master")
        instance["master"].setdefault("view", "symbol")
        for value in instance["master"].values():
            identifier(value, "master")
        instance.setdefault("orientation", "R0")
        if instance["orientation"] not in ORIENTATIONS:
            reject("Unsupported instance orientation")
        instance["origin"] = point(instance["origin"], grid, "instance.origin")
        for key in ("parameters", "connections"):
            if not isinstance(instance[key], dict) or key == "connections" and not instance[key]:
                reject(f"instance.{key} must be an object; connections cannot be empty")
            for name in instance[key]:
                (signal_identifier if key == "connections" else identifier)(name, key)
        instance["parameters"] = {name: parameter_literal(value, name) for name, value in instance["parameters"].items()}
        connections = {signal_identifier(name): signal_identifier(value) for name, value in instance["connections"].items()}
        if len(connections) != len(instance["connections"]):
            reject("Duplicate terminal names after signal normalization")
        instance["connections"] = connections
        if any(net not in nets for net in instance["connections"].values()):
            reject("Instance refers to an undeclared net")
    for pin in plan["pins"]:
        fields(pin, ("name", "net", "direction", "position"), ("orientation",), where="pin")
        pin["net"] = signal_identifier(pin["net"])
        if pin["net"] not in nets or pin["name"] != pin["net"]:
            reject("v1 requires pin name to equal its declared net name")
        if pin["direction"] not in ("input", "output", "inputOutput"):
            reject("Unsupported pin direction")
        pin.setdefault("orientation", "R0")
        if pin["orientation"] not in ORIENTATIONS:
            reject("Unsupported pin orientation")
        pin["position"] = point(pin["position"], grid, "pin.position")
    if "port_order" in plan:
        if not isinstance(plan["port_order"], list):
            reject("port_order must be an array")
        plan["port_order"] = [signal_identifier(name) for name in plan["port_order"]]
        if len(set(plan["port_order"])) != len(plan["port_order"]) or set(plan["port_order"]) != {pin["name"] for pin in plan["pins"]}:
            reject("port_order must name every pin exactly once")
    if not isinstance(plan["wires"], list) or len(plan["wires"]) > 10000:
        reject("wires must be an array of at most 10000 paths")
    for wire in plan["wires"]:
        fields(wire, ("net", "points"), where="wire")
        wire["net"] = signal_identifier(wire["net"])
        if wire["net"] not in nets or not isinstance(wire["points"], list) or not 2 <= len(wire["points"]) <= 1000:
            reject("Wire must have a declared net and 2–1000 points")
        wire["points"] = [point(value, grid, "wire.points") for value in wire["points"]]
        for start, end in zip(wire["points"], wire["points"][1:]):
            if start == end or (start[0] != end[0] and start[1] != end[1]):
                reject("Wire segments must be nonzero and orthogonal")
    labels = plan.setdefault("labels", [])
    if not isinstance(labels, list) or len(labels) > 1000:
        reject("labels must be an array of at most 1000 entries")
    for label in labels:
        if not isinstance(label, dict):
            reject("Invalid schematic label")
        allowed = {"kind", "text", "origin", "orientation", "height", "layer", "purpose", "justification", "font", "properties", "net"}
        if set(label) - allowed:
            reject("Invalid fields in schematic label", unknown=sorted(set(label) - allowed))
        required = ("kind", "text", "origin", "orientation", "height", "layer", "purpose", "justification", "font")
        if any(key not in label for key in required):
            reject("Schematic label is missing required fields")
        if label["kind"] not in {"label", "note", "wire_name"}:
            reject("Unsupported schematic annotation kind")
        for key in ("text", "layer", "purpose", "justification", "font"):
            if not isinstance(label[key], str) or not label[key] or len(label[key]) > 4096:
                reject("Schematic label text attributes are required")
        label["origin"] = point(label["origin"], grid, "label.origin")
        if label["orientation"] not in ORIENTATIONS:
            reject("Unsupported schematic label orientation")
        finite(label["height"], "label.height", positive=True)
        label["properties"] = property_records(label.get("properties", []), "Schematic label properties")
        if label["kind"] != "label" and label["properties"]:
            reject("Only ordinary labels may carry user properties")
        if label["kind"] == "note":
            if (label["layer"], label["purpose"]) != ("text", "drawing"):
                reject("Note annotations require the text/drawing layer purpose")
        elif (label["layer"], label["purpose"]) == ("text", "drawing"):
            reject("Ordinary labels cannot use the text/drawing note layer; use kind=note")
        elif label["kind"] == "wire_name":
            if (label["layer"], label["purpose"]) != ("wire", "label") or not isinstance(label.get("net"), str):
                reject("Wire-name annotations require wire/label and a declared net")
            label["net"] = signal_identifier(label["net"], "wire-name net")
            if label["text"] != label["net"] or label["net"] not in nets:
                reject("Wire-name text must equal its declared net")
        elif (label["layer"], label["purpose"]) == ("wire", "label"):
            reject("wire/label annotations must declare kind=wire_name")
    plan["labels"] = labels
    inherited = plan.setdefault("inherited_connections", [])
    if not isinstance(inherited, list) or len(inherited) > 1000:
        reject("inherited_connections must be an array of at most 1000 entries")
    for definition in inherited:
        if not isinstance(definition, dict) or set(definition) - {"property", "default", "pin"}:
            reject("Invalid inherited connection definition")
        if set(definition) != {"property", "default", "pin"}:
            reject("Inherited connection requires property, default and pin")
        identifier(definition["property"], "inherited property")
        definition["default"] = signal_identifier(definition["default"], "inherited default net")
        definition["pin"] = signal_identifier(definition["pin"], "inherited attachment pin")
        if definition["default"] not in nets or definition["pin"] not in {item["name"] for item in plan["pins"]}:
            reject("Inherited connection refers to an unknown net or pin")
    if len({(item["property"], item["pin"]) for item in inherited}) != len(inherited):
        reject("Duplicate inherited connection definitions")
    plan["inherited_connections"] = inherited
    if "routing_policy" in plan:
        policy = plan["routing_policy"]
        if not isinstance(policy, dict) or set(policy) - {"allow_crossings", "review_required", "allow_check_warnings", "allow_label_islands"}:
            reject("routing_policy contains unsupported fields")
        if "allow_crossings" in policy and type(policy["allow_crossings"]) is not bool:
            reject("routing_policy.allow_crossings must be boolean")
        if "review_required" in policy and type(policy["review_required"]) is not bool:
            reject("routing_policy.review_required must be boolean")
        if "allow_check_warnings" in policy and type(policy["allow_check_warnings"]) is not bool:
            reject("routing_policy.allow_check_warnings must be boolean")
        if "allow_label_islands" in policy and type(policy["allow_label_islands"]) is not bool:
            reject("routing_policy.allow_label_islands must be boolean")
    if "source_contract" in plan:
        source_contract = plan["source_contract"]
        if not isinstance(source_contract, dict) or set(source_contract) - {"mode", "source_cell", "source_view", "source_il", "source_il_sha256", "compact_map"}:
            reject("source_contract contains unsupported fields")
        if source_contract.get("mode") not in {"source_preserving", "source_preserving_compact_candidate", "source_preserving_hierarchy"}:
            reject("source_contract.mode must select a supported source-preserving mode")
        for key in ("source_cell", "source_view", "source_il", "source_il_sha256"):
            if not isinstance(source_contract.get(key), str) or not source_contract[key]:
                reject(f"source_contract.{key} is required")
        if not re.fullmatch(r"[a-f0-9]{64}", source_contract["source_il_sha256"]):
            reject("source_contract.source_il_sha256 must be a SHA-256")
        if "compact_map" in source_contract and not isinstance(source_contract["compact_map"], dict):
            reject("source_contract.compact_map must be an object")
    if "simulation" in plan:
        validate_simulation(plan, backend=simulation_backend, enforce_backend=enforce_backend)
    plan["nets"] = sorted(nets)
    plan["instances"].sort(key=lambda item: item["name"])
    plan["pins"].sort(key=lambda item: item["name"])
    return plan


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_simulation(plan, *, backend=None, enforce_backend=None):
    setup = plan["simulation"]
    fields(setup, ("backend", "analysis", "ground", "sources", "device_models", "signals"), ("temperature", "assertions", "monte_carlo"), where="simulation")
    backend_gate = backend
    if backend_gate is None:
        backend_gate = _gate_value(enforce_backend, default="alps")
    if not isinstance(setup["backend"], str) or not setup["backend"]:
        reject("simulation.backend must be a nonempty string")
    if not _matches_gate(setup["backend"], backend_gate):
        reject("Simulation backend is not accepted by the configured adapter", backend=setup["backend"])
    if setup["ground"] not in plan["nets"]:
        reject("Simulation ground must be a declared net")
    if "0" in plan["nets"] and setup["ground"] != "0":
        reject("SPICE node 0 is reserved for the configured ground")
    setup.setdefault("temperature", 27)
    finite(setup["temperature"], "temperature")
    analysis = setup["analysis"]
    fields(analysis, ("type", "source", "start", "stop", "step"), where="analysis")
    if analysis["type"] != "dc":
        reject("Only nominal DC transfer analysis is supported")
    for key in ("start", "stop", "step"):
        finite(analysis[key], key, positive=key == "step")
    if analysis["stop"] <= analysis["start"] or (analysis["stop"] - analysis["start"]) / analysis["step"] > 10000:
        reject("Invalid or excessive DC sweep")
    steps = (analysis["stop"] - analysis["start"]) / analysis["step"]
    if not math.isclose(steps, round(steps), abs_tol=1e-7):
        reject("DC stop must lie on the sweep grid")
    unique(setup["sources"], "sources")
    for source in setup["sources"]:
        fields(source, ("name", "positive", "negative", "dc"), where="source")
        if not source["name"].startswith("V") or source["positive"] == source["negative"]:
            reject("Voltage source names must start with V and connect distinct nets")
        if any(source[key] not in plan["nets"] for key in ("positive", "negative")):
            reject("Source refers to an undeclared net")
        finite(source["dc"], "source.dc")
    if analysis["source"] not in {source["name"] for source in setup["sources"]}:
        reject("DC source does not exist")
    signals = setup["signals"]
    if not isinstance(signals, list) or not signals or len(set(signals)) != len(signals) or any(signal not in plan["nets"] for signal in signals):
        reject("signals must be unique declared nets")
    mappings = setup["device_models"]
    if not isinstance(mappings, dict) or set(mappings) != {item["name"] for item in plan["instances"]}:
        reject("device_models must map every instance exactly once")
    spice_names = [source["name"].lower() for source in setup["sources"]]
    for instance in plan["instances"]:
        model = mappings[instance["name"]]
        fields(model, ("model", "terminals", "parameters"), where="device_model")
        identifier(model["model"], "model")
        if model["terminals"] != ["D", "G", "S", "B"] or set(instance["connections"]) != {"D", "G", "S", "B"}:
            reject("v1 netlisting supports four-terminal MOS devices only")
        if not isinstance(model["parameters"], dict) or not {"l", "w"} <= model["parameters"].keys() or model["parameters"].keys() - {"l", "w", "m", "nf"}:
            reject("MOS mapping requires l/w; m/nf are optional")
        for value in model["parameters"].values():
            if value not in instance["parameters"] or engineering(instance["parameters"][value]) <= 0:
                reject("Mapped MOS parameters must exist and be positive")
        spice_names.append(("M_" + instance["name"]).lower())
    if len(set(spice_names)) != len(spice_names) or len({net.lower() for net in plan["nets"]}) != len(plan["nets"]):
        reject("SPICE names collide case-insensitively")
    setup.setdefault("assertions", [])
    if not isinstance(setup["assertions"], list):
        reject("assertions must be an array")
    for assertion in setup["assertions"]:
        fields(assertion, ("signal", "at", "min", "max"), where="assertion")
        if assertion["signal"] not in signals:
            reject("Assertion signal is not recorded")
        for key in ("at", "min", "max"):
            finite(assertion[key], f"assertion.{key}")
        offset = (assertion["at"] - analysis["start"]) / analysis["step"]
        if not analysis["start"] <= assertion["at"] <= analysis["stop"] or not math.isclose(offset, round(offset), abs_tol=1e-7) or assertion["min"] > assertion["max"]:
            reject("Assertion must target a sweep point and a valid range")
    if "monte_carlo" in setup:
        monte_carlo = setup["monte_carlo"]
        fields(monte_carlo, ("samples", "seed", "variables"), ("min_pass_rate",), where="monte_carlo")
        if type(monte_carlo["samples"]) is not int or not 2 <= monte_carlo["samples"] <= 200:
            reject("monte_carlo.samples must be an integer from 2 to 200")
        if type(monte_carlo["seed"]) is not int or not 0 <= monte_carlo["seed"] <= 2**32 - 1:
            reject("monte_carlo.seed must be an unsigned 32-bit integer")
        monte_carlo.setdefault("min_pass_rate", 1.0)
        finite(monte_carlo["min_pass_rate"], "monte_carlo.min_pass_rate")
        if not 0 <= monte_carlo["min_pass_rate"] <= 1:
            reject("monte_carlo.min_pass_rate must be between 0 and 1")
        if not isinstance(monte_carlo["variables"], list) or not monte_carlo["variables"] or len(monte_carlo["variables"]) > 100:
            reject("monte_carlo.variables must contain 1–100 variables")
        instances = {item["name"]: item for item in plan["instances"]}
        seen = set()
        for variable in monte_carlo["variables"]:
            fields(variable, ("instance", "parameter", "distribution"), ("sigma", "low", "high"), where="monte_carlo.variable")
            if variable["instance"] not in instances or variable["parameter"] not in instances[variable["instance"]]["parameters"]:
                reject("Monte Carlo variable must refer to an instance parameter")
            key = (variable["instance"], variable["parameter"])
            if key in seen:
                reject("Monte Carlo variables must be unique")
            seen.add(key)
            if variable["distribution"] == "normal":
                if set(variable) != {"instance", "parameter", "distribution", "sigma"}:
                    reject("normal Monte Carlo variable requires sigma only")
                sigma = engineering(variable["sigma"])
                if sigma <= 0:
                    reject("Monte Carlo sigma must be positive")
            elif variable["distribution"] == "uniform":
                if set(variable) != {"instance", "parameter", "distribution", "low", "high"}:
                    reject("uniform Monte Carlo variable requires low and high only")
                low, high = engineering(variable["low"]), engineering(variable["high"])
                if low <= 0 or high <= low:
                    reject("Monte Carlo uniform range must be positive and ordered")
            else:
                reject("Unsupported Monte Carlo distribution")


def design_digest(plan):
    return digest({key: value for key, value in plan.items() if key not in ("mode", "simulation")})


__all__ = [
    "NAME", "NUMBER", "ORIENTATIONS", "canonical", "design_digest", "digest",
    "engineering", "fields", "finite", "identifier", "no_duplicate_keys",
    "numeric_text", "parameter_literal", "point", "property_records", "reject",
    "signal_identifier", "unique", "validate_plan", "validate_simulation",
]
