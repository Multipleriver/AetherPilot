"""Bounded single-plane Manhattan routing with explicit short-circuit rejection."""

from __future__ import annotations

import copy
import heapq
import math
import time
from collections import defaultdict

from .core.contract import digest, point
from .errors import ValidationError
from .placement import topology
from .planner import compile_wires


def route_schematic(source, contacts, obstacles=(), *, margin=16, clearance=0, max_cells=250000, max_expansions=200000, timeout_seconds=10, max_attempts=8):
    plan = copy.deepcopy(source)
    grid = plan["grid"]
    if any(type(value) is not int or value < 0 for value in (margin, clearance, max_cells, max_expansions, max_attempts)) or not 1 <= margin <= 1000 or not 0 <= clearance <= 10 or not 1 <= max_cells <= 1000000 or not 1 <= max_expansions <= 1000000 or not 1 <= max_attempts <= 32 or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise ValidationError("Invalid routing budget", stage="plan", code="invalid_routing_options")
    deadline = time.monotonic() + timeout_seconds
    before = digest(topology(plan))

    def snapped(position):
        return tuple(round(value / grid) for value in point(list(position), grid, "routing contact"))

    terminals = defaultdict(set)
    for net, position, label in contacts:
        terminals[net].add(snapped(position))
    for pin in plan["pins"]:
        terminals[pin["net"]].add(snapped(pin["position"]))
    if set(terminals) != set(plan["nets"]) or any(not positions for positions in terminals.values()):
        raise ValidationError("Every routed net needs actual contacts", stage="plan", code="unwired_net")
    all_points = [position for positions in terminals.values() for position in positions]
    if len(all_points) > 4096 or len(obstacles) > 2000:
        raise ValidationError("Routing contact budget exceeded", stage="plan", code="resource_limit")
    bounds = [[min(position[axis] for position in all_points) - margin, max(position[axis] for position in all_points) + margin] for axis in (0, 1)]
    if (bounds[0][1] - bounds[0][0] + 1) * (bounds[1][1] - bounds[1][0] + 1) > max_cells:
        raise ValidationError("Routing grid exceeds the local cell budget", stage="plan", code="resource_limit")
    occupied = {}
    for net, positions in sorted(terminals.items()):
        for position in positions:
            if position in occupied and occupied[position] != net:
                raise ValidationError("Different nets share a terminal point", stage="plan", code="terminal_short")
            occupied[position] = net
    blocked = set()
    access_owners = {}
    boxes = []
    for obstacle in obstacles:
        if time.monotonic() >= deadline:
            raise ValidationError("Routing geometry exceeded its deadline", stage="plan", code="resource_limit")
        box = obstacle["bbox"]
        if not isinstance(box, (list, tuple)) or len(box) != 2 or any(not isinstance(corner, (list, tuple)) or len(corner) != 2 for corner in box) or any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1000000 for corner in box for value in corner):
            raise ValidationError("Invalid master bounding box", stage="plan", code="invalid_geometry")
        lower = tuple(math.floor(value / grid) for value in box[0])
        upper = tuple(math.ceil(value / grid) for value in box[1])
        if any(lower[axis] > upper[axis] for axis in (0, 1)):
            raise ValidationError("Invalid master bounding box", stage="plan", code="invalid_geometry")
        for previous_lower, previous_upper in boxes:
            if lower[0] < previous_upper[0] and upper[0] > previous_lower[0] and lower[1] < previous_upper[1] and upper[1] > previous_lower[1]:
                raise ValidationError("Placed symbol bodies overlap", stage="plan", code="placement_overlap")
        boxes.append((lower, upper))
        for horizontal in range(max(lower[0] + 1, bounds[0][0]), min(upper[0], bounds[0][1] + 1)):
            for vertical in range(max(lower[1] + 1, bounds[1][0]), min(upper[1], bounds[1][1] + 1)):
                blocked.add((horizontal, vertical))
        for position in all_points:
            if lower[0] < position[0] < upper[0] and lower[1] < position[1] < upper[1]:
                distances = [(position[0] - lower[0], 0, -1), (upper[0] - position[0], 0, 1), (position[1] - lower[1], 1, -1), (upper[1] - position[1], 1, 1)]
                for distance, axis, sign in sorted(distances):
                    corridor = []
                    for step in range(distance + 1):
                        access = list(position)
                        access[axis] += step * sign
                        corridor.append(tuple(access))
                    if any(occupied.get(access) not in {None, occupied[position]} or access_owners.get(access) not in {None, occupied[position]} for access in corridor):
                        continue
                    blocked.difference_update(corridor)
                    access_owners.update({access: occupied[position] for access in corridor})
                    break
    expansions = 0
    total_bends = 0
    wires = []

    def usable(position, net):
        if position in blocked or access_owners.get(position) not in {None, net} or not all(bounds[axis][0] <= position[axis] <= bounds[axis][1] for axis in (0, 1)):
            return False
        for horizontal in range(-clearance, clearance + 1):
            for vertical in range(-clearance, clearance + 1):
                owner = occupied.get((position[0] + horizontal, position[1] + vertical))
                if owner is not None and owner != net:
                    return False
        return True

    def search(start, goal, net):
        nonlocal expansions, total_bends
        pending = [(abs(start[0] - goal[0]) + abs(start[1] - goal[1]), 0, start, -1)]
        costs = {(start, -1): 0}
        parents = {}
        while pending:
            estimate, cost, position, direction = heapq.heappop(pending)
            state = position, direction
            if cost != costs[state]:
                continue
            expansions += 1
            if expansions > max_expansions or time.monotonic() >= deadline:
                raise ValidationError("Routing search exhausted its budget", stage="plan", code="resource_limit")
            if position == goal:
                path = [position]
                while state in parents:
                    previous = parents[state]
                    if previous[1] != -1 and previous[1] != state[1]:
                        total_bends += 1
                    state = previous
                    path.append(state[0])
                return list(reversed(path))
            for next_direction, offset in enumerate(((1, 0), (0, 1), (-1, 0), (0, -1))):
                neighbor = position[0] + offset[0], position[1] + offset[1]
                if not usable(neighbor, net):
                    continue
                next_state = neighbor, next_direction
                next_cost = cost + 10 + (2 if direction not in {-1, next_direction} else 0)
                if next_cost < costs.get(next_state, float("inf")):
                    costs[next_state] = next_cost
                    parents[next_state] = state
                    heuristic = 10 * (abs(neighbor[0] - goal[0]) + abs(neighbor[1] - goal[1]))
                    heapq.heappush(pending, (next_cost + heuristic, next_cost, neighbor, next_direction))
        raise ValidationError("No route found in the bounded routing window", stage="plan", code="routing_failed", details={"net": net, "start": start, "goal": goal, "proof_of_unroutability": False})

    def connect(order):
        for net in order:
            remaining = sorted(terminals[net])
            tree = {remaining.pop(0)}
            if not remaining:
                origin = next(iter(tree))
                candidates = [(origin[0] + 1, origin[1]), (origin[0], origin[1] + 1), (origin[0] - 1, origin[1]), (origin[0], origin[1] - 1)]
                free = next((candidate for candidate in candidates if usable(candidate, net)), None)
                if free is None:
                    raise ValidationError("No room for an explicit single-terminal wire stub", stage="plan", code="routing_failed", details={"net": net, "proof_of_unroutability": False})
                remaining.append(free)
            while remaining:
                if len(remaining) * len(tree) > 1000000:
                    raise ValidationError("Routing connection search exceeded its budget", stage="plan", code="resource_limit")
                distance, start, goal = min((abs(start[0] - goal[0]) + abs(start[1] - goal[1]), start, goal) for start in remaining for goal in tree)
                path = search(start, goal, net)
                vertices = [path[0]]
                for previous, current, following in zip(path, path[1:], path[2:]):
                    if (current[0] - previous[0], current[1] - previous[1]) != (following[0] - current[0], following[1] - current[1]):
                        vertices.append(current)
                if path[-1] != vertices[-1]:
                    vertices.append(path[-1])
                if len(vertices) > 1:
                    wires.append({"net": net, "points": [[round(value * grid, 9) for value in position] for position in vertices]})
                for position in path:
                    occupied[position] = net
                tree.update(path)
                remaining.remove(start)

    initial_occupied = dict(occupied)
    orders = [tuple(sorted(terminals, key=lambda name: (-len(terminals[name]), name))),
              tuple(sorted(terminals, key=lambda name: (len(terminals[name]), name))), tuple(sorted(terminals))]
    attempted = []
    last_error = None
    while orders and len(attempted) < max_attempts:
        order = orders.pop(0)
        if order in attempted:
            continue
        attempted.append(order)
        occupied = dict(initial_occupied)
        wires = []
        total_bends = 0
        try:
            connect(order)
            break
        except ValidationError as error:
            if error.code != "routing_failed":
                raise
            last_error = error
            failed_net = error.details.get("net")
            if failed_net:
                orders.append((failed_net,) + tuple(net for net in order if net != failed_net))
    else:
        raise ValidationError("Bounded deterministic routing orders exhausted", stage="plan", code="routing_failed",
                              details={"attempts": len(attempted), "last_failure": last_error.details if last_error else {}, "proof_of_unroutability": False})
    plan["wires"] = wires
    segments = compile_wires(plan, contacts)
    if digest(topology(plan)) != before:
        raise AssertionError("routing changed topology")
    return {"plan": plan, "segments": segments, "topology_sha256": before,
            "metrics": {"bends": total_bends, "segments": len(segments), "expansions": expansions, "attempts": len(attempted),
                        "wire_length": sum(abs(segment["points"][0][axis] - segment["points"][1][axis]) for segment in segments for axis in (0, 1)),
                        "cross_net_contacts": 0}, "limits": {"max_cells": max_cells, "max_expansions": max_expansions, "timeout_seconds": timeout_seconds, "max_attempts": max_attempts}}
