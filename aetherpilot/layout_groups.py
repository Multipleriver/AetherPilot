"""Group composition: combine independently planned recipe groups via explicit links.

Each group plans its own instances in a local frame (never frozen absolute
coordinates); the combiner then chooses the groups' relative translation from
the link interface geometry — aligning the linked terminals' columns and
applying the declared H-gap — and only then computes the final coordinates and
the link wires.  Groups never share instances, never create new electrical
nodes, and all links reference real nets from the source connectivity.

Conflicts are diagnosed, never routed through: links demanding inconsistent
translations or overlapping group occupancies fail with ``interface_conflict``
before any plan is produced.
"""

from __future__ import annotations

from .errors import ValidationError
from .layout_geometry import GRID, assemble_plan, body_height, require_master, snap, transformed
from .layout_geometry import validate_role_map, wire_label


class GroupContext:
    """A per-group planning frame: same helpers as a recipe context, local origin."""

    def __init__(self, source, masters, roles, pin_map, *, grid=GRID):
        self.instances, self.pins = validate_role_map(source, roles, pin_map, partial=True)
        self.masters = masters
        self.roles = roles
        self.pin_map = pin_map
        self.grid = grid
        self.placements = {}
        self.wires = []
        self.labels = []
        self.pin_positions = {}
        self.routing_policy = None
        self.diagnostics = []

    def instance(self, role):
        return self.instances[self.roles[role]["instance"]]

    def connections(self, role):
        return self.instance(role)["connections"]

    def master(self, role):
        return require_master(self.masters, self.instance(role)["master"], role=role)

    def place(self, role, orientation, anchor_terminal, anchor_point):
        geometry = transformed(self.master(role), orientation)
        offset = geometry["terminals"][anchor_terminal]
        origin = [snap(anchor_point[0] - offset[0], self.grid), snap(anchor_point[1] - offset[1], self.grid)]
        name = self.instance(role)["name"]
        self.placements[name] = {"origin": origin, "orientation": orientation, "master_geometry": self.master(role),
                                 "terminals": {terminal: (snap(position[0] + origin[0], self.grid), snap(position[1] + origin[1], self.grid))
                                               for terminal, position in geometry["terminals"].items()}}
        return origin

    def term(self, role, terminal):
        return self.placements[self.instance(role)["name"]]["terminals"][terminal]

    def wire(self, net, points):
        self.wires.append({"net": net, "points": [[snap(x, self.grid), snap(y, self.grid)] for x, y in points]})

    def label(self, net, origin):
        self.labels.append(wire_label(net, origin, grid=self.grid))

    def stub(self, net, point, end):
        from .layout_geometry import labeled_stub

        wire, label = labeled_stub(net, point, end, grid=self.grid)
        self.wires.append(wire)
        self.labels.append(label)

    def set_pin(self, pin_role, position):
        self.pin_positions[pin_role] = (snap(position[0], self.grid), snap(position[1], self.grid))


def _translate_group(group, delta):
    dx, dy = delta
    for placement in group.placements.values():
        placement["origin"] = [snap(placement["origin"][0] + dx), snap(placement["origin"][1] + dy)]
        placement["terminals"] = {terminal: (snap(x + dx), snap(y + dy)) for terminal, (x, y) in placement["terminals"].items()}
    for wire in group.wires:
        wire["points"] = [[snap(x + dx), snap(y + dy)] for x, y in wire["points"]]
    for label in group.labels:
        label["origin"] = [snap(label["origin"][0] + dx), snap(label["origin"][1] + dy)]
    group.pin_positions = {key: (snap(x + dx), snap(y + dy)) for key, (x, y) in group.pin_positions.items()}


def _occupancy(group):
    points = [tuple(corner) for placement in group.placements.values() for corner in _body_corners(placement)]
    points += [tuple(point) for wire in group.wires for point in wire["points"]]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _body_corners(placement):
    geometry = transformed(placement["master_geometry"], placement["orientation"])
    origin = placement["origin"]
    return [[geometry["bbox"][0][0] + origin[0], geometry["bbox"][0][1] + origin[1]],
            [geometry["bbox"][1][0] + origin[0], geometry["bbox"][1][1] + origin[1]]]


def compose_groups(source, masters, *, target, groups, links, grid=GRID):
    """Combine group plans into one validated plan.

    ``groups``: [{"name", "compose": callable(GroupContext), "roles": {...}, "pins": {...}}]
    ``links``:  [{"net", "from": (group, role, terminal), "to": (group, role, terminal),
                  "axis": "y"|"x", "gap_H": float, "label": bool}]
    """
    if not groups or len({group["name"] for group in groups}) != len(groups):
        raise ValidationError("Group names must be unique", stage="plan", code="invalid_group_spec")
    contexts = {}
    covered = []
    for group in groups:
        ctx = GroupContext(source, masters, group["roles"], group.get("pins", {}), grid=grid)
        group["compose"](ctx)
        contexts[group["name"]] = ctx
        covered.extend(spec["instance"] for spec in group["roles"].values())
    if len(set(covered)) != len(covered):
        raise ValidationError("One instance is claimed by two groups", stage="plan", code="role_conflict",
                              details={"instances": sorted(covered)})
    source_instances = {instance["name"] for instance in source["instances"]}
    if set(covered) != source_instances:
        raise ValidationError("Groups must cover every source instance exactly once", stage="plan", code="role_unconfirmed",
                              details={"uncovered": sorted(source_instances - set(covered)),
                                       "unknown": sorted(set(covered) - source_instances)})
    source_pins = {pin["name"] for pin in source.get("pins", [])}
    mapped_pins = [name for group in groups for name in group.get("pins", {}).values()]
    link_pins = [link["tap"]["pin"] for link in links if link.get("tap")]
    if set(mapped_pins + link_pins) != source_pins or len(mapped_pins + link_pins) != len(set(mapped_pins + link_pins)):
        raise ValidationError("Groups and link taps must cover every source pin exactly once", stage="plan", code="role_unassigned",
                              details={"uncovered": sorted(source_pins - set(mapped_pins) - set(link_pins)),
                                       "duplicated": sorted({name for name in mapped_pins + link_pins if (mapped_pins + link_pins).count(name) > 1})})

    nmos_masters = [require_master(masters, instance["master"])
                    for instance in source["instances"] if instance["master"]["cell"].startswith("n")]
    h = max((body_height(master) for master in nmos_masters), default=None)
    if h is None:
        raise ValidationError("Cannot derive the symbol body height H", stage="plan", code="master_geometry_missing")

    order = [groups[0]["name"]]
    translations = {groups[0]["name"]: (0.0, 0.0)}
    placements_merged = dict(contexts[groups[0]["name"]].placements)
    wires = list(contexts[groups[0]["name"]].wires)
    labels = list(contexts[groups[0]["name"]].labels)
    pin_positions = dict(contexts[groups[0]["name"]].pin_positions)
    diagnostics = []

    remaining = {group["name"]: group for group in groups[1:]}
    pending_links = list(links)

    def _emit_link(link, from_ctx, to_ctx):
        from_point = from_ctx.placements[from_ctx.instance(link["from"][1])["name"]]["terminals"][link["from"][2]]
        to_point = to_ctx.placements[to_ctx.instance(link["to"][1])["name"]]["terminals"][link["to"][2]]
        axis = link.get("axis", "y")
        misaligned = (from_point[0] != to_point[0]) if axis == "y" else (from_point[1] != to_point[1])
        if misaligned:
            raise ValidationError("Linked terminals are not aligned after translation", stage="plan", code="interface_conflict",
                                  details={"net": link["net"], "from": list(from_point), "to": list(to_point)})
        gap = snap(link["gap_H"] * h, grid)
        actual_gap = abs(to_point[1] - from_point[1]) if axis == "y" else abs(to_point[0] - from_point[0])
        if actual_gap != gap:
            raise ValidationError("Placed groups do not satisfy the declared link gap", stage="plan", code="interface_conflict",
                                  details={"net": link["net"], "declared": gap, "actual": actual_gap})
        wires.append({"net": link["net"], "points": [list(from_point), list(to_point)]})
        if link.get("label"):
            label_dy = snap(link.get("label_dy_H", 1.0) * h, grid)
            labels.append(wire_label(link["net"], (from_point[0], snap(from_point[1] + label_dy, grid)), grid=grid))
        tap = link.get("tap")
        if tap:
            if axis != "y":
                raise ValidationError("Link taps currently support vertical links", stage="plan", code="interface_conflict",
                                      details={"link": str(link.get("net"))})
            tap_y = snap((from_point[1] + to_point[1]) / 2, grid)
            length = snap(tap.get("length_H", 2.0) * h, grid)
            sign = 1.0 if tap.get("side", "right") == "right" else -1.0
            tap_pin = (snap(from_point[0] + sign * length, grid), tap_y)
            wires.append({"net": link["net"], "points": [[from_point[0], tap_y], list(tap_pin)]})
            pin_positions[tap["pin_role"]] = tap_pin

    while remaining or pending_links:
        progress = False
        for link in list(pending_links):
            from_group, to_group = link["from"][0], link["to"][0]
            if from_group not in translations:
                continue
            from_ctx = contexts[from_group]
            to_ctx = contexts[to_group]
            if to_group in remaining:
                from_point = from_ctx.placements[from_ctx.instance(link["from"][1])["name"]]["terminals"][link["from"][2]]
                to_local = to_ctx.placements[to_ctx.instance(link["to"][1])["name"]]["terminals"][link["to"][2]]
                gap = snap(link["gap_H"] * h, grid)
                if link.get("axis", "y") == "y":
                    delta = (snap(from_point[0] - to_local[0], grid), snap(from_point[1] + gap - to_local[1], grid))
                else:
                    delta = (snap(from_point[0] + gap - to_local[0], grid), snap(from_point[1] - to_local[1], grid))
                existing = translations.get(to_group)
                if existing is not None and existing != delta:
                    raise ValidationError("Links demand inconsistent group translations", stage="plan", code="interface_conflict",
                                          details={"group": to_group, "first": list(existing), "demanded": list(delta)})
                translations[to_group] = delta
                _translate_group(to_ctx, delta)
                placements_merged.update(to_ctx.placements)
                wires.extend(to_ctx.wires)
                labels.extend(to_ctx.labels)
                pin_positions.update(to_ctx.pin_positions)
                order.append(to_group)
                remaining.pop(to_group)
            _emit_link(link, from_ctx, to_ctx)
            pending_links.remove(link)
            progress = True
        if not progress:
            raise ValidationError("Link graph cannot place every group", stage="plan", code="interface_conflict",
                                  details={"unplaced": sorted(remaining), "pending_links": len(pending_links)})

    seen = {}
    for name in order:
        box = _occupancy(contexts[name])
        if box is None:
            continue
        for other, other_box in seen.items():
            if box[0] < other_box[2] and box[2] > other_box[0] and box[1] < other_box[3] and box[3] > other_box[1]:
                raise ValidationError("Translated group occupancies overlap", stage="plan", code="interface_conflict",
                                      details={"groups": [other, name], "boxes": [list(other_box), list(box)]})
        seen[name] = box

    pin_map = {}
    for group in groups:
        pin_map.update(group.get("pins", {}))
    for link in links:
        if link.get("tap"):
            pin_map[link["tap"]["pin_role"]] = link["tap"]["pin"]
    routing_policy = None
    if any(contexts[name].routing_policy for name in order):
        routing_policy = {"allow_label_islands": True}
    plan = assemble_plan(source, {role: spec for group in groups for role, spec in group["roles"].items()},
                         pin_map, placements_merged, wires, labels, pin_positions,
                         target=target, grid=grid, routing_policy=routing_policy or None)
    return {"plan": plan, "diagnostics": diagnostics, "groups": order, "translations": {name: list(value) for name, value in translations.items()}}


__all__ = ["GroupContext", "compose_groups"]
