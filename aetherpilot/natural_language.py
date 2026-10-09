"""Deterministic natural-language-to-schematic compilation.

This module deliberately does not execute arbitrary language-model output.  It
accepts a small, auditable vocabulary, compiles it to the existing schematic
IR, and reports every assumption.  A caller can then pass the IR through the
normal placement/routing and native check/save gates.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .contract import identifier, signal_identifier, validate_plan
from .errors import ValidationError


_DEVICE_MASTERS = {
    "nmos": ("n18", {"D": "", "G": "", "S": "", "B": ""}),
    "pmos": ("p18", {"D": "", "G": "", "S": "", "B": ""}),
    "mos": ("n18", {"D": "", "G": "", "S": "", "B": ""}),
    "resistor": ("primRes2", {"PLUS": "", "MINUS": ""}),
    "res": ("primRes2", {"PLUS": "", "MINUS": ""}),
    "capacitor": ("mim", {"PLUS": "", "MINUS": ""}),
    "cap": ("mim", {"PLUS": "", "MINUS": ""}),
    "diode": ("ndio18", {"PLUS": "", "MINUS": ""}),
    "bjt": ("npn18_W5L5", {"C": "", "B": "", "E": ""}),
    "npn": ("npn18_W5L5", {"C": "", "B": "", "E": ""}),
    "pnp": ("pnp18_W5L5", {"C": "", "B": "", "E": ""}),
}
_GROUND_ALIASES = {"gnd": "VSS", "ground": "VSS", "vss!": "VSS", "vdd!": "VDD"}
_DEVICE_RE = re.compile(r"^\s*(?P<kind>nmos|pmos|mos|resistor|res|capacitor|cap|diode|bjt|npn|pnp)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b(?P<body>.*)$", re.I)
_KV_RE = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=\s*(?P<value>[A-Za-z0-9_.+\-]+)")
_BETWEEN_RE = re.compile(r"\bbetween\s+(?P<a>[A-Za-z0-9_<>!]+)\s+and\s+(?P<b>[A-Za-z0-9_<>!]+)", re.I)
_RESISTANCE_RE = re.compile(r"^\s*(?P<value>[A-Za-z0-9_.+\-]+)\s+between\b", re.I)
_TERM_RE = re.compile(r"\b(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*[:=]\s*(?P<value>[A-Za-z0-9_<>!]+)")
_PARAMETER_ALIASES = {
    "w": "w", "width": "w", "l": "Length", "length": "Length",
    "nf": "Fingers", "fingers": "Fingers", "m": "m",
}


def _net(value: str) -> str:
    value = value.strip().strip(",;()")
    return _GROUND_ALIASES.get(value.casefold(), value)


def _literal(value: str) -> str:
    # Parameters are intentionally literals only; expressions are not a
    # natural-language escape hatch into the database.
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", value):
        raise ValidationError("Natural-language parameter is not a literal", stage="plan", code="invalid_natural_language")
    return value


def _template_inverter(target: Mapping[str, Any], grid: float) -> dict[str, Any]:
    """Return the certified, deterministic two-transistor inverter template."""
    return validate_plan({
        "schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
        "target": {**dict(target), "technology": target.get("technology", "018um_PDK"), "view": "schematic"},
        "instances": [
            {"name": "MP", "master": {"library": "018um_PDK", "cell": "p18", "view": "symbol"},
             "origin": [4, 6], "orientation": "R0", "parameters": {"Length": "0.18u", "w": "1u", "Fingers": "1", "m": "1"},
             "connections": {"D": "OUT", "G": "IN", "S": "VDD", "B": "VDD"}},
            {"name": "MN", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [4, 3], "orientation": "R0", "parameters": {"Length": "0.18u", "w": "0.6u", "Fingers": "1", "m": "1"},
             "connections": {"D": "OUT", "G": "IN", "S": "VSS", "B": "VSS"}},
        ],
        "nets": ["IN", "OUT", "VDD", "VSS"],
        "pins": [{"name": "IN", "net": "IN", "direction": "input", "position": [2, 4.5]},
                 {"name": "OUT", "net": "OUT", "direction": "output", "position": [6, 4.5]},
                 {"name": "VDD", "net": "VDD", "direction": "input", "position": [4.25, 7]},
                 {"name": "VSS", "net": "VSS", "direction": "input", "position": [4.25, 2]}],
        "wires": [
            {"net": "VDD", "points": [[4.25, 6.1875], [4.25, 7], [5.25, 7], [5.25, 6], [4.25, 6]]},
            {"net": "VSS", "points": [[4.25, 2.8125], [4.25, 2], [5.25, 2], [5.25, 3], [4.25, 3]]},
            {"net": "IN", "points": [[4, 6], [3, 6], [3, 3], [4, 3]]},
            {"net": "IN", "points": [[2, 4.5], [3, 4.5]]},
            {"net": "OUT", "points": [[4.25, 5.8125], [4.75, 5.8125], [4.75, 3.1875], [4.25, 3.1875]]},
            {"net": "OUT", "points": [[4.75, 4.5], [6, 4.5]]},
        ],
        "port_order": ["IN", "OUT", "VDD", "VSS"],
    })


def _template_ota5(target: Mapping[str, Any], grid: float) -> dict[str, Any]:
    """Return the certified five-transistor OTA fixture as a deterministic template."""
    return validate_plan({
        "schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
        "target": {**dict(target), "technology": target.get("technology", "018um_PDK"), "view": "schematic"},
        "instances": [
            {"name": "MN1", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [4, 6], "orientation": "R0",
             "parameters": {"Length": "0.36u", "Single_Width": "4u", "Fingers": "1", "m": "1", "pd": "4.5u", "ps": "4.5u"},
             "connections": {"D": "MIRROR", "G": "INP", "S": "TAIL", "B": "VSS"}},
            {"name": "MN2", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [10, 6], "orientation": "MY",
             "parameters": {"Length": "0.36u", "Single_Width": "4u", "Fingers": "1", "m": "1", "pd": "4.5u", "ps": "4.5u"},
             "connections": {"D": "OUT", "G": "INN", "S": "TAIL", "B": "VSS"}},
            {"name": "MP3", "master": {"library": "018um_PDK", "cell": "p18", "view": "symbol"},
             "origin": [4, 12], "orientation": "R0",
             "parameters": {"Length": "0.36u", "Single_Width": "8u", "Fingers": "1", "m": "1", "pd": "8.5u", "ps": "8.5u"},
             "connections": {"D": "MIRROR", "G": "MIRROR", "S": "VDD", "B": "VDD"}},
            {"name": "MP4", "master": {"library": "018um_PDK", "cell": "p18", "view": "symbol"},
             "origin": [10, 12], "orientation": "R0",
             "parameters": {"Length": "0.36u", "Single_Width": "8u", "Fingers": "1", "m": "1", "pd": "8.5u", "ps": "8.5u"},
             "connections": {"D": "OUT", "G": "MIRROR", "S": "VDD", "B": "VDD"}},
            {"name": "MN5", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [7, -2], "orientation": "R0",
             "parameters": {"Length": "0.72u", "Single_Width": "8u", "Fingers": "1", "m": "1", "pd": "8.5u", "ps": "8.5u"},
             "connections": {"D": "TAIL", "G": "BIASN", "S": "VSS", "B": "VSS"}},
            {"name": "RMIRROR", "master": {"library": "analog", "cell": "res", "view": "symbol"},
             "origin": [2, 11.8125], "orientation": "R0", "parameters": {"R": "1meg"},
             "connections": {"P": "MIRROR", "N": "VSS"}},
        ],
        "nets": ["INP", "INN", "OUT", "VDD", "VSS", "BIASN", "TAIL", "MIRROR"],
        "pins": [{"name": "INP", "net": "INP", "direction": "input", "position": [2, 6]},
                 {"name": "INN", "net": "INN", "direction": "input", "position": [13, 7]},
                 {"name": "OUT", "net": "OUT", "direction": "output", "position": [15, 9]},
                 {"name": "VDD", "net": "VDD", "direction": "input", "position": [7, 16]},
                 {"name": "VSS", "net": "VSS", "direction": "input", "position": [1.5, -8]},
                 {"name": "BIASN", "net": "BIASN", "direction": "input", "position": [12, -3]}],
        "labels": [
            {"kind": "wire_name", "text": "MIRROR", "origin": [6, 11.8125], "orientation": "R0", "height": 0.0625,
             "layer": "wire", "purpose": "label", "justification": "lowerLeft", "font": "roman", "properties": [], "net": "MIRROR"},
            {"kind": "wire_name", "text": "TAIL", "origin": [7, 5.8125], "orientation": "R0", "height": 0.0625,
             "layer": "wire", "purpose": "label", "justification": "lowerLeft", "font": "roman", "properties": [], "net": "TAIL"},
        ],
        "wires": [
            {"net": "VDD", "points": [[7, 16], [4.25, 16], [4.25, 12.1875]]},
            {"net": "VDD", "points": [[4.25, 12.1875], [4.25, 12]]},
            {"net": "VDD", "points": [[4.25, 12], [9.9375, 12], [9.9375, 12.0625], [10.25, 12.0625], [10.25, 12]]},
            {"net": "VDD", "points": [[10.25, 12], [10.3125, 12], [10.3125, 12.125], [10.25, 12.125], [10.25, 12.1875]]},
            {"net": "VSS", "points": [[1.5, -8], [13, -8], [13, -2]]},
            {"net": "VSS", "points": [[13, -2], [7.25, -2]]},
            {"net": "VSS", "points": [[7.25, -2], [7.25, -2.1875]]},
            {"net": "VSS", "points": [[13, 5.875], [9.75, 5.875], [9.75, 6]]},
            {"net": "VSS", "points": [[9.75, 6], [4.25, 6]]},
            {"net": "INN", "points": [[13, 7], [10, 7], [10, 6]]},
            {"net": "OUT", "points": [[15, 9], [10.25, 9], [10.25, 11.8125]]},
            {"net": "OUT", "points": [[10.25, 11.8125], [9.75, 11.8125], [9.75, 6.1875]]},
            {"net": "TAIL", "points": [[4.25, 5.8125], [9.75, 5.8125]]},
            {"net": "TAIL", "points": [[7, 5.8125], [7, -1.5], [7.25, -1.5], [7.25, -1.8125]]},
            {"net": "MIRROR", "points": [[4, 12], [4.1875, 12], [4.1875, 11.9375], [4.25, 11.9375], [4.25, 11.8125]]},
            {"net": "MIRROR", "points": [[4.25, 11.8125], [9.6875, 11.8125], [9.6875, 11.875], [10, 11.875], [10, 12]]},
            {"net": "MIRROR", "points": [[2, 11.8125], [1.5, 11.8125], [1.5, 12.5], [3.5, 12.5], [3.5, 11.5], [6, 11.5], [6, 11.8125]]},
            {"net": "MIRROR", "points": [[4.25, 6.1875], [6.5, 6.1875], [6.5, 11.5], [6.5, 11.8125]]},
            {"net": "INP", "points": [[2, 6], [4, 6]]},
            {"net": "VSS", "points": [[13, -2], [13, 5.875]]},
            {"net": "BIASN", "points": [[12, -3], [12, -2.5], [6.5, -2.5], [6.5, -2], [7, -2]]},
            {"net": "VSS", "points": [[2.375, 11.8125], [2.375, 6.0625], [0.5, 6.0625], [0.5, -8], [1.5, -8]]},
        ],
        "port_order": ["INP", "INN", "OUT", "VDD", "VSS", "BIASN"],
    })


def _template_diffpair(target: Mapping[str, Any], grid: float) -> dict[str, Any]:
    """Return a deterministic differential pair with tail current source."""
    return validate_plan({
        "schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
        "target": {**dict(target), "technology": target.get("technology", "018um_PDK"), "view": "schematic"},
        "instances": [
            {"name": "MN1", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [4, 6], "orientation": "R0",
             "parameters": {"Length": "0.36u", "Single_Width": "4u", "Fingers": "1", "m": "1", "pd": "4.5u", "ps": "4.5u"},
             "connections": {"D": "OUTP", "G": "INP", "S": "TAIL", "B": "VSS"}},
            {"name": "MN2", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [10, 6], "orientation": "MY",
             "parameters": {"Length": "0.36u", "Single_Width": "4u", "Fingers": "1", "m": "1", "pd": "4.5u", "ps": "4.5u"},
             "connections": {"D": "OUTN", "G": "INN", "S": "TAIL", "B": "VSS"}},
            {"name": "MN5", "master": {"library": "018um_PDK", "cell": "n18", "view": "symbol"},
             "origin": [7, -2], "orientation": "R0",
             "parameters": {"Length": "0.72u", "Single_Width": "8u", "Fingers": "1", "m": "1", "pd": "8.5u", "ps": "8.5u"},
             "connections": {"D": "TAIL", "G": "BIASN", "S": "VSS", "B": "VSS"}},
        ],
        "nets": ["INP", "INN", "OUTP", "OUTN", "VSS", "BIASN", "TAIL"],
        "pins": [{"name": "INP", "net": "INP", "direction": "input", "position": [2, 6]},
                 {"name": "INN", "net": "INN", "direction": "input", "position": [13, 7]},
                 {"name": "OUTP", "net": "OUTP", "direction": "output", "position": [2, 8]},
                 {"name": "OUTN", "net": "OUTN", "direction": "output", "position": [12, 7.5]},
                 {"name": "BIASN", "net": "BIASN", "direction": "input", "position": [12, -3]},
                 {"name": "VSS", "net": "VSS", "direction": "input", "position": [1.5, -8]}],
        "labels": [
            {"kind": "wire_name", "text": "TAIL", "origin": [7, 5.8125], "orientation": "R0", "height": 0.0625,
             "layer": "wire", "purpose": "label", "justification": "lowerLeft", "font": "roman", "properties": [], "net": "TAIL"},
        ],
        "wires": [
            {"net": "INP", "points": [[2, 6], [4, 6]]},
            {"net": "INN", "points": [[13, 7], [10, 7], [10, 6]]},
            {"net": "OUTP", "points": [[2, 8], [2, 6.1875], [4.25, 6.1875]]},
            {"net": "OUTN", "points": [[9.75, 6.1875], [9.75, 7.5], [12, 7.5]]},
            {"net": "TAIL", "points": [[4.25, 5.8125], [9.75, 5.8125]]},
            {"net": "TAIL", "points": [[7, 5.8125], [7, -1.5], [7.25, -1.5], [7.25, -1.8125]]},
            {"net": "VSS", "points": [[1.5, -8], [13, -8], [13, -2]]},
            {"net": "VSS", "points": [[13, -2], [7.25, -2]]},
            {"net": "VSS", "points": [[7.25, -2], [7.25, -2.1875]]},
            {"net": "VSS", "points": [[13, 5.875], [9.75, 5.875], [9.75, 6]]},
            {"net": "VSS", "points": [[9.75, 6], [4.25, 6]]},
            {"net": "VSS", "points": [[13, -2], [13, 5.875]]},
            {"net": "BIASN", "points": [[12, -3], [12, -2.5], [6.5, -2.5], [6.5, -2], [7, -2]]},
        ],
        "port_order": ["INP", "INN", "OUTP", "OUTN", "BIASN", "VSS"],
    })


def natural_language_to_plan(description: str, *, target: Mapping[str, Any], grid: float = 0.0625,
                             allow_draft: bool = True) -> dict[str, Any]:
    """Compile a bounded English/Chinese circuit description into schematic IR.

    Supported deterministic forms include ``CMOS inverter``, ``OTA5`` (five-transistor
    OTA), ``differential pair`` and one-device declarations such as
    ``nmos MN D=OUT G=IN S=VSS B=VSS w=1u`` or ``resistor R1 1k between IN and OUT``.
    Unknown or ambiguous statements fail closed with line-numbered diagnostics.
    """
    if not isinstance(description, str) or not description.strip() or len(description.encode()) > 256 * 1024:
        raise ValidationError("Natural-language circuit description is empty or too large", stage="plan", code="invalid_natural_language")
    if not isinstance(target, Mapping) or not all(isinstance(target.get(key), str) for key in ("library", "cell")):
        raise ValidationError("Natural-language compilation requires a library and cell target", stage="plan", code="invalid_natural_language")
    lowered = description.casefold()
    if "inverter" in lowered or "反相器" in description:
        return {"plan": _template_inverter(target, grid), "compiler": "deterministic_template",
                "template": "cmos_inverter", "assumptions": ["018um_PDK n18/p18", "VSS is ground"],
                "diagnostics": [], "requires_planning": False}
    if "ota5" in lowered or "五管" in description or re.search(r"\bota\b", lowered):
        return {"plan": _template_ota5(target, grid), "compiler": "deterministic_template",
                "template": "ota5",
                "assumptions": ["018um_PDK n18/p18 and analog res", "certified OTA5 fixture geometry", "VSS is ground"],
                "diagnostics": [], "requires_planning": False}
    if "差分对" in description or "differential pair" in lowered or "diffpair" in lowered or "diff pair" in lowered:
        return {"plan": _template_diffpair(target, grid), "compiler": "deterministic_template",
                "template": "diffpair",
                "assumptions": ["018um_PDK n18", "differential pair with tail current source", "VSS is ground"],
                "diagnostics": [], "requires_planning": False}

    instances, nets, explicit_ports, diagnostics = [], set(), {}, []
    for line_number, raw in enumerate(description.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", "//", "；", ";")):
            continue
        match = _DEVICE_RE.match(line)
        if not match:
            port_match = re.match(r"^(?:inputs?|输入)\s*[:：]\s*(.+)$", line, re.I)
            out_match = re.match(r"^(?:outputs?|输出)\s*[:：]\s*(.+)$", line, re.I)
            if port_match or out_match:
                direction = "input" if port_match else "output"
                for name in re.split(r"[,\s]+", (port_match or out_match).group(1).strip()):
                    if name:
                        explicit_ports[_net(name)] = direction
                continue
            diagnostics.append({"line": line_number, "code": "unsupported_sentence", "text": line})
            continue
        kind, name, body = match.group("kind").casefold(), match.group("name"), match.group("body")
        master_cell, terminals = _DEVICE_MASTERS[kind]
        connections = dict(terminals)
        for term in _TERM_RE.finditer(body):
            key = term.group("key").upper()
            if key in connections:
                connections[key] = _net(term.group("value"))
        between = _BETWEEN_RE.search(body)
        if between:
            values = [_net(between.group("a")), _net(between.group("b"))]
            keys = list(connections)
            connections[keys[0]], connections[keys[1]] = values
        if any(not value for value in connections.values()):
            diagnostics.append({"line": line_number, "code": "missing_terminal", "instance": name,
                                "required": [key for key, value in connections.items() if not value]})
            continue
        parameters = {}
        if master_cell == "primRes2":
            resistance = _RESISTANCE_RE.search(body)
            if resistance:
                parameters.update({"rprim": _literal(resistance.group("value")), "scaleprim": "1",
                                   "lprim": "1u", "wprim": "1u", "isnoisy": "no"})
        for item in _KV_RE.finditer(body):
            key, value = item.group("key"), item.group("value")
            if key.upper() not in connections and key.casefold() not in {"between", "and"}:
                canonical = _PARAMETER_ALIASES.get(key.casefold(), key) if master_cell in {"n18", "p18"} else key
                parameters[canonical] = _literal(value)
        try:
            identifier(name, "natural-language instance")
        except ValidationError as exc:
            diagnostics.append({"line": line_number, "code": exc.code, "instance": name})
            continue
        instances.append({"name": name, "master": {"library": "018um_PDK", "cell": master_cell, "view": "symbol"},
                          "origin": [float(4 * len(instances)), 0.0], "orientation": "R0",
                          "parameters": parameters or {"m": "1"}, "connections": connections})
        nets.update(connections.values())
    nets.update(explicit_ports)
    if diagnostics:
        raise ValidationError("Natural-language circuit contains unsupported or ambiguous statements", stage="plan",
                              code="invalid_natural_language", details={"diagnostics": diagnostics})
    if not instances:
        raise ValidationError("Natural-language circuit contains no devices", stage="plan", code="invalid_natural_language")
    nets = sorted(signal_identifier(item) for item in nets)
    pins = []
    for index, name in enumerate(sorted(explicit_ports)):
        pins.append({"name": name, "net": name, "direction": explicit_ports[name], "position": [-4.0, float(index * 2)]})
    # This is intentionally a draft: native terminal geometry is required to
    # synthesize legal wires.  Harness.plan_schematic performs that step.
    plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
            "target": {**dict(target), "technology": target.get("technology", "018um_PDK"), "view": "schematic"},
            "instances": instances, "nets": nets, "pins": pins, "wires": [],
            "port_order": [pin["name"] for pin in pins] if pins else None}
    if plan["port_order"] is None:
        plan.pop("port_order")
    return {"plan": validate_plan(plan), "compiler": "bounded_device_grammar", "template": None,
            "assumptions": ["018um_PDK masters", "native placement/routing is still required"],
            "diagnostics": [], "requires_planning": True, "draft": bool(allow_draft)}


def natural_language_capabilities() -> dict[str, Any]:
    return {"schema_version": 1, "deterministic": True, "templates": ["cmos_inverter", "ota5", "diffpair"],
            "device_grammar": sorted(_DEVICE_MASTERS), "port_keywords": ["input", "output", "输入", "输出"],
            "requires_native_planning_for": ["generic_device_wire_geometry"],
            "unsafe_features_rejected": ["expressions", "shell", "python", "unknown_devices", "ambiguous_terminals"]}


__all__ = ["natural_language_to_plan", "natural_language_capabilities"]
