"""Deterministic grid-exact placement constraints without topology synthesis."""

from __future__ import annotations

import copy
from fractions import Fraction

from .core.contract import digest, fields, point, validate_plan
from .errors import ValidationError
from .planner import transform_point


class Equations:
    def __init__(self):
        self.parents = {"constant": "constant"}
        self.signs = {"constant": 1}
        self.offsets = {"constant": Fraction(0)}
        self.fixed = {"constant": Fraction(0)}

    def find(self, name):
        if name not in self.parents:
            self.parents[name], self.signs[name], self.offsets[name] = name, 1, Fraction(0)
        root, sign, offset = name, 1, Fraction(0)
        while self.parents[root] != root:
            offset += sign * self.offsets[root]
            sign *= self.signs[root]
            root = self.parents[root]
        if root != name:
            self.parents[name], self.signs[name], self.offsets[name] = root, sign, offset
        return root, sign, offset

    def add(self, left, right, sign, offset):
        left_root, left_sign, left_offset = self.find(left)
        right_root, right_sign, right_offset = self.find(right)
        relation_sign = sign * right_sign * left_sign
        relation_offset = (sign * right_offset + offset - left_offset) * left_sign
        if left_root == right_root:
            if relation_sign == 1:
                if relation_offset:
                    raise ValueError("inconsistent coordinate equations")
            else:
                self.anchor(left_root, relation_offset / 2)
            return
        left_fixed, right_fixed = self.fixed.get(left_root), self.fixed.get(right_root)
        if left_fixed is not None and right_fixed is not None and left_fixed != relation_sign * right_fixed + relation_offset:
            raise ValueError("conflicting fixed positions")
        self.parents[left_root], self.signs[left_root], self.offsets[left_root] = right_root, relation_sign, relation_offset
        if left_fixed is not None:
            self.anchor(right_root, relation_sign * (left_fixed - relation_offset))
        self.fixed.pop(left_root, None)

    def anchor(self, root, value):
        if root in self.fixed and self.fixed[root] != value:
            raise ValueError("conflicting coordinate anchor")
        self.fixed[root] = value

    def values(self, preferred):
        roots = dict(self.fixed)
        for name in sorted(preferred):
            root, sign, offset = self.find(name)
            roots.setdefault(root, sign * (preferred[name] - offset))
        result = {}
        for name in preferred:
            root, sign, offset = self.find(name)
            value = sign * roots[root] + offset
            if value.denominator != 1:
                raise ValueError("constraints require an off-grid coordinate")
            result[name] = int(value)
        return result


def topology(plan):
    return {**({"port_order": plan["port_order"]} if "port_order" in plan else {}),
            "instances": [{key: item[key] for key in ("name", "master", "parameters", "connections")} for item in plan["instances"]],
            "pins": [{key: item[key] for key in ("name", "net", "direction")} for item in plan["pins"]], "nets": plan["nets"]}


def place_schematic(source, constraints, *, style=None):
    plan = validate_plan(source)
    if style is not None:
        from .style import style_constraints

        constraints = style_constraints(plan, style=style) + list(constraints or [])
    if len(plan["instances"]) + len(plan["pins"]) > 2000 or not isinstance(constraints, list) or len(constraints) > 2000:
        raise ValidationError("Placement exceeds the local constraint budget", stage="plan", code="resource_limit")
    original_topology = digest(topology(plan))
    grid = plan["grid"]
    objects = {"instance:" + item["name"]: (item, "origin") for item in plan["instances"]}
    objects.update({"pin:" + item["name"]: (item, "position") for item in plan["pins"]})
    equations = Equations()
    preferred = {name + ":" + axis: round(item[field][index] / grid) for name, (item, field) in objects.items() for index, axis in enumerate(("x", "y"))}
    decisions = []
    identities = set()

    def coordinate(value):
        return round(point([value, 0], grid, "constraint coordinate")[0] / grid)

    def member(name):
        if not isinstance(name, str) or name not in objects:
            raise ValueError("constraint refers to an unknown instance or pin")
        return name

    def apply(constraint):
        operation = constraint["type"]
        common = {"id", "type", "strength", "priority"}
        specific = {"fixed": {"object", "position"}, "align": {"objects", "axis"},
                    "row": {"objects", "coordinate", "start", "spacing"}, "column": {"objects", "coordinate", "start", "spacing"},
                    "offset": {"first", "second", "delta"}, "pair": {"first", "second", "spacing", "center"},
                    "mirror": {"first", "second", "axis", "coordinate"}, "flow": {"objects", "axis", "spacing"}}
        if operation not in specific or set(constraint) - common - specific[operation]:
            raise ValueError("unknown placement constraint or field")
        if operation == "fixed":
            name = member(constraint["object"])
            position = point(constraint["position"], grid, "fixed position")
            for index, axis in enumerate(("x", "y")):
                equations.add(name + ":" + axis, "constant", 1, coordinate(position[index]))
        elif operation in {"align", "row", "column", "flow"}:
            names = constraint["objects"]
            if not isinstance(names, list) or not 2 <= len(names) <= 2000 or len(set(names)) != len(names):
                raise ValueError("constraint requires distinct ordered objects")
            names = [member(name) for name in names]
            axis = constraint.get("axis", "x" if operation == "row" else "y")
            if axis not in {"x", "y"}:
                raise ValueError("axis must be x or y")
            if operation == "align":
                for name in names[1:]:
                    equations.add(name + ":" + axis, names[0] + ":" + axis, 1, 0)
            else:
                spacing = coordinate(constraint["spacing"])
                if spacing <= 0:
                    raise ValueError("ordered spacing must be positive")
                for index, name in enumerate(names[1:], start=1):
                    equations.add(name + ":" + axis, names[0] + ":" + axis, 1, index * spacing)
                if operation != "flow":
                    cross = "y" if axis == "x" else "x"
                    equations.add(names[0] + ":" + axis, "constant", 1, coordinate(constraint["start"]))
                    for name in names:
                        equations.add(name + ":" + cross, "constant", 1, coordinate(constraint["coordinate"]))
        else:
            first, second = member(constraint["first"]), member(constraint["second"])
            if first == second:
                raise ValueError("paired objects must differ")
            if operation == "offset":
                delta = point(constraint["delta"], grid, "offset")
                for index, axis in enumerate(("x", "y")):
                    equations.add(second + ":" + axis, first + ":" + axis, 1, coordinate(delta[index]))
            elif operation == "pair":
                spacing = coordinate(constraint["spacing"])
                if spacing <= 0:
                    raise ValueError("pair spacing must be positive")
                equations.add(second + ":x", first + ":x", 1, spacing)
                equations.add(second + ":y", first + ":y", 1, 0)
                if "center" in constraint:
                    equations.add(second + ":x", first + ":x", -1, 2 * coordinate(constraint["center"]))
            else:
                axis = constraint["axis"]
                if axis not in {"x", "y"}:
                    raise ValueError("mirror axis must be x or y")
                cross = "y" if axis == "x" else "x"
                equations.add(second + ":" + axis, first + ":" + axis, -1, 2 * coordinate(constraint["coordinate"]))
                equations.add(second + ":" + cross, first + ":" + cross, 1, 0)

    ordered = []
    for constraint in constraints:
        if not isinstance(constraint, dict) or not isinstance(constraint.get("id"), str) or not constraint["id"] or constraint["id"] in identities:
            raise ValidationError("Constraints need unique nonempty IDs", stage="plan", code="invalid_constraints")
        identities.add(constraint["id"])
        strength, priority = constraint.get("strength", "hard"), constraint.get("priority", 0)
        if strength not in {"hard", "soft"} or type(priority) is not int or not -1000 <= priority <= 1000:
            raise ValidationError("Invalid constraint strength or priority", stage="plan", code="invalid_constraints")
        ordered.append((strength != "hard", -priority, constraint["id"], constraint))
    for soft, priority, identity, constraint in sorted(ordered):
        previous = copy.deepcopy(equations)
        try:
            apply(constraint)
            equations.values(preferred)
            decisions.append({"id": identity, "status": "satisfied"})
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            equations = previous
            if not soft:
                raise ValidationError("Hard placement constraint cannot be satisfied", stage="plan", code="constraint_conflict", details={"constraint": identity, "reason": str(exc)}) from exc
            decisions.append({"id": identity, "status": "relaxed", "reason": str(exc)})
    values = equations.values(preferred)
    for name, (item, field) in objects.items():
        item[field] = point([round(values[name + ":" + axis] * grid, 9) for axis in ("x", "y")], grid, "placed position")
    if digest(topology(plan)) != original_topology:
        raise AssertionError("placement changed topology")
    plan["wires"] = []
    return {"plan": plan, "constraints": decisions, "topology_sha256": original_topology,
            "routing_required": True, "semantics": "grid-exact origin constraints; no topology synthesis or implicit orientation changes"}


def placed_geometry(plan, masters):
    contacts = []
    obstacles = []
    for instance in plan["instances"]:
        key = "/".join(instance["master"][field] for field in ("library", "cell", "view"))
        geometry = masters.get(key)
        if not isinstance(geometry, dict) or set(geometry.get("terminals", {})) != set(instance["connections"]):
            raise ValidationError("Placement needs complete master geometry", stage="plan", code="master_geometry_missing", details={"master": key})
        corners = geometry.get("body_bbox", geometry["bbox"])
        transformed = [transform_point([horizontal, vertical], instance["origin"], instance["orientation"]) for horizontal in (corners[0][0], corners[1][0]) for vertical in (corners[0][1], corners[1][1])]
        obstacles.append({"name": instance["name"], "bbox": [[min(point[0] for point in transformed), min(point[1] for point in transformed)], [max(point[0] for point in transformed), max(point[1] for point in transformed)]]})
        for terminal, position in sorted(geometry["terminals"].items()):
            contacts.append((instance["connections"][terminal], transform_point(position, instance["origin"], instance["orientation"]), instance["name"] + ":" + terminal))
    return contacts, obstacles
