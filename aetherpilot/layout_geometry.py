"""Shared layout geometry and verification helpers for structured recipes.

These primitives back the recipe library in :mod:`aetherpilot.layout_recipes`:
grid snapping, orientation-aware symbol geometry, gate-side orientation
selection verified against real terminal transforms, body-collision checks
with per-terminal approach zones, role-map validation, plan assembly with
electrical data copied verbatim from the source, and H-normalized metrics.
Everything is deterministic and offline; native writing lives in the worker.
"""

from __future__ import annotations

import math

from .contract import validate_plan
from .errors import ValidationError
from .planner import compile_wires, transform_point


GRID = 0.0625

# Per-terminal approach zones: a wire may enter a symbol body only to reach a
# terminal of that instance, and only within this radius of the terminal point
# (the 018um res N terminal sits 0.025 inside the body bbox).  Approaching a
# different point or crossing the body from another side stays illegal.
TERMINAL_APPROACH_ALLOWANCE = 0.0625


def snap(value, grid=GRID):
    return round(round(value / grid) * grid, 9)


def transformed(master, orientation):
    """Symbol body bbox and terminal offsets under an orientation."""
    body = master.get("body_bbox", master["bbox"])
    corners = [transform_point([x, y], [0, 0], orientation)
               for x in (body[0][0], body[1][0]) for y in (body[0][1], body[1][1])]
    bbox = [[min(p[0] for p in corners), min(p[1] for p in corners)],
            [max(p[0] for p in corners), max(p[1] for p in corners)]]
    terminals = {name: transform_point(list(position), [0, 0], orientation)
                 for name, position in master["terminals"].items()}
    return {"bbox": bbox, "terminals": terminals}


def pick_orientation(master, expected_side, *, role=""):
    """Choose R0/MY so the gate faces ``expected_side``, verified by geometry."""
    for orientation in ("R0", "MY"):
        geometry = transformed(master, orientation)
        gate = geometry["terminals"]["G"]
        center_x = (geometry["bbox"][0][0] + geometry["bbox"][1][0]) / 2
        side = "left" if gate[0] <= center_x else "right"
        if side == expected_side:
            return orientation
    raise ValidationError("No legal orientation makes the gate face the role's side", stage="plan",
                          code="orientation_unknown", details={"role": role, "expected": expected_side})


def segment_intersects_body(start, end, bbox, terminals=(), *, allowance=TERMINAL_APPROACH_ALLOWANCE, tolerance=1e-9):
    """True when an orthogonal segment enters the body interior beyond a terminal zone."""
    x0, y0 = bbox[0][0] + tolerance, bbox[0][1] + tolerance
    x1, y1 = bbox[1][0] - tolerance, bbox[1][1] - tolerance
    if start[0] == end[0]:
        if not (x0 < start[0] < x1):
            return False
        axis = 1
    elif start[1] == end[1]:
        if not (y0 < start[1] < y1):
            return False
        axis = 0
    else:
        return True
    low, high = sorted((start[axis], end[axis]))
    edge0, edge1 = (y0, y1) if axis == 1 else (x0, x1)
    overlap = (max(low, edge0), min(high, edge1))
    if overlap[0] >= overlap[1]:
        return False
    zones = sorted(terminal[axis] for terminal in terminals
                   if abs(terminal[1 - axis] - start[1 - axis]) <= tolerance and low - tolerance <= terminal[axis] <= high + tolerance)
    lo, hi = overlap
    for center in zones:
        if center - allowance <= lo + tolerance:
            lo = max(lo, center + allowance)
    return hi - lo > tolerance


def validate_role_map(source, roles, pin_map, *, partial=False):
    """Role coverage contract: every role resolves, no instance is claimed twice,
    every mapped pin exists.  ``partial=True`` (group planning) skips the
    full-coverage check; the combiner enforces it across groups."""
    if not isinstance(source, dict) or not isinstance(source.get("instances"), list):
        raise ValidationError("Compose source must be a plan or flat readback", stage="plan", code="invalid_compose_input")
    instances = {instance["name"]: instance for instance in source["instances"]}
    pins = {pin["name"]: pin for pin in source.get("pins", [])}
    assigned = [spec["instance"] for spec in roles.values()]
    if len(set(assigned)) != len(assigned):
        raise ValidationError("Role map assigns one instance to two roles", stage="plan", code="role_conflict",
                              details={"instances": sorted(assigned)})
    unknown = sorted(set(assigned) - set(instances))
    if unknown:
        raise ValidationError("Role map references instances absent from the source", stage="plan", code="role_unassigned",
                              details={"missing": unknown, "available": sorted(instances)})
    if not partial:
        unassigned = sorted(set(instances) - set(assigned))
        if unassigned:
            raise ValidationError("Source contains instances with no confirmed role; refusing to guess", stage="plan",
                                  code="role_unconfirmed", details={"unassigned": unassigned})
    missing_pins = sorted({name for name in pin_map.values()} - set(pins))
    if missing_pins:
        raise ValidationError("Role map references pins absent from the source", stage="plan", code="role_unassigned",
                              details={"missing_pins": missing_pins})
    return instances, pins


def master_key(master):
    return "/".join(master[field] for field in ("library", "cell", "view"))


def require_master(masters, master, *, role=""):
    geometry = masters.get(master_key(master))
    if not isinstance(geometry, dict) or "terminals" not in geometry:
        raise ValidationError("Compose needs complete master geometry", stage="plan", code="master_geometry_missing",
                              details={"master": master_key(master), "role": role})
    return geometry


def body_height(master):
    body = master.get("body_bbox", master["bbox"])
    h = body[1][1] - body[0][1]
    if not math.isfinite(h) or h <= 0:
        raise ValidationError("Cannot derive the symbol body height H", stage="plan", code="master_geometry_missing")
    return h


def check_connection_premises(connections_by_role, premises, *, recipe):
    """Verify declarative connection premises (same_net / distinct nets) against
    the role-resolved terminal connections; failures are recipe_mismatch, never
    silently repaired."""
    def net_of(spec):
        role, terminal = spec
        return connections_by_role.get(role, {}).get(terminal)

    for premise in premises:
        kind = premise[0]
        if kind == "same_net":
            nets = {net_of(spec) for spec in premise[1]}
            if None in nets or len(nets) != 1:
                raise ValidationError("Recipe connection premise is not satisfied", stage="plan", code="recipe_mismatch",
                                      details={"recipe": recipe, "premise": [premise[0], [list(spec) for spec in premise[1]]],
                                               "actual_nets": sorted(str(net_of(spec)) for spec in premise[1])})
        elif kind == "distinct_net":
            nets = [net_of(spec) for spec in premise[1]]
            if None in nets or len(set(nets)) != len(nets):
                raise ValidationError("Recipe connection premise is not satisfied", stage="plan", code="recipe_mismatch",
                                      details={"recipe": recipe, "premise": [premise[0], [list(spec) for spec in premise[1]]],
                                               "actual_nets": sorted(str(net_of(spec)) for spec in premise[1])})
        else:
            raise ValidationError("Unknown premise kind", stage="plan", code="recipe_mismatch",
                                  details={"recipe": recipe, "premise": list(premise)})


def wire_label(net, origin, *, grid=GRID):
    return {"kind": "wire_name", "text": net, "origin": [snap(origin[0], grid), snap(origin[1], grid)],
            "orientation": "R0", "height": 0.0625, "layer": "wire", "purpose": "label",
            "justification": "lowerLeft", "font": "roman", "properties": [], "net": net}


def labeled_stub(net, point, end, *, grid=GRID):
    """A short wire plus its wire_name label at the midpoint (label island)."""
    return ({"net": net, "points": [[snap(point[0], grid), snap(point[1], grid)], [snap(end[0], grid), snap(end[1], grid)]]},
            wire_label(net, ((point[0] + end[0]) / 2, (point[1] + end[1]) / 2), grid=grid))


def assemble_plan(source, roles, pin_map, placements, wires, labels, pin_positions, *, target, grid=GRID,
                  routing_policy=None):
    """Build the validated plan: electrical fields verbatim from the source,
    geometry from the recipe, then body-collision and electrical compile checks."""
    instances = {instance["name"]: instance for instance in source["instances"]}
    pins = {pin["name"]: pin for pin in source.get("pins", [])}
    plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
            "target": {**dict(target), "view": "schematic",
                       "technology": target.get("technology", source.get("target", {}).get("technology", "018um_PDK"))},
            "instances": [{"name": instance["name"], "master": instance["master"],
                           "origin": list(placements[instance["name"]]["origin"]),
                           "orientation": placements[instance["name"]]["orientation"],
                           "parameters": instance.get("parameters", {}), "connections": instance["connections"]}
                          for instance in sorted(instances.values(), key=lambda item: item["name"])],
            "nets": sorted(source.get("nets") or sorted({net for instance in instances.values() for net in instance["connections"].values()})),
            "pins": [{"name": pin_name, "net": pins[pin_name]["net"], "direction": pins[pin_name].get("direction", "input"),
                      "position": [pin_positions[key][0], pin_positions[key][1]]}
                     for key, pin_name in pin_map.items()],
            "wires": wires, "labels": labels}
    if routing_policy:
        plan["routing_policy"] = routing_policy
    if source.get("port_order"):
        plan["port_order"] = [name for name in source["port_order"] if name in set(pin_map.values())]
    plan = validate_plan(plan)

    obstacles = []
    for instance in plan["instances"]:
        geometry = transformed(placements[instance["name"]]["master_geometry"], instance["orientation"])
        origin = instance["origin"]
        bbox = [[geometry["bbox"][0][0] + origin[0], geometry["bbox"][0][1] + origin[1]],
                [geometry["bbox"][1][0] + origin[0], geometry["bbox"][1][1] + origin[1]]]
        terminals = [placements[instance["name"]]["terminals"][terminal] for terminal in instance["connections"]]
        obstacles.append({"name": instance["name"], "bbox": bbox, "terminals": terminals})
    for item in plan["wires"]:
        for start, end in zip(item["points"], item["points"][1:]):
            for obstacle in obstacles:
                if segment_intersects_body(start, end, obstacle["bbox"], obstacle["terminals"]):
                    raise ValidationError("Composed wire crosses a symbol body", stage="plan", code="compose_blocked",
                                          details={"net": item["net"], "wire": [start, end], "instance": obstacle["name"]})
    contacts = [(instance["connections"][terminal], placements[instance["name"]]["terminals"][terminal], instance["name"] + ":" + terminal)
                for instance in plan["instances"] for terminal in instance["connections"]]
    compile_wires(plan, contacts)
    return plan


def layout_metrics(plan, masters):
    """H-normalized geometric metrics for before/after comparison.

    H is the tallest instance body height; wire bends and the longest branch
    are counted on merged polylines (native readbacks split at junctions);
    labels count both plan labels and native wire-name texts.
    """
    bodies = []
    for instance in plan.get("instances", []):
        geometry = masters.get(master_key(instance["master"]))
        if not isinstance(geometry, dict):
            continue
        body = geometry.get("body_bbox", geometry["bbox"])
        corners = [transform_point([x, y], instance["origin"], instance.get("orientation", "R0"))
                   for x in (body[0][0], body[1][0]) for y in (body[0][1], body[1][1])]
        bodies.append([(min(p[0] for p in corners), min(p[1] for p in corners)),
                       (max(p[0] for p in corners), max(p[1] for p in corners))])
    if not bodies:
        raise ValidationError("Metrics need at least one instance with master geometry", stage="plan", code="master_geometry_missing")
    h = max(body[1][1] - body[0][1] for body in bodies)
    west = min(body[0][0] for body in bodies)
    east = max(body[1][0] for body in bodies)
    south = min(body[0][1] for body in bodies)
    north = max(body[1][1] for body in bodies)
    from .style import _merged_polylines

    segments = set()
    for item in plan.get("wires", []):
        for start, end in zip(item["points"], item["points"][1:]):
            segments.add((item["net"], tuple(start), tuple(end)))
    polylines = _merged_polylines(plan.get("wires", []))
    bends = sum(max(0, len(points) - 2) for points in polylines)
    total_length = sum(abs(e[0] - s[0]) + abs(e[1] - s[1]) for _net, s, e in segments)
    longest_branch = max((sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(points, points[1:]))
                          for points in polylines), default=0.0)
    labels = len(plan.get("labels", [])) + sum(1 for text in plan.get("texts", [])
                                               if text.get("layer") == "wire" and text.get("purpose") == "label" and text.get("net"))
    return {"H": h, "core_width_H": round((east - west) / h, 3), "core_height_H": round((north - south) / h, 3),
            "core_bbox": [[west, south], [east, north]], "total_wire_length": round(total_length, 4),
            "wire_segments": len(segments), "longest_branch": round(longest_branch, 4), "bends": bends,
            "pins": len(plan.get("pins", [])), "labels": labels}


def group_interface(plan, masters):
    """Occupancy and external access contract of a composed group.

    occupancy covers transformed body bboxes, wires and labels; each pin is an
    external interface with its net, position and outward lead direction.
    """
    bodies = []
    for instance in plan.get("instances", []):
        geometry = masters.get(master_key(instance["master"]))
        if not isinstance(geometry, dict):
            continue
        body = geometry.get("body_bbox", geometry["bbox"])
        corners = [transform_point([x, y], instance["origin"], instance.get("orientation", "R0"))
                   for x in (body[0][0], body[1][0]) for y in (body[0][1], body[1][1])]
        bodies.append(corners)
    xs = [p[0] for corners in bodies for p in corners] + [p[0] for wire in plan.get("wires", []) for p in wire["points"]]
    ys = [p[1] for corners in bodies for p in corners] + [p[1] for wire in plan.get("wires", []) for p in wire["points"]]
    occupancy = [[min(xs), min(ys)], [max(xs), max(ys)]] if xs and ys else None
    interfaces = []
    for pin in plan.get("pins", []):
        position = [float(pin["position"][0]), float(pin["position"][1])]
        if occupancy is not None:
            (west, south), (east, north) = occupancy
            distances = {"left": abs(position[0] - west), "right": abs(position[0] - east),
                         "down": abs(position[1] - south), "up": abs(position[1] - north)}
            side = min(distances, key=lambda key: (distances[key], ("left", "right", "down", "up").index(key)))
        else:
            side = "unknown"
        interfaces.append({"pin": pin["name"], "net": pin["net"], "position": position,
                           "side": side, "direction": pin.get("direction", "input")})
    return {"occupancy": occupancy, "interfaces": interfaces}


__all__ = ["GRID", "TERMINAL_APPROACH_ALLOWANCE", "assemble_plan", "body_height", "check_connection_premises",
           "group_interface", "labeled_stub", "layout_metrics", "master_key", "pick_orientation", "require_master",
           "segment_intersects_body", "snap", "transformed", "validate_role_map", "wire_label"]
