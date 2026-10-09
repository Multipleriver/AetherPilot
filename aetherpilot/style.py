"""Analog schematic style rules and read-only readability lint.

The ``analog_default`` rule set encodes the readability conventions used by
the certified fixtures (and inspired by the documented conventions of
agentic schematic tooling): vertical device layering (NMOS low, PMOS high),
differential-pair form (same row, R0/MY mirror, symmetric), pins on the
periphery, grid alignment, orthogonal non-crossing wires, labeled internal
nets, and canonical power-net names.  The linter never modifies a design;
it reports graded findings with object-level evidence so generated and
hand-drawn schematics can be checked with the same yardstick.
"""

from __future__ import annotations

import math
import re

from .contract import validate_plan
from .errors import ValidationError
from .planner import transform_point

_BODY_HALF = 0.75  # approximate symbol body half-extent used by overlap/periphery rules

STYLES = ("analog_default",)
DEFAULT_GRID = 0.0625

_CANONICAL_POWER = {"VDD", "VSS", "VDD!", "VSS!", "gnd!"}


def _device_kind(cell):
    """Classify a master cell as nmos/pmos/other without guessing beyond name markers."""
    name = str(cell).casefold()
    if name == "n18" or "nmos" in name or "nch" in name:
        return "nmos"
    if name == "p18" or "pmos" in name or "pch" in name:
        return "pmos"
    return "other"


def _finite_point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in value):
        return None
    return float(value[0]), float(value[1])


def _on_grid(value, grid):
    return math.isclose(value / grid, round(value / grid), rel_tol=0, abs_tol=1e-7)


def _segments(points):
    return [(a, b) for a, b in zip(points, points[1:]) if a != b]


def _segments_intersect(a1, a2, b1, b2):
    """Inclusive intersection test for axis-aligned segments."""
    ax1, ay1 = min(a1[0], a2[0]), min(a1[1], a2[1])
    ax2, ay2 = max(a1[0], a2[0]), max(a1[1], a2[1])
    bx1, by1 = min(b1[0], b2[0]), min(b1[1], b2[1])
    bx2, by2 = max(b1[0], b2[0]), max(b1[1], b2[1])
    return ax1 <= bx2 and ax2 >= bx1 and ay1 <= by2 and ay2 >= by1


def _is_orthogonal(a, b):
    return a[0] == b[0] or a[1] == b[1]


def _finding(rule_id, severity, obj, message, evidence=None):
    return {"rule_id": rule_id, "severity": severity, "object": obj, "message": message, "evidence": evidence or {}}


def _check_grid(instances, pins, wires, grid):
    findings = []
    off = []
    for instance in instances:
        origin = _finite_point(instance.get("origin"))
        if origin is None or not all(_on_grid(v, grid) for v in origin):
            off.append(("instance", instance.get("name"), origin))
    for pin in pins:
        position = _finite_point(pin.get("position"))
        if position is None or not all(_on_grid(v, grid) for v in position):
            off.append(("pin", pin.get("name"), position))
    for wire in wires:
        for point in wire.get("points", []):
            xy = _finite_point(point)
            if xy is None or not all(_on_grid(v, grid) for v in xy):
                off.append(("wire", wire.get("net"), xy))
    for kind, name, xy in off:
        findings.append(_finding("grid_alignment", "violation", str(name), f"{kind} {name} is off the {grid} grid", {"point": xy}))
    return findings


def _check_layering(instances):
    kinds = {item.get("name"): _device_kind(item.get("master", {}).get("cell", "")) for item in instances}
    nmos_y = [_finite_point(i.get("origin"))[1] for i in instances if kinds.get(i.get("name")) == "nmos" and _finite_point(i.get("origin"))]
    pmos_y = [_finite_point(i.get("origin"))[1] for i in instances if kinds.get(i.get("name")) == "pmos" and _finite_point(i.get("origin"))]
    if not nmos_y or not pmos_y:
        return []
    if min(pmos_y) > max(nmos_y):
        return []
    offenders = sorted(name for name, kind in kinds.items() if kind == "pmos" and
                       _finite_point(next(i for i in instances if i.get("name") == name).get("origin"))[1] <= max(nmos_y))
    return [_finding("device_layering", "violation", ", ".join(offenders),
                     "PMOS devices must sit strictly above all NMOS devices (NMOS low rows, PMOS high rows)",
                     {"max_nmos_y": max(nmos_y), "offenders": offenders})]


def _check_differential_pairs(instances):
    findings = []
    groups = {}
    for instance in instances:
        origin = _finite_point(instance.get("origin"))
        cell = instance.get("master", {}).get("cell", "")
        if origin is None or _device_kind(cell) == "other":
            continue
        groups.setdefault((cell, origin[1]), []).append(instance)
    for (cell, row), members in sorted(groups.items()):
        if len(members) != 2:
            continue
        gates = {m.get("connections", {}).get("G") for m in members}
        if len(gates) != 2:
            continue  # same-gate pairs are current mirrors, not differential pairs
        left, right = sorted(members, key=lambda m: _finite_point(m.get("origin"))[0])
        orientations = (left.get("orientation"), right.get("orientation"))
        names = f"{left.get('name')}, {right.get('name')}"
        if orientations != ("R0", "MY"):
            findings.append(_finding("differential_pair_form", "warning", names,
                                     "differential pair should be left R0 / right MY on one row",
                                     {"orientations": list(orientations), "row": row, "cell": cell}))
    return findings


def _instance_bodies(instances, masters):
    """Real transformed body bboxes per instance, when master geometry is given."""
    bodies = {}
    if not masters:
        return bodies
    for instance in instances:
        master = instance.get("master") or {}
        key = "/".join(str(master.get(field, "")) for field in ("library", "cell", "view"))
        geometry = masters.get(key)
        origin = _finite_point(instance.get("origin"))
        body = (geometry or {}).get("body_bbox") or (geometry or {}).get("bbox")
        if geometry is None or origin is None or not body:
            continue
        corners = [transform_point([x, y], origin, instance.get("orientation", "R0"))
                   for x in (body[0][0], body[1][0]) for y in (body[0][1], body[1][1])]
        bodies[instance.get("name")] = (min(p[0] for p in corners), min(p[1] for p in corners),
                                        max(p[0] for p in corners), max(p[1] for p in corners))
    return bodies


def _check_pin_periphery(instances, pins, bodies=None):
    findings = []
    for pin in pins:
        position = _finite_point(pin.get("position"))
        if position is None:
            continue
        if bodies:
            for instance in instances:
                bbox = bodies.get(instance.get("name"))
                if bbox is None:
                    continue
                if bbox[0] < position[0] < bbox[2] and bbox[1] < position[1] < bbox[3]:
                    findings.append(_finding("pin_periphery", "violation", str(pin.get("name")),
                                             "pin must sit on the design periphery, not inside instance bodies",
                                             {"position": list(position), "instance": instance.get("name")}))
            continue
        for instance in instances:
            origin = _finite_point(instance.get("origin"))
            if origin is None:
                continue
            if abs(position[0] - origin[0]) < _BODY_HALF and abs(position[1] - origin[1]) < _BODY_HALF:
                findings.append(_finding("pin_periphery", "violation", str(pin.get("name")),
                                         "pin must sit on the design periphery, not inside instance bodies",
                                         {"position": list(position), "instance": instance.get("name")}))
    return findings


def _check_wires(wires):
    findings = []
    by_net = {}
    for wire in wires:
        points = [_finite_point(p) for p in wire.get("points", [])]
        points = [p for p in points if p is not None]
        by_net.setdefault(wire.get("net"), []).extend(_segments(points))
    for net, segments in sorted(by_net.items(), key=lambda item: str(item[0])):
        for a, b in segments:
            if not _is_orthogonal(a, b):
                findings.append(_finding("wire_orthogonality", "violation", str(net),
                                         "wire segments must be horizontal or vertical", {"segment": [list(a), list(b)]}))
    nets = sorted(by_net, key=str)
    for index, net in enumerate(nets):
        for other in nets[index + 1:]:
            if not net or not other or net == other:
                continue
            for a1, a2 in by_net[net]:
                for b1, b2 in by_net[other]:
                    if _is_orthogonal(a1, a2) and _is_orthogonal(b1, b2) and _segments_intersect(a1, a2, b1, b2):
                        findings.append(_finding("wire_crossing", "violation", f"{net} / {other}",
                                                 "wires of different nets must not touch or cross",
                                                 {"nets": [net, other], "segment_a": [list(a1), list(a2)], "segment_b": [list(b1), list(b2)]}))
    return findings


def _check_net_labeling(readback, wires, pins):
    pin_nets = {pin.get("net") or pin.get("name") for pin in pins}
    label_nets = set()
    for label in readback.get("labels", []) or []:
        if isinstance(label, dict) and label.get("kind") == "wire_name" and label.get("net"):
            label_nets.add(label["net"])
    for text in readback.get("texts", []) or []:
        if isinstance(text, dict) and text.get("layer") == "wire" and text.get("net"):
            label_nets.add(text["net"])
    findings = []
    wired_nets = {wire.get("net") for wire in wires if wire.get("net")}
    for net in sorted(wired_nets - pin_nets - label_nets, key=str):
        findings.append(_finding("net_labeling", "warning", str(net),
                                 "internal net has wires but no wire_name label", {"net": net}))
    return findings


def _check_power_names(nets):
    findings = []
    for net in nets:
        if not isinstance(net, str):
            continue
        if net in _CANONICAL_POWER:
            continue
        if net.casefold().rstrip("!") in {"vdd", "vss", "gnd"}:
            findings.append(_finding("power_naming", "warning", net,
                                     "power/ground nets should use canonical names (VDD/VSS/gnd!)", {"net": net}))
    return findings


def _check_instance_overlap(instances, bodies=None):
    findings = []
    boxes = []
    for instance in instances:
        if bodies is not None:
            bbox = bodies.get(instance.get("name"))
            if bbox is None:
                continue
            boxes.append((instance.get("name"), bbox[0], bbox[2], bbox[1], bbox[3]))
            continue
        origin = _finite_point(instance.get("origin"))
        if origin is None:
            continue
        boxes.append((instance.get("name"), origin[0] - _BODY_HALF, origin[0] + _BODY_HALF,
                      origin[1] - _BODY_HALF, origin[1] + _BODY_HALF))
    for index, (name_a, ax1, ax2, ay1, ay2) in enumerate(boxes):
        for name_b, bx1, bx2, by1, by2 in boxes[index + 1:]:
            if ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1:
                findings.append(_finding("instance_overlap", "violation", f"{name_a} / {name_b}",
                                         "instance bodies overlap", {"instances": [name_a, name_b]}))
    return findings


def _check_signal_flow(pins):
    findings = []
    inputs = [p for p in pins if p.get("direction") == "input" and _finite_point(p.get("position"))
              and p.get("name", "").rstrip("!").casefold() not in {"vdd", "vss", "gnd"}]
    outputs = [p for p in pins if p.get("direction") == "output" and _finite_point(p.get("position"))]
    if not inputs or not outputs:
        return findings
    # Only a wholesale reversal is suspicious: every signal input sits right of
    # every output.  Bilateral fixtures (differential pairs) legitimately mix.
    leftmost_in = min(_finite_point(p["position"])[0] for p in inputs)
    rightmost_out = max(_finite_point(p["position"])[0] for p in outputs)
    if leftmost_in >= rightmost_out:
        findings.append(_finding("signal_flow", "warning", ", ".join(sorted(p["name"] for p in inputs + outputs)),
                                 "all signal inputs sit right of every output; expected a left-to-right flow",
                                 {"leftmost_input_x": round(leftmost_in, 4), "rightmost_output_x": round(rightmost_out, 4)}))
    return findings


def _segment_of(net_wires, point, tolerance=0.1):
    for wire in net_wires:
        points = [p for p in wire.get("points", []) if _finite_point(p)]
        for first, second in zip(points, points[1:]):
            if first[0] == second[0]:
                distance = abs(point[0] - first[0]) if min(first[1], second[1]) - 1e-9 <= point[1] <= max(first[1], second[1]) + 1e-9 else min(math.dist(point, first), math.dist(point, second))
            elif first[1] == second[1]:
                distance = abs(point[1] - first[1]) if min(first[0], second[0]) - 1e-9 <= point[0] <= max(first[0], second[0]) + 1e-9 else min(math.dist(point, first), math.dist(point, second))
            else:
                distance = min(math.dist(point, first), math.dist(point, second))
            if distance <= tolerance:
                return True
    return False


def _label_bbox(text, origin, height):
    width = max(0.125, len(str(text)) * float(height) * 0.6)
    return origin[0], origin[1], origin[0] + width, origin[1] + float(height)


def _check_labels(readback, wires):
    findings = []
    labels = []
    for label in readback.get("labels", []) or []:
        if isinstance(label, dict) and label.get("kind") == "wire_name" and label.get("net"):
            labels.append(label)
    for text in readback.get("texts", []) or []:
        if isinstance(text, dict) and text.get("layer") == "wire" and text.get("net"):
            labels.append(text)
    by_net = {}
    for wire in wires:
        by_net.setdefault(wire.get("net"), []).append(wire)
    boxes = []
    for label in labels:
        origin = _finite_point(label.get("origin"))
        net = label.get("net")
        text = label.get("text", net)
        if origin is None:
            continue
        if net in by_net and not _segment_of(by_net[net], origin):
            findings.append(_finding("label_on_wire", "violation", str(text),
                                     "wire_name label origin must sit on a wire of its net", {"net": net, "origin": list(origin)}))
        boxes.append((text, _label_bbox(text, origin, label.get("height", 0.0625))))
    for index, (name_a, box_a) in enumerate(boxes):
        for name_b, box_b in boxes[index + 1:]:
            if box_a[0] < box_b[2] and box_a[2] > box_b[0] and box_a[1] < box_b[3] and box_a[3] > box_b[1]:
                findings.append(_finding("label_collision", "warning", f"{name_a} / {name_b}",
                                         "wire_name labels overlap", {"labels": [name_a, name_b]}))
    return findings


def _check_wire_jogs(wires):
    findings = []
    by_net = {}
    for wire in wires:
        by_net.setdefault(wire.get("net"), []).append(wire)
    foreign_segments = {}
    for net, net_wires in by_net.items():
        foreign_segments[net] = []
        for wire in net_wires:
            points = [tuple(p) for p in wire.get("points", []) if _finite_point(p)]
            foreign_segments[net].extend(zip(points, points[1:]))
    all_segments = [segment for segments in foreign_segments.values() for segment in segments]
    for net, net_wires in sorted(by_net.items(), key=lambda item: str(item[0])):
        foreign = [segment for other, segments in foreign_segments.items() if other != net for segment in segments]
        for points in _merged_polylines(net_wires):
            for a, b, c, d in zip(points, points[1:], points[2:], points[3:]):
                dir1 = (math.copysign(1, b[0] - a[0]) if b[0] != a[0] else 0, math.copysign(1, b[1] - a[1]) if b[1] != a[1] else 0)
                dir2 = (math.copysign(1, c[0] - b[0]) if c[0] != b[0] else 0, math.copysign(1, c[1] - b[1]) if c[1] != b[1] else 0)
                dir3 = (math.copysign(1, d[0] - c[0]) if d[0] != c[0] else 0, math.copysign(1, d[1] - c[1]) if d[1] != c[1] else 0)
                bump = dir1 == tuple(-v for v in dir3) and dir2 != dir1 and dir2 != (0, 0)
                middle = math.dist(b, c)
                if not (bump and 0 < middle < 0.5):
                    continue
                # A bump that detours around a foreign wire or terminal is a
                # legitimate dodge, not a readability defect.
                if any(_segments_intersect(a, d, f1, f2) for f1, f2 in foreign if f1 != f2):
                    continue
                findings.append(_finding("wire_jog", "warning", str(net),
                                         "sub-grid jog (short out-and-back bump) reads as a defect at zoom",
                                         {"net": net, "at": [list(b), list(c)], "bump_length": round(middle, 4)}))
    return findings


def _merged_polylines(wires):
    """Join same-net two-point wire fragments into maximal polylines (native readbacks split at junctions)."""
    segments = []
    for wire in wires:
        points = [tuple(p) for p in wire.get("points", []) if _finite_point(p)]
        for a, b in zip(points, points[1:]):
            if a != b:
                segments.append((a, b))
    polylines = []
    while segments:
        chain = list(segments.pop())
        extended = True
        while extended:
            extended = False
            for index, (a, b) in enumerate(segments):
                if chain[-1] == a:
                    chain.append(b)
                elif chain[-1] == b:
                    chain.append(a)
                elif chain[0] == b:
                    chain.insert(0, a)
                elif chain[0] == a:
                    chain.insert(0, b)
                else:
                    continue
                segments.pop(index)
                extended = True
                break
        polylines.append(chain)
    return polylines


def _check_bend_budget(wires):
    findings = []
    by_net = {}
    for wire in wires:
        by_net.setdefault(wire.get("net"), []).append(wire)
    for net, net_wires in sorted(by_net.items(), key=lambda item: str(item[0])):
        bends = 0
        for points in _merged_polylines(net_wires):
            for previous, current, following in zip(points, points[1:], points[2:]):
                if (current[0] - previous[0], current[1] - previous[1]) != (following[0] - current[0], following[1] - current[1]):
                    bends += 1
        if bends > 16:
            findings.append(_finding("bend_budget", "warning", str(net),
                                     "one net with more than 16 bends is hard to follow; consider shorter routes or labels",
                                     {"net": net, "bends": bends}))
    return findings


def _check_pin_spacing(pins):
    findings = []
    positioned = [(p.get("name"), _finite_point(p.get("position"))) for p in pins]
    positioned = [(name, xy) for name, xy in positioned if xy is not None]
    for index, (name_a, a) in enumerate(positioned):
        for name_b, b in positioned[index + 1:]:
            if (abs(a[0] - b[0]) < 0.0625 and 0 < abs(a[1] - b[1]) < 1.0) or (abs(a[1] - b[1]) < 0.0625 and 0 < abs(a[0] - b[0]) < 1.0):
                findings.append(_finding("pin_spacing", "warning", f"{name_a} / {name_b}",
                                         "adjacent pins on one side should keep at least 1.0 pitch", {"distance": round(math.dist(a, b), 4)}))
    return findings


_RULES = ("grid_alignment", "device_layering", "differential_pair_form", "pin_periphery",
          "wire_orthogonality", "wire_crossing", "net_labeling", "power_naming", "instance_overlap",
          "signal_flow", "label_on_wire", "label_collision", "bend_budget", "pin_spacing", "wire_jog",
          "semantic_net_names", "semantic_instance_names")


def lint_schematic(readback, *, style="analog_default", grid=DEFAULT_GRID, masters=None, waivers=()):
    """Lint a schematic plan or stable readback against the analog_default style rules.

    When ``masters`` (native symbol geometry keyed by library/cell/view) is
    supplied, the overlap and pin-periphery rules use real transformed body
    bboxes instead of the coarse origin-centered heuristic — compact composed
    layouts are then judged by actual symbol bodies.  ``waivers`` names rules a
    recipe explicitly declares inapplicable (e.g. device_layering for a
    side-by-side transmission gate); waived findings are reported with severity
    "waived" and counted separately, never hidden.
    """
    if style not in STYLES:
        raise ValidationError("Unknown schematic style", stage="lint", code="invalid_style",
                              details={"style": style, "known": list(STYLES)})
    if not isinstance(readback, dict):
        raise ValidationError("Lint input must be a plan or readback object", stage="lint", code="invalid_lint_input")
    instances, pins, wires = readback.get("instances"), readback.get("pins"), readback.get("wires")
    if any(value is not None and not isinstance(value, list) for value in (instances, pins, wires)):
        raise ValidationError("Lint input instances/pins/wires must be arrays", stage="lint", code="invalid_lint_input")
    instances, pins, wires = instances or [], pins or [], wires or []
    bodies = _instance_bodies(instances, masters) if masters else None
    findings = []
    findings += _check_grid(instances, pins, wires, grid)
    findings += _check_layering(instances)
    findings += _check_differential_pairs(instances)
    findings += _check_pin_periphery(instances, pins, bodies)
    findings += _check_wires(wires)
    findings += _check_net_labeling(readback, wires, pins)
    findings += _check_power_names(readback.get("nets") or [w.get("net") for w in wires])
    findings += _check_instance_overlap(instances, bodies)
    findings += _check_signal_flow(pins)
    findings += _check_labels(readback, wires)
    findings += _check_bend_budget(wires)
    findings += _check_wire_jogs(wires)
    findings += _check_pin_spacing(pins)
    findings += _check_semantic_names(instances, readback.get("nets") or [w.get("net") for w in wires])
    waived = set(waivers)
    unknown_waivers = sorted(waived - set(_RULES))
    if unknown_waivers:
        raise ValidationError("Waiver references unknown lint rules", stage="lint", code="invalid_waiver",
                              details={"unknown": unknown_waivers})
    for finding in findings:
        if finding["rule_id"] in waived:
            finding["severity"] = "waived"
    violations = sum(1 for item in findings if item["severity"] == "violation")
    warnings = sum(1 for item in findings if item["severity"] == "warning")
    waived_count = sum(1 for item in findings if item["severity"] == "waived")
    return {"schema_version": 1, "style": style, "grid": grid, "ok": violations == 0,
            "geometry": "native_masters" if bodies else "origin_heuristic",
            "waivers": sorted(waived),
            "findings": findings, "rules": list(_RULES),
            "summary": {"rules": len(_RULES), "violations": violations, "warnings": warnings, "waived": waived_count,
                        "instances": len(instances), "pins": len(pins), "wires": len(wires)}}


def style_constraints(plan, *, style="analog_default"):
    """Derive soft analog_default placement constraints from a plan's devices and pins.

    All derived constraints are soft with priority -100, so any explicit user
    constraint wins and every relaxation is recorded in the placement
    decisions.  Rows: NMOS at y=0, other devices at y=4, PMOS at y=8; input
    pins form a left column, output pins a right column, power pins sit at the
    top (vdd-like) or bottom (vss/gnd-like).
    """
    if style not in STYLES:
        raise ValidationError("Unknown schematic style", stage="plan", code="invalid_style",
                              details={"style": style, "known": list(STYLES)})
    if not isinstance(plan, dict) or not isinstance(plan.get("instances"), list):
        raise ValidationError("Style constraints require a plan object", stage="plan", code="invalid_plan")
    constraints = []
    groups = {"nmos": [], "pmos": [], "other": []}
    for instance in plan["instances"]:
        kind = _device_kind(instance.get("master", {}).get("cell", ""))
        groups[kind].append("instance:" + instance["name"])
    row_y = {"nmos": 0, "other": 4, "pmos": 8}
    width = 0
    for kind in ("nmos", "other", "pmos"):
        members = sorted(groups[kind])
        width = max(width, len(members))
        if len(members) == 1:
            constraints.append({"id": f"style:{kind}-row", "type": "fixed", "object": members[0],
                                "position": [0, row_y[kind]], "strength": "soft", "priority": -100})
        elif members:
            constraints.append({"id": f"style:{kind}-row", "type": "row", "objects": members,
                                "coordinate": row_y[kind], "start": 0, "spacing": 3, "strength": "soft", "priority": -100})
    pins = [pin for pin in plan.get("pins", []) if isinstance(pin.get("name"), str)]
    inputs, outputs, inouts = [], [], []
    for pin in sorted(pins, key=lambda item: item["name"]):
        lowered = pin["name"].rstrip("!").casefold()
        if lowered in {"vdd", "vss", "gnd"}:
            y = 12 if lowered == "vdd" else -4
            constraints.append({"id": "style:power-" + pin["name"], "type": "fixed", "object": "pin:" + pin["name"],
                                "position": [0, y], "strength": "soft", "priority": -100})
        elif pin.get("direction") == "output":
            outputs.append("pin:" + pin["name"])
        elif pin.get("direction") == "inputOutput":
            inouts.append("pin:" + pin["name"])
        else:
            inputs.append("pin:" + pin["name"])
    # 区域约定：输入左下、输出右下、inout 底排、电源居中上下。
    if inputs:
        if len(inputs) == 1:
            constraints.append({"id": "style:input-column", "type": "fixed", "object": inputs[0],
                                "position": [-3, -2], "strength": "soft", "priority": -100})
        else:
            constraints.append({"id": "style:input-column", "type": "column", "objects": inputs,
                                "coordinate": -3, "start": -2, "spacing": 2, "strength": "soft", "priority": -100})
    if outputs:
        x = 3 * max(width, 1) + 3
        if len(outputs) == 1:
            constraints.append({"id": "style:output-column", "type": "fixed", "object": outputs[0],
                                "position": [x, -2], "strength": "soft", "priority": -100})
        else:
            constraints.append({"id": "style:output-column", "type": "column", "objects": outputs,
                                "coordinate": x, "start": -2, "spacing": 2, "strength": "soft", "priority": -100})
    for index, name in enumerate(inouts):
        constraints.append({"id": "style:inout-bottom-" + name, "type": "fixed", "object": name,
                            "position": [2 + index * 2, -4], "strength": "soft", "priority": -100})
    return constraints


def normalized_instance_parameters(parameters):
    """Drop unset CDF parameters (empty strings) that the plan contract cannot carry."""
    if not isinstance(parameters, dict):
        return {}
    return {key: value for key, value in parameters.items() if value != "" and value is not None}


def map_to_grid(readback, *, target, grid=DEFAULT_GRID, column_spacing=3.0, row_spacing=4.0, row_tolerance=1.0):
    """Map a flat schematic readback onto a clean uniform grid as a new create-mode plan.

    Rows are clustered by instance origin y (preserving the design's vertical
    order), columns by x within each row; differential pairs (same master, same
    row, gates on different nets) are normalized to left R0 / right MY.  The
    returned plan carries the same masters, parameters, connections, nets and
    pins (topology is preserved by construction and verifiable by digest); its
    wires are empty and must be produced by the deterministic router.
    """
    if not isinstance(readback, dict) or not isinstance(readback.get("instances"), list):
        raise ValidationError("Recreation requires a flat schematic readback", stage="plan", code="invalid_recreate_input")
    if readback.get("records"):
        raise ValidationError("Recreation currently accepts flat readbacks only", stage="plan", code="invalid_recreate_input")
    for value in (grid, column_spacing, row_spacing, row_tolerance):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValidationError("Recreation spacing and tolerance must be positive finite numbers", stage="plan", code="invalid_recreate_input")
    instances = []
    for instance in readback["instances"]:
        origin = _finite_point(instance.get("origin"))
        if origin is None or not isinstance(instance.get("connections"), dict) or not isinstance(instance.get("master"), dict):
            raise ValidationError("Recreation readback contains an instance without geometry or connectivity", stage="plan", code="invalid_recreate_input")
        instances.append(instance)
    rows = []
    for instance in sorted(instances, key=lambda item: (_finite_point(item["origin"])[1], _finite_point(item["origin"])[0], item["name"])):
        y = _finite_point(instance["origin"])[1]
        for row in rows:
            if abs(row["y"] - y) <= row_tolerance:
                row["members"].append(instance)
                break
        else:
            rows.append({"y": y, "members": [instance]})
    rows.sort(key=lambda row: row["y"])
    placed = {}
    for row_index, row in enumerate(rows):
        members = sorted(row["members"], key=lambda item: (_finite_point(item["origin"])[0], item["name"]))
        for column_index, instance in enumerate(members):
            placed[instance["name"]] = {"origin": [round(column_index * column_spacing, 9), round(row_index * row_spacing, 9)],
                                        "orientation": instance.get("orientation", "R0")}
    for row in rows:
        groups = {}
        for instance in row["members"]:
            cell = instance["master"].get("cell", "")
            if _device_kind(cell) == "other":
                continue
            groups.setdefault(cell, []).append(instance)
        for cell, members in groups.items():
            if len(members) != 2:
                continue
            gates = {member["connections"].get("G") for member in members}
            if len(gates) != 2:
                continue
            left, right = sorted(members, key=lambda item: placed[item["name"]]["origin"][0])
            placed[left["name"]]["orientation"] = "R0"
            placed[right["name"]]["orientation"] = "MY"
    source_pins = [pin for pin in readback.get("pins", []) if isinstance(pin, dict) and isinstance(pin.get("name"), str) and isinstance(pin.get("net"), str)]
    pins = []
    power = [pin for pin in source_pins if pin["name"].rstrip("!").casefold() in {"vdd", "vss", "gnd"}]
    inputs = [pin for pin in source_pins if pin not in power and pin.get("direction") != "output"]
    outputs = [pin for pin in source_pins if pin not in power and pin.get("direction") == "output"]
    max_column = max((placed[name]["origin"][0] for name in placed), default=0.0)
    top_y = len(rows) * row_spacing + 2
    bottom_y = -2.0
    for index, pin in enumerate(sorted(power, key=lambda item: item["name"])):
        y = top_y if pin["name"].rstrip("!").casefold() == "vdd" else bottom_y
        pins.append({"name": pin["name"], "net": pin["net"], "direction": pin.get("direction", "input"),
                     "position": [round(max_column / 2, 9), y]})
    for index, pin in enumerate(sorted(inputs, key=lambda item: item["name"])):
        pins.append({"name": pin["name"], "net": pin["net"], "direction": pin.get("direction", "input"),
                     "position": [-column_spacing, round(index * 2 * grid * 32, 9)]})
    for index, pin in enumerate(sorted(outputs, key=lambda item: item["name"])):
        pins.append({"name": pin["name"], "net": pin["net"], "direction": "output",
                     "position": [max_column + column_spacing, round(index * 2 * grid * 32, 9)]})
    nets = sorted({net for instance in instances for net in instance["connections"].values()} | {pin["net"] for pin in pins})
    plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
            "target": {**dict(target), "view": "schematic", "technology": readback.get("technology", "018um_PDK")},
            "instances": [{"name": instance["name"], "master": instance["master"],
                           "origin": placed[instance["name"]]["origin"], "orientation": placed[instance["name"]]["orientation"],
                           "parameters": normalized_instance_parameters(instance.get("parameters", {})), "connections": instance["connections"]}
                          for instance in sorted(instances, key=lambda item: item["name"])],
            "nets": nets, "pins": pins, "wires": []}
    if readback.get("port_order"):
        order = [name for name in readback["port_order"] if name in {pin["name"] for pin in pins}]
        if len(order) == len(pins):
            plan["port_order"] = order
    return validate_plan(plan)


def label_internal_nets(plan):
    """Add wire_name labels for wired nets that have no pin (internal nets).

    The native checker floats pin-less, unlabeled nets onto auto-created
    net0/net1 objects; anchoring a wire_name label on a generated wire keeps
    the intended net identity through check-and-save.  Returns the plan with a
    normalized ``labels`` list (possibly empty).
    """
    pinned = {pin["name"] for pin in plan.get("pins", [])}
    labels = [dict(label) for label in plan.get("labels", [])]
    labeled = {label.get("net") for label in labels if label.get("kind") == "wire_name"}
    for net in sorted(set(plan.get("nets", [])) - pinned - labeled):
        wire = next((item for item in plan.get("wires", []) if item.get("net") == net), None)
        if wire is None:
            continue
        labels.append({"kind": "wire_name", "text": net, "origin": list(wire["points"][0]), "orientation": "R0",
                       "height": 0.0625, "layer": "wire", "purpose": "label", "justification": "lowerLeft",
                       "font": "roman", "properties": [], "net": net})
    plan["labels"] = labels
    return plan


def _has_label_near(labels, net, point, *, tolerance=0.0625):
    for label in labels:
        if label.get("kind") != "wire_name" or label.get("net") != net:
            continue
        origin = _finite_point(label.get("origin"))
        if origin is not None and abs(origin[0] - point[0]) <= tolerance and abs(origin[1] - point[1]) <= tolerance:
            return True
    return False


def pin_lead_wires(plan, *, length=0.5, grid=DEFAULT_GRID):
    """Give every top-level pin an outward lead wire with a wire_name label.

    Region convention: input pins lead left, output and inout pins lead right,
    vdd-like power pins lead up, vss/gnd-like pins lead down.  The lead shares
    the pin's net, so connectivity is unchanged; the label anchors the net name
    at the pin for readability.  The conventional direction is tried first,
    but a lead that would touch a foreign net's wire or pin (routers legitimately
    tuck perimeter wires one grid step past a pin column) falls back to the
    first clear direction/length and fails closed with ``lead_blocked`` when
    every candidate is occupied.  Labels are deduplicated per lead midpoint,
    so a net may carry labels at several islands while repeated calls stay
    idempotent.
    """
    from .planner import intersections

    leads = {"left": (-length, 0), "right": (length, 0), "up": (0, length), "down": (0, -length)}
    wires = list(plan.get("wires", []))
    labels = [dict(label) for label in plan.get("labels", [])]
    foreign = [(wire["net"], tuple(float(v) for v in start), tuple(float(v) for v in end))
               for wire in wires for start, end in zip(wire["points"], wire["points"][1:])]
    pin_points = [(pin.get("net"), tuple(float(v) for v in pin["position"]))
                  for pin in plan.get("pins", []) if pin.get("net") and _finite_point(pin.get("position"))]

    def clear(net, origin, delta):
        end = (round(origin[0] + delta[0], 9), round(origin[1] + delta[1], 9))
        for other_net, start, stop in foreign:
            if other_net != net and intersections((origin, end), (start, stop)):
                return None
        if any(other_net != net and point != origin and
               intersections((origin, end), (point, point)) for other_net, point in pin_points):
            return None
        return end

    for pin in plan.get("pins", []):
        name, net = pin.get("name"), pin.get("net")
        origin = _finite_point(pin.get("position"))
        if not name or not net or origin is None:
            continue
        origin = (float(origin[0]), float(origin[1]))
        lowered = name.rstrip("!").casefold()
        direction = pin.get("direction", "input")
        if lowered == "vdd":
            preferred = leads["up"]
        elif lowered in {"vss", "gnd"}:
            preferred = leads["down"]
        elif direction == "input":
            preferred = leads["left"]
        else:
            preferred = leads["right"]
        candidates = [preferred]
        candidates += [delta for delta in (leads["up"], leads["down"], leads["left"], leads["right"]) if delta != preferred]
        candidates += [(delta[0] * scale, delta[1] * scale) for scale in (0.5, grid / length) for delta in list(candidates)]
        end = next((candidate for delta in candidates if (candidate := clear(net, origin, delta)) is not None), None)
        if end is None:
            raise ValidationError("Pin has no clearance for an outward lead", stage="plan", code="lead_blocked",
                                  details={"pin": name, "net": net})
        wires.append({"net": net, "points": [[origin[0], origin[1]], list(end)]})
        foreign.append((net, origin, end))
        mid = [round((origin[0] + end[0]) / 2, 9), round((origin[1] + end[1]) / 2, 9)]
        if not _has_label_near(labels, net, mid):
            labels.append({"kind": "wire_name", "text": net, "origin": mid, "orientation": "R0", "height": 0.0625,
                           "layer": "wire", "purpose": "label", "justification": "lowerLeft", "font": "roman",
                           "properties": [], "net": net})
    plan["wires"] = wires
    plan["labels"] = labels
    return plan


def power_stub_nets(plan, contacts, *, grid=DEFAULT_GRID, length=0.25):
    """Replace routed power rails with per-terminal labeled stubs.

    Terminals on a pin-connected power net (vdd/vss/gnd-like) each get a short
    stub plus a wire_name label; the pin keeps its own lead and label from
    pin_lead_wires.  Stub direction prefers up for vdd-like nets and down for
    vss/gnd-like nets, but falls back to the first direction/length that does
    not touch a foreign net's wire or terminal (transistor bulk/source
    terminals sit one grid step apart, so a naive fixed-direction stub can
    short them).  The plan's routing_policy gains ``allow_label_islands`` so
    the intentional label islands compile cleanly.
    """
    from .planner import intersections

    power_nets = {net for net in plan.get("nets", []) if isinstance(net, str) and net.rstrip("!").casefold() in {"vdd", "vss", "gnd"}}
    pin_nets = {pin.get("net") for pin in plan.get("pins", [])}
    power_nets &= pin_nets
    if not power_nets:
        return plan
    plan["wires"] = [wire for wire in plan.get("wires", []) if wire.get("net") not in power_nets]
    foreign = [(wire["net"], tuple(float(v) for v in start), tuple(float(v) for v in end))
               for wire in plan["wires"] for start, end in zip(wire["points"], wire["points"][1:])]
    contact_points = [(net, tuple(float(v) for v in position)) for net, position, _label in contacts]

    def clear(net, point, delta):
        end = (round(point[0] + delta[0], 9), round(point[1] + delta[1], 9))
        for other_net, start, stop in foreign:
            if other_net != net and intersections((point, end), (start, stop)):
                return None
        if any(other_net != net and other_point != point and
               intersections((point, end), (other_point, other_point)) for other_net, other_point in contact_points):
            return None
        return end

    labels = [dict(label) for label in plan.get("labels", [])]
    seen_points = set()
    for net, position, _label in contacts:
        if net not in power_nets:
            continue
        point = tuple(float(value) for value in position)
        if point in seen_points:
            continue
        seen_points.add(point)
        preferred = (0.0, length) if net.rstrip("!").casefold() == "vdd" else (0.0, -length)
        candidates = [preferred]
        candidates += [delta for delta in ((0.0, length), (0.0, -length), (length, 0.0), (-length, 0.0)) if delta != preferred]
        candidates += [(delta[0] * scale, delta[1] * scale) for scale in (0.5, grid / length) for delta in list(candidates)]
        end = next((candidate for delta in candidates if (candidate := clear(net, point, delta)) is not None), None)
        if end is None:
            raise ValidationError("Power terminal has no clearance for a labeled stub", stage="plan", code="stub_blocked",
                                  details={"net": net, "terminal": _label})
        plan["wires"].append({"net": net, "points": [list(point), list(end)]})
        foreign.append((net, point, end))
        mid = [round((point[0] + end[0]) / 2, 9), round((point[1] + end[1]) / 2, 9)]
        if not _has_label_near(labels, net, mid):
            labels.append({"kind": "wire_name", "text": net, "origin": mid, "orientation": "R0", "height": 0.0625,
                           "layer": "wire", "purpose": "label", "justification": "lowerLeft", "font": "roman",
                           "properties": [], "net": net})
    plan["labels"] = labels
    policy = dict(plan.get("routing_policy", {}))
    policy["allow_label_islands"] = True
    plan["routing_policy"] = policy
    return plan


_AUTO_NET = re.compile(r"^(net\d+|N_h_[0-9a-f]+|I_h_[0-9a-f]+|n\d+)$", re.I)
_AUTO_INST = re.compile(r"^(\d+|[A-Z])$")


def _check_semantic_names(instances, nets):
    findings = []
    for net in nets:
        if isinstance(net, str) and _AUTO_NET.match(net):
            findings.append(_finding("semantic_net_names", "violation", net,
                                     "net names must carry semantics; auto names are rejected", {"net": net}))
    for instance in instances:
        name = instance.get("name")
        if isinstance(name, str) and _AUTO_INST.match(name):
            findings.append(_finding("semantic_instance_names", "warning", name,
                                     "instance names should carry device-class semantics (e.g. MN1/MP3/RTAIL)", {"name": name}))
    return findings


def semantic_net_renames(plan):
    """Rename auto-generated nets from the devices/terminals they connect."""
    used = {net for net in plan.get("nets", []) if isinstance(net, str)}
    renames = {}
    for net in sorted(used):
        if not _AUTO_NET.match(net):
            continue
        endpoints = sorted({f"{instance['name']}_{terminal}" for instance in plan.get("instances", [])
                            for terminal, net_name in instance.get("connections", {}).items() if net_name == net})
        if not endpoints:
            continue
        derived = "_".join(endpoints)[:80]
        if derived and derived not in used:
            renames[net] = derived
            used.add(derived)
    return renames


def apply_net_renames(plan, renames):
    """Apply a net-rename mapping across nets, connections, pins, wires and labels."""
    if not renames:
        return plan
    plan["nets"] = sorted({renames.get(net, net) for net in plan.get("nets", [])})
    for instance in plan.get("instances", []):
        instance["connections"] = {terminal: renames.get(net, net) for terminal, net in instance.get("connections", {}).items()}
    for pin in plan.get("pins", []):
        if pin.get("net") in renames:
            pin["net"] = renames[pin["net"]]
    for wire in plan.get("wires", []):
        if wire.get("net") in renames:
            wire["net"] = renames[wire["net"]]
    for label in plan.get("labels", []):
        if label.get("net") in renames:
            label["net"] = renames[label["net"]]
        if label.get("kind") == "wire_name" and label.get("text") in renames:
            label["text"] = renames[label["text"]]
    return plan


__all__ = ["DEFAULT_GRID", "STYLES", "apply_net_renames", "label_internal_nets", "lint_schematic", "map_to_grid", "normalized_instance_parameters", "pin_lead_wires", "power_stub_nets", "semantic_net_renames", "style_constraints"]
