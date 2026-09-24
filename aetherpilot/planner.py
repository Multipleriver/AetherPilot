"""Deterministic orthogonal segments and electrical contact graph checks."""

from __future__ import annotations

from collections import defaultdict

from .errors import ValidationError


def segment_key(net, start, end):
    return (net, *sorted((tuple(start), tuple(end))))


def on_segment(point, start, end):
    return (start[0] == end[0] == point[0] and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])) or (start[1] == end[1] == point[1] and min(start[0], end[0]) <= point[0] <= max(start[0], end[0]))


def intersections(first, second):
    start, end = first
    other_start, other_end = second
    points = {point for point in (start, end) if on_segment(point, other_start, other_end)}
    points.update(point for point in (other_start, other_end) if on_segment(point, start, end))
    if (start[0] == end[0]) != (other_start[0] == other_end[0]):
        crossing = (start[0], other_start[1]) if start[0] == end[0] else (other_start[0], start[1])
        if on_segment(crossing, start, end) and on_segment(crossing, other_start, other_end):
            points.add(crossing)
    return points


def compile_wires(plan, contacts=()):
    routing_policy = plan.get("routing_policy", {}) if isinstance(plan, dict) else {}
    allow_crossings = bool(routing_policy.get("allow_crossings", False)) if isinstance(routing_policy, dict) else False
    allow_label_islands = bool(routing_policy.get("allow_label_islands", False)) if isinstance(routing_policy, dict) else False
    raw = []
    points = defaultdict(set)
    for wire in plan["wires"]:
        for start, end in zip(wire["points"], wire["points"][1:]):
            raw.append(segment_key(wire["net"], start, end))
            points[wire["net"]].update((tuple(start), tuple(end)))
    if len(raw) > 3000:
        raise ValidationError("v1 supports at most 3000 wire segments", stage="plan", code="plan_too_large")
    for index, (net, start, end) in enumerate(raw):
        for other_net, other_start, other_end in raw[index + 1:]:
            crossing = intersections((start, end), (other_start, other_end))
            if crossing and net != other_net and not allow_crossings:
                raise ValidationError("Different nets touch or cross; use explicit noncrossing routing", stage="plan", code="wire_short", details={"nets": [net, other_net]})
            # An approved migration crossover is a continuous segment, not a
            # junction: do not split either net at an interior crossing.
            if net == other_net or not allow_crossings:
                points[net].update(crossing)
    all_contacts = [(pin["net"], tuple(pin["position"]), "pin:" + pin["name"]) for pin in plan["pins"]] + list(contacts)
    for net, point, label in all_contacts:
        points[net].add(tuple(point))
        if not any(wire_net == net and on_segment(tuple(point), start, end) for wire_net, start, end in raw):
            raise ValidationError("Terminal is not on its declared wire", stage="plan", code="unwired_terminal", details={"terminal": label, "net": net})
        if any(wire_net != net and on_segment(tuple(point), start, end) for wire_net, start, end in raw):
            raise ValidationError("Terminal touches a different net", stage="plan", code="terminal_short", details={"terminal": label})
    segments = set()
    for net, start, end in raw:
        splits = sorted(point for point in points[net] if on_segment(point, start, end))
        for first, second in zip(splits, splits[1:]):
            segments.add(segment_key(net, first, second))
    for net in plan["nets"]:
        graph = defaultdict(set)
        for segment_net, start, end in segments:
            if segment_net == net:
                graph[start].add(end)
                graph[end].add(start)
        if not graph:
            raise ValidationError("Every declared net needs physical wiring", stage="plan", code="unwired_net", details={"net": net})
        pending = [next(iter(graph))]
        visited = set()
        while pending:
            point = pending.pop()
            if point not in visited:
                visited.add(point)
                pending.extend(graph[point] - visited)
        if visited != set(graph):
            if not allow_label_islands:
                raise ValidationError("A net has disconnected wire islands", stage="plan", code="disconnected_net", details={"net": net})
            labels = [item for item in plan.get("labels", []) if item.get("kind") == "wire_name" and item.get("net") == net]
            if len(labels) < 2:
                raise ValidationError("Label-island routing requires a wire-name label on each local island", stage="plan", code="unlabeled_net_island", details={"net": net})
    return [{"net": net, "points": [list(start), list(end)]} for net, start, end in sorted(segments)]


def transform_point(point, origin, orientation):
    horizontal, vertical = point
    transformed = {
        "R0": (horizontal, vertical), "R90": (-vertical, horizontal),
        "R180": (-horizontal, -vertical), "R270": (vertical, -horizontal),
        "MX": (horizontal, -vertical), "MY": (-horizontal, vertical),
        "MXR90": (vertical, horizontal), "MYR90": (-vertical, -horizontal),
    }[orientation]
    return tuple(round(component + offset, 9) for component, offset in zip(transformed, origin))
