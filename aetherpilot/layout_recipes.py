"""Layout recipe library: a small registry of verified structured compositions.

Each recipe is a data structure (roles, pin map, spacing, connection
premises) plus one planning function.  The registry only selects recipes — it
is not an interpreter, and adding a recipe means writing an explicit function
with its own premises.  Recipes never infer electrical semantics from names:
the caller's role map must cover every source instance, premises are verified
against the real terminal connections, and nets are derived from those
connections (never hard-coded), so renamed instances work unchanged.

Supported recipes:
- ``ota5``      five-transistor OTA (differential pair + mirror loads + tail + bias resistor)
- ``inverter``  CMOS inverter (one pull-up + one pull-down, shared input/output)
- ``mirror2``   two-transistor current mirror (diode-connected reference + output device)
- ``tgate``     CMOS transmission gate (one NMOS + one PMOS sharing both channel nets)
"""

from __future__ import annotations

from .errors import ValidationError
from .layout_geometry import (GRID, assemble_plan, body_height, check_connection_premises, group_interface,
                              labeled_stub, pick_orientation, require_master, snap, transformed, validate_role_map,
                              wire_label)


# ---------------------------------------------------------------------------
# recipe planning context
# ---------------------------------------------------------------------------

class _Context:
    def __init__(self, source, masters, roles, pin_map, spacing, *, grid=GRID):
        self.instances, self.pins = validate_role_map(source, roles, pin_map)
        self.masters = masters
        self.roles = roles
        self.pin_map = pin_map
        self.spacing = spacing
        self.grid = grid
        self.diagnostics = []
        self.placements = {}
        self.wires = []
        self.labels = []
        self.pin_positions = {}
        self.routing_policy = None

    def instance(self, role):
        return self.instances[self.roles[role]["instance"]]

    def connections(self, role):
        return self.instance(role)["connections"]

    def net(self, role, terminal):
        return self.connections(role)[terminal]

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
        wire, label = labeled_stub(net, point, end, grid=self.grid)
        self.wires.append(wire)
        self.labels.append(label)

    def set_pin(self, pin_role, position):
        self.pin_positions[pin_role] = (snap(position[0], self.grid), snap(position[1], self.grid))


def _pin_net(ctx, pin_role):
    return ctx.pins[ctx.pin_map[pin_role]]["net"]


def _check_pin_nets(ctx, pin_nets, *, recipe):
    """Each mapped pin must sit on the net implied by its reference terminal."""
    for pin_role, refs in pin_nets.items():
        expected = {ctx.net(role, terminal) for role, terminal in refs}
        actual = _pin_net(ctx, pin_role)
        if len(expected) != 1 or expected.pop() != actual:
            raise ValidationError("Pin does not sit on the net its recipe role requires", stage="plan",
                                  code="recipe_mismatch", details={"recipe": recipe, "pin_role": pin_role,
                                                                   "pin_net": actual})


# ---------------------------------------------------------------------------
# ota5 (the certified reference recipe; geometry frozen by tests/fixtures)
# ---------------------------------------------------------------------------

OTA5_STRUCTURE = {
    "structure": "ota5",
    "roles": {
        "load_left": {"instance": "MP3", "kind": "pmos"},
        "load_right": {"instance": "MP4", "kind": "pmos"},
        "input_left": {"instance": "MN1", "kind": "nmos"},
        "input_right": {"instance": "MN2", "kind": "nmos"},
        "tail": {"instance": "MN5", "kind": "nmos"},
        "bias_device": {"instance": "RMIRROR", "kind": "resistor"},
    },
    "pins": {"input_left": "INP", "input_right": "INN", "output": "OUT", "power": "VDD", "ground": "VSS", "bias": "BIASN"},
    "nets": {"mirror": "MIRROR", "tail": "TAIL"},
    "spacing": {"branch_columns_H": 4.0, "load_row_H": 4.0, "tail_row_H": 2.5, "vdd_rail_H": 1.0},
}

_OTA5_GATE_SIDE = {"load_left": "right", "load_right": "left", "input_left": "left", "input_right": "right", "tail": "left"}


def _compose_ota5(ctx):
    h = ctx.h
    for role, side in _OTA5_GATE_SIDE.items():
        ctx.orientation[role] = pick_orientation(ctx.master(role), side, role=role)
    ctx.orientation["bias_device"] = "R270"
    spacing = ctx.spacing
    column_gap = snap(spacing["branch_columns_H"] * h, ctx.grid)
    load_row = snap(spacing["load_row_H"] * h, ctx.grid)
    tail_row = snap(-spacing["tail_row_H"] * h, ctx.grid)
    vdd_rail = snap(load_row + spacing["vdd_rail_H"] * h, ctx.grid)

    # nets derived from real connections and the pin map, never hard-coded
    vdd_net = _pin_net(ctx, "power")
    vss_net = _pin_net(ctx, "ground")
    mirror_net = ctx.net("load_left", "D")
    out_net = ctx.net("input_right", "D")
    tail_net = ctx.net("tail", "D")
    inp_net = ctx.net("input_left", "G")
    inn_net = ctx.net("input_right", "G")
    bias_net = ctx.net("tail", "G")

    x_left, x_right = 0.0, column_gap
    ctx.place("input_left", ctx.orientation["input_left"], "D", (x_left, snap(0.5 * h, ctx.grid)))
    ctx.place("input_right", ctx.orientation["input_right"], "D", (x_right, snap(0.5 * h, ctx.grid)))
    ctx.place("load_left", ctx.orientation["load_left"], "D", (x_left, snap(load_row - 0.5 * h, ctx.grid)))
    ctx.place("load_right", ctx.orientation["load_right"], "D", (x_right, snap(load_row - 0.5 * h, ctx.grid)))
    x_center = snap((x_left + x_right) / 2, ctx.grid)
    ctx.place("tail", ctx.orientation["tail"], "D", (x_center, snap(tail_row + 0.5 * h, ctx.grid)))
    mirror_column_top = ctx.term("load_left", "D")
    ctx.place("bias_device", ctx.orientation["bias_device"], "P", (snap(x_left - 2 * h, ctx.grid), mirror_column_top[1]))

    tail_bar_y = snap(ctx.term("input_left", "S")[1] - h, ctx.grid)
    out_tap_y = snap((ctx.term("input_right", "D")[1] + ctx.term("load_right", "D")[1]) / 2, ctx.grid)
    bias_y = ctx.term("tail", "G")[1]

    # 1. power rail above the loads with short vertical stubs (S and B merge in)
    for role in ("load_left", "load_right"):
        s, b = ctx.term(role, "S"), ctx.term(role, "B")
        ctx.wire(vdd_net, [s, (s[0], vdd_rail)])
        ctx.wire(vdd_net, [b, s])
    ctx.wire(vdd_net, [(x_left, vdd_rail), (x_right, vdd_rail)])
    # 2. branch drain columns: left carries the mirror node, right the output
    ctx.wire(mirror_net, [ctx.term("input_left", "D"), ctx.term("load_left", "D")])
    ctx.wire(out_net, [ctx.term("input_right", "D"), ctx.term("load_right", "D")])
    # 3. mirror gate line between the inward-facing gates
    g_left, g_right = ctx.term("load_left", "G"), ctx.term("load_right", "G")
    ctx.wire(mirror_net, [g_left, g_right])
    # 4. diode tie from the gate line into the left drain column; the bias
    #    resistor P (top of the vertical device) joins over the top
    tie_x = snap(g_left[0] + 0.5 * h, ctx.grid)
    ctx.wire(mirror_net, [(tie_x, g_left[1]), (tie_x, mirror_column_top[1]), mirror_column_top])
    bias_p = ctx.term("bias_device", "P")
    bias_approach_y = snap(mirror_column_top[1] + 2 * ctx.grid, ctx.grid)
    ctx.wire(mirror_net, [bias_p, (bias_p[0], bias_approach_y), (x_left, bias_approach_y), mirror_column_top])
    # 5. common-source bar with a centered tap into the tail drain
    for role in ("input_left", "input_right"):
        s = ctx.term(role, "S")
        ctx.wire(tail_net, [s, (s[0], tail_bar_y)])
    ctx.wire(tail_net, [(x_left, tail_bar_y), (x_right, tail_bar_y)])
    ctx.wire(tail_net, [(x_center, tail_bar_y), ctx.term("tail", "D")])
    # 6. side inputs straight into the outward gates
    inp_pin = (snap(ctx.term("input_left", "G")[0] - 1.5 * h, ctx.grid), ctx.term("input_left", "G")[1])
    inn_pin = (snap(ctx.term("input_right", "G")[0] + 1.5 * h, ctx.grid), ctx.term("input_right", "G")[1])
    ctx.wire(inp_net, [inp_pin, ctx.term("input_left", "G")])
    ctx.wire(inn_net, [inn_pin, ctx.term("input_right", "G")])
    # 7. output tapped horizontally right from the right drain column
    out_pin = (snap(x_right + 2 * h, ctx.grid), out_tap_y)
    ctx.wire(out_net, [(x_right, out_tap_y), out_pin])
    # 8. tail bias pin near the tail gate
    bias_pin = (snap(ctx.term("tail", "G")[0] - 2 * h, ctx.grid), bias_y)
    ctx.wire(bias_net, [bias_pin, ctx.term("tail", "G")])
    # 9. ground spine for tail S/B and the ground pin; auxiliary terminals
    #    (diff-pair bulks, bias resistor return) become labeled stubs
    tail_s, tail_b = ctx.term("tail", "S"), ctx.term("tail", "B")
    vss_pin = (x_center, snap(tail_s[1] - 1.5 * h, ctx.grid))
    ctx.wire(vss_net, [tail_s, vss_pin])
    ctx.wire(vss_net, [tail_b, tail_s])
    for point in (ctx.term("input_left", "B"), ctx.term("input_right", "B"), ctx.term("bias_device", "N")):
        ctx.stub(vss_net, point, (point[0], snap(point[1] - 0.125, ctx.grid)))
    ctx.routing_policy = {"allow_label_islands": True}

    ctx.set_pin("input_left", inp_pin)
    ctx.set_pin("input_right", inn_pin)
    ctx.set_pin("output", out_pin)
    ctx.set_pin("power", (x_center, vdd_rail))
    ctx.set_pin("ground", vss_pin)
    ctx.set_pin("bias", bias_pin)
    ctx.label(mirror_net, (x_left, snap(h, ctx.grid)))
    ctx.label(tail_net, (snap((x_left + x_center) / 2, ctx.grid), tail_bar_y))


# ---------------------------------------------------------------------------
# inverter
# ---------------------------------------------------------------------------

INVERTER_STRUCTURE = {
    "structure": "inverter",
    "roles": {"pull_up": {"instance": "MP", "kind": "pmos"}, "pull_down": {"instance": "MN", "kind": "nmos"}},
    "pins": {"input": "IN", "output": "OUT", "power": "VDD", "ground": "VSS"},
    "spacing": {"row_gap_H": 3.0, "port_reach_H": 2.0},
}


def _compose_inverter(ctx):
    h = ctx.h
    ctx.orientation["pull_down"] = pick_orientation(ctx.master("pull_down"), "left", role="pull_down")
    ctx.orientation["pull_up"] = pick_orientation(ctx.master("pull_up"), "left", role="pull_up")
    out_net = _pin_net(ctx, "output")
    in_net = _pin_net(ctx, "input")
    vdd_net = _pin_net(ctx, "power")
    vss_net = _pin_net(ctx, "ground")
    row_gap = snap(ctx.spacing["row_gap_H"] * h, ctx.grid)
    reach = snap(ctx.spacing["port_reach_H"] * h, ctx.grid)

    ctx.place("pull_down", ctx.orientation["pull_down"], "D", (0.0, snap(0.5 * h, ctx.grid)))
    up_d_y = snap(ctx.term("pull_down", "D")[1] + row_gap, ctx.grid)
    ctx.place("pull_up", ctx.orientation["pull_up"], "D", (0.0, up_d_y))

    d_low, d_high = ctx.term("pull_down", "D"), ctx.term("pull_up", "D")
    ctx.wire(out_net, [d_low, d_high])
    out_tap = snap((d_low[1] + d_high[1]) / 2, ctx.grid)
    out_pin = (snap(d_low[0] + reach, ctx.grid), out_tap)
    ctx.wire(out_net, [(d_low[0], out_tap), out_pin])
    g_low, g_high = ctx.term("pull_down", "G"), ctx.term("pull_up", "G")
    ctx.wire(in_net, [g_low, g_high])
    in_tap = snap((g_low[1] + g_high[1]) / 2, ctx.grid)
    in_pin = (snap(g_low[0] - reach, ctx.grid), in_tap)
    ctx.wire(in_net, [(g_low[0], in_tap), in_pin])
    # supplies: bulk merges into the source stub, then to the pin
    up_s, up_b = ctx.term("pull_up", "S"), ctx.term("pull_up", "B")
    vdd_pin = (up_s[0], snap(up_s[1] + reach, ctx.grid))
    ctx.wire(vdd_net, [up_s, vdd_pin])
    ctx.wire(vdd_net, [up_b, up_s])
    dn_s, dn_b = ctx.term("pull_down", "S"), ctx.term("pull_down", "B")
    vss_pin = (dn_s[0], snap(dn_s[1] - reach, ctx.grid))
    ctx.wire(vss_net, [dn_s, vss_pin])
    ctx.wire(vss_net, [dn_b, dn_s])

    ctx.set_pin("input", in_pin)
    ctx.set_pin("output", out_pin)
    ctx.set_pin("power", vdd_pin)
    ctx.set_pin("ground", vss_pin)


# ---------------------------------------------------------------------------
# mirror2 (two-transistor current mirror, diode-connected reference)
# ---------------------------------------------------------------------------

MIRROR2_STRUCTURE = {
    "structure": "mirror2",
    "roles": {"reference": {"instance": "MREF", "kind": "nmos"}, "output": {"instance": "MOUT", "kind": "nmos"}},
    "pins": {"reference": "IREF", "output": "IOUT", "ground": "VSS"},
    "spacing": {"column_gap_H": 4.0, "rail_drop_H": 1.5, "port_reach_H": 2.0},
}


def _compose_mirror2(ctx):
    h = ctx.h
    # gates face each other so the control line is a short horizontal
    ctx.orientation["reference"] = pick_orientation(ctx.master("reference"), "right", role="reference")
    ctx.orientation["output"] = pick_orientation(ctx.master("output"), "left", role="output")
    ref_net = ctx.net("reference", "D")
    out_net = ctx.net("output", "D")
    vss_net = _pin_net(ctx, "ground")
    gap = snap(ctx.spacing["column_gap_H"] * h, ctx.grid)
    rail_y = snap(-ctx.spacing["rail_drop_H"] * h, ctx.grid)
    reach = snap(ctx.spacing["port_reach_H"] * h, ctx.grid)

    ctx.place("reference", ctx.orientation["reference"], "D", (0.0, snap(0.5 * h, ctx.grid)))
    ctx.place("output", ctx.orientation["output"], "D", (gap, snap(0.5 * h, ctx.grid)))

    # control line between the inward gates
    ctx.wire(ref_net, [ctx.term("reference", "G"), ctx.term("output", "G")])
    # diode tie on the reference device: D up, over, down into its gate
    d_ref, g_ref = ctx.term("reference", "D"), ctx.term("reference", "G")
    tie_y = snap(d_ref[1] + h, ctx.grid)
    ctx.wire(ref_net, [d_ref, (d_ref[0], tie_y), (g_ref[0], tie_y), g_ref])
    # shared ground rail: sources join, bulks merge into their source stub
    for role in ("reference", "output"):
        s, b = ctx.term(role, "S"), ctx.term(role, "B")
        ctx.wire(vss_net, [s, (s[0], rail_y)])
        ctx.wire(vss_net, [b, s])
    ctx.wire(vss_net, [(0.0, rail_y), (gap, rail_y)])
    vss_pin = (snap(gap / 2, ctx.grid), snap(rail_y - reach, ctx.grid))
    ctx.wire(vss_net, [(snap(gap / 2, ctx.grid), rail_y), vss_pin])
    # ports: reference current in from the left at the tie level, output right
    iref_pin = (snap(d_ref[0] - reach, ctx.grid), tie_y)
    ctx.wire(ref_net, [iref_pin, (d_ref[0], tie_y)])
    iout_pin = (snap(ctx.term("output", "D")[0] + reach, ctx.grid), ctx.term("output", "D")[1])
    ctx.wire(out_net, [ctx.term("output", "D"), iout_pin])

    ctx.set_pin("reference", iref_pin)
    ctx.set_pin("output", iout_pin)
    ctx.set_pin("ground", vss_pin)


# ---------------------------------------------------------------------------
# tgate (CMOS transmission gate)
# ---------------------------------------------------------------------------

TGATE_STRUCTURE = {
    "structure": "tgate",
    "roles": {"n_device": {"instance": "MN", "kind": "nmos"}, "p_device": {"instance": "MP", "kind": "pmos"}},
    "pins": {"channel_in": "IN", "channel_out": "OUT", "control_n": "EN", "control_p": "ENB", "power": "VDD", "ground": "VSS"},
    "spacing": {"column_gap_H": 2.0, "bus_offset_H": 1.5, "port_reach_H": 1.5},
}


def _compose_tgate(ctx):
    h = ctx.h
    # PMOS left with its gate facing left, NMOS right with its gate facing right
    # and its body flipped so both drains land on the bottom side.
    ctx.orientation["p_device"] = pick_orientation(ctx.master("p_device"), "left", role="p_device")
    ctx.orientation["n_device"] = "R180"
    n_geometry = transformed(ctx.master("n_device"), "R180")
    n_gate = n_geometry["terminals"]["G"]
    n_center_x = (n_geometry["bbox"][0][0] + n_geometry["bbox"][1][0]) / 2
    if n_gate[0] <= n_center_x:
        raise ValidationError("No legal orientation makes the gate face the role's side", stage="plan",
                              code="orientation_unknown", details={"role": "n_device", "expected": "right"})
    net_a = ctx.net("n_device", "D")   # both drains share one channel net
    net_b = ctx.net("n_device", "S")   # both sources share the other
    vdd_net = _pin_net(ctx, "power")
    vss_net = _pin_net(ctx, "ground")
    gap = snap(ctx.spacing["column_gap_H"] * h, ctx.grid)
    bus = snap(ctx.spacing["bus_offset_H"] * h, ctx.grid)
    reach = snap(ctx.spacing["port_reach_H"] * h, ctx.grid)

    ctx.place("p_device", ctx.orientation["p_device"], "D", (0.0, snap(-0.5 * h, ctx.grid)))
    ctx.place("n_device", ctx.orientation["n_device"], "D", (gap, snap(-0.5 * h, ctx.grid)))

    # channel net A (drains): bottom bus; channel net B (sources): top bus
    for role in ("p_device", "n_device"):
        d, s = ctx.term(role, "D"), ctx.term(role, "S")
        ctx.wire(net_a, [d, (d[0], -bus)])
        ctx.wire(net_b, [s, (s[0], bus)])
    x_a = [ctx.term(role, "D")[0] for role in ("p_device", "n_device")]
    x_b = [ctx.term(role, "S")[0] for role in ("p_device", "n_device")]
    ctx.wire(net_a, [(min(x_a), -bus), (max(x_a), -bus)])
    ctx.wire(net_b, [(min(x_b), bus), (max(x_b), bus)])
    x_mid = snap((min(x_a) + max(x_a)) / 2, ctx.grid)
    in_pin = (x_mid, snap(-bus - reach, ctx.grid))
    out_pin = (x_mid, snap(bus + reach, ctx.grid))
    ctx.wire(net_a, [(x_mid, -bus), in_pin])
    ctx.wire(net_b, [(x_mid, bus), out_pin])
    # controls leave on the outer sides, horizontally
    enb_pin = (snap(ctx.term("p_device", "G")[0] - reach, ctx.grid), ctx.term("p_device", "G")[1])
    en_pin = (snap(ctx.term("n_device", "G")[0] + reach, ctx.grid), ctx.term("n_device", "G")[1])
    ctx.wire(ctx.net("p_device", "G"), [ctx.term("p_device", "G"), enb_pin])
    ctx.wire(ctx.net("n_device", "G"), [ctx.term("n_device", "G"), en_pin])
    # bulks become labeled stubs; the supply pins are their own labeled islands
    ctx.stub(vdd_net, ctx.term("p_device", "B"), (ctx.term("p_device", "B")[0], snap(ctx.term("p_device", "B")[1] + 2 * ctx.grid, ctx.grid)))
    ctx.stub(vss_net, ctx.term("n_device", "B"), (ctx.term("n_device", "B")[0], snap(ctx.term("n_device", "B")[1] - 2 * ctx.grid, ctx.grid)))
    ctx.routing_policy = {"allow_label_islands": True}
    vdd_pin = (snap(min(x_a) - 2 * reach, ctx.grid), snap(bus + reach, ctx.grid))
    vss_pin = (snap(max(x_a) + 2 * reach, ctx.grid), snap(-bus - reach, ctx.grid))
    ctx.stub(vdd_net, vdd_pin, (snap(vdd_pin[0] - 2 * ctx.grid, ctx.grid), vdd_pin[1]))
    ctx.stub(vss_net, vss_pin, (snap(vss_pin[0] + 2 * ctx.grid, ctx.grid), vss_pin[1]))

    ctx.set_pin("channel_in", in_pin)
    ctx.set_pin("channel_out", out_pin)
    ctx.set_pin("control_n", en_pin)
    ctx.set_pin("control_p", enb_pin)
    ctx.set_pin("power", vdd_pin)
    ctx.set_pin("ground", vss_pin)


# Reference topologies for the recipe library demos, expressed in the generic
# natural-language device grammar.  They are small hand-written reference
# fixtures for recipe verification — not user designs — and each matches its
# recipe's default role map and pin map exactly.
REFERENCE_DESCRIPTIONS = {
    "ota5": "创建一个五管OTA",
    "inverter": "cmos inverter",
    "mirror2": "nmos MREF D=IREF G=IREF S=VSS B=VSS w=1u l=0.18u\n"
               "nmos MOUT D=IOUT G=IREF S=VSS B=VSS w=4u l=0.18u\n"
               "inputs: IREF VSS\noutputs: IOUT",
    "tgate": "nmos MN D=IN S=OUT G=EN B=VSS w=2u l=0.18u\n"
             "pmos MP D=IN S=OUT G=ENB B=VDD w=4u l=0.18u\n"
             "inputs: IN EN ENB VDD VSS\noutputs: OUT",
}


# ---------------------------------------------------------------------------
# registry and engine
# ---------------------------------------------------------------------------

RECIPES = {
    "ota5": {"structure": OTA5_STRUCTURE, "compose": _compose_ota5,
             "requires": [("same_net", [("load_left", "D"), ("load_left", "G"), ("input_left", "D")]),
                          ("same_net", [("load_left", "G"), ("load_right", "G")]),
                          ("same_net", [("input_left", "S"), ("input_right", "S"), ("tail", "D")]),
                          ("same_net", [("load_left", "S"), ("load_left", "B"), ("load_right", "S"), ("load_right", "B")]),
                          ("same_net", [("input_left", "B"), ("input_right", "B"), ("tail", "S"), ("tail", "B")]),
                          ("distinct_net", [("input_left", "G"), ("input_right", "G")]),
                          ("distinct_net", [("load_left", "D"), ("load_right", "D")])],
             "pin_nets": {"power": [("load_left", "S")], "ground": [("tail", "S")], "output": [("input_right", "D")],
                          "input_left": [("input_left", "G")], "input_right": [("input_right", "G")], "bias": [("tail", "G")]}},
    "inverter": {"structure": INVERTER_STRUCTURE, "compose": _compose_inverter,
                 "requires": [("same_net", [("pull_up", "G"), ("pull_down", "G")]),
                              ("same_net", [("pull_up", "D"), ("pull_down", "D")]),
                              ("same_net", [("pull_up", "S"), ("pull_up", "B")]),
                              ("same_net", [("pull_down", "S"), ("pull_down", "B")]),
                              ("distinct_net", [("pull_up", "S"), ("pull_down", "S")]),
                              ("distinct_net", [("pull_up", "G"), ("pull_up", "D")])],
                 "pin_nets": {"input": [("pull_up", "G")], "output": [("pull_up", "D")],
                              "power": [("pull_up", "S")], "ground": [("pull_down", "S")]}},
    "mirror2": {"structure": MIRROR2_STRUCTURE,
                "compose": _compose_mirror2,
                "requires": [("same_net", [("reference", "G"), ("reference", "D"), ("output", "G")]),
                             ("same_net", [("reference", "S"), ("reference", "B"), ("output", "S"), ("output", "B")]),
                             ("distinct_net", [("reference", "D"), ("output", "D")])],
                "pin_nets": {"reference": [("reference", "D")], "output": [("output", "D")],
                             "ground": [("reference", "S")]}},
    "tgate": {"structure": TGATE_STRUCTURE, "compose": _compose_tgate,
              "requires": [("same_net", [("n_device", "D"), ("p_device", "D")]),
                           ("same_net", [("n_device", "S"), ("p_device", "S")]),
                           ("distinct_net", [("n_device", "G"), ("p_device", "G")]),
                           ("distinct_net", [("n_device", "D"), ("n_device", "S")])],
              "pin_nets": {"channel_in": [("n_device", "D")], "channel_out": [("n_device", "S")],
                           "control_n": [("n_device", "G")], "control_p": [("p_device", "G")],
                           "power": [("p_device", "B")], "ground": [("n_device", "B")]},
              # side-by-side is the canonical compact TG form; the NMOS-below-
              # PMOS layering heuristic does not apply and is waived explicitly
              "lint_waivers": ["device_layering"]},
}


def recipe_lint_waivers(recipe):
    spec = RECIPES.get(recipe)
    if spec is None:
        raise ValidationError("Unknown structure recipe", stage="plan", code="unsupported_structure",
                              details={"recipe": recipe, "known": sorted(RECIPES)})
    return list(spec.get("lint_waivers", []))


def compose_structured(source, masters, *, target, recipe="ota5", structure=None, grid=GRID):
    """Compose a recipe-driven plan.  Returns plan + diagnostics + role record +
    group interface; all failures are explicit ValidationErrors."""
    spec = RECIPES.get(recipe)
    if spec is None:
        raise ValidationError("Unknown structure recipe", stage="plan", code="unsupported_structure",
                              details={"recipe": recipe, "known": sorted(RECIPES)})
    merged = dict(spec["structure"] if structure is None else structure)
    roles = merged["roles"]
    ctx = _Context(source, masters, roles, merged["pins"], merged.get("spacing", {}), grid=grid)
    ctx.h = body_height(ctx.master(next(role for role, spec_ in roles.items() if spec_["kind"] == "nmos")))
    ctx.orientation = {}
    connections_by_role = {role: ctx.instance(role)["connections"] for role in roles}
    check_connection_premises(connections_by_role, spec["requires"], recipe=recipe)
    _check_pin_nets(ctx, spec["pin_nets"], recipe=recipe)
    spec["compose"](ctx)
    plan = assemble_plan(source, roles, ctx.pin_map, ctx.placements, ctx.wires, ctx.labels, ctx.pin_positions,
                         target=target, grid=grid, routing_policy=ctx.routing_policy)
    return {"plan": plan, "diagnostics": ctx.diagnostics,
            "roles": {"structure": recipe, "assignments": roles, "pins": ctx.pin_map},
            "H": ctx.h, "interface": group_interface(plan, masters)}


# ---------------------------------------------------------------------------
# ota5 as two composed groups (loads + pair/tail): the interface-composition
# proof for the recipe library.  Group frames are local; the combiner derives
# the relative translation from the link interface geometry (drain columns
# aligned, 3H row gap), emits the link columns and the output tap, and the
# merged geometry matches the monolithic ota5 recipe as sets.
# ---------------------------------------------------------------------------

def _group_ota5_pair_tail(ctx):
    from .layout_geometry import body_height

    h = body_height(ctx.master("input_left"))
    gap = snap(4.0 * h, ctx.grid)
    tail_row = snap(-2.5 * h, ctx.grid)
    ctx.place("input_left", "R0", "D", (0.0, snap(0.5 * h, ctx.grid)))
    ctx.place("input_right", "MY", "D", (gap, snap(0.5 * h, ctx.grid)))
    x_center = snap(gap / 2, ctx.grid)
    ctx.place("tail", "R0", "D", (x_center, snap(tail_row + 0.5 * h, ctx.grid)))
    tail_net = ctx.connections("tail")["D"]
    vss_net = ctx.pins[ctx.pin_map["ground"]]["net"]
    inp_net = ctx.connections("input_left")["G"]
    inn_net = ctx.connections("input_right")["G"]
    bias_net = ctx.connections("tail")["G"]

    tail_bar_y = snap(ctx.term("input_left", "S")[1] - h, ctx.grid)
    for role in ("input_left", "input_right"):
        s = ctx.term(role, "S")
        ctx.wire(tail_net, [s, (s[0], tail_bar_y)])
    ctx.wire(tail_net, [(0.0, tail_bar_y), (gap, tail_bar_y)])
    ctx.wire(tail_net, [(x_center, tail_bar_y), ctx.term("tail", "D")])
    inp_pin = (snap(ctx.term("input_left", "G")[0] - 1.5 * h, ctx.grid), ctx.term("input_left", "G")[1])
    inn_pin = (snap(ctx.term("input_right", "G")[0] + 1.5 * h, ctx.grid), ctx.term("input_right", "G")[1])
    ctx.wire(inp_net, [inp_pin, ctx.term("input_left", "G")])
    ctx.wire(inn_net, [inn_pin, ctx.term("input_right", "G")])
    bias_pin = (snap(ctx.term("tail", "G")[0] - 2 * h, ctx.grid), ctx.term("tail", "G")[1])
    ctx.wire(bias_net, [bias_pin, ctx.term("tail", "G")])
    tail_s, tail_b = ctx.term("tail", "S"), ctx.term("tail", "B")
    vss_pin = (x_center, snap(tail_s[1] - 1.5 * h, ctx.grid))
    ctx.wire(vss_net, [tail_s, vss_pin])
    ctx.wire(vss_net, [tail_b, tail_s])
    for point in (ctx.term("input_left", "B"), ctx.term("input_right", "B")):
        ctx.stub(vss_net, point, (point[0], snap(point[1] - 0.125, ctx.grid)))
    ctx.routing_policy = {"allow_label_islands": True}
    ctx.set_pin("input_left", inp_pin)
    ctx.set_pin("input_right", inn_pin)
    ctx.set_pin("bias", bias_pin)
    ctx.set_pin("ground", vss_pin)
    ctx.label(tail_net, (snap(x_center / 2, ctx.grid), tail_bar_y))


def _group_ota5_loads(ctx):
    from .layout_geometry import body_height, pick_orientation

    h = body_height(ctx.master("load_left"))
    gap = snap(4.0 * h, ctx.grid)
    vdd_net = ctx.pins[ctx.pin_map["power"]]["net"]
    mirror_net = ctx.connections("load_left")["D"]
    orient_left = pick_orientation(ctx.master("load_left"), "right", role="load_left")
    orient_right = pick_orientation(ctx.master("load_right"), "left", role="load_right")
    ctx.place("load_left", orient_left, "D", (0.0, 0.0))
    ctx.place("load_right", orient_right, "D", (gap, 0.0))
    ctx.place("bias_device", "R270", "P", (snap(-2 * h, ctx.grid), 0.0))
    g_left, g_right = ctx.term("load_left", "G"), ctx.term("load_right", "G")
    rail_y = snap(ctx.term("load_left", "S")[1] + 0.5 * h, ctx.grid)
    for role in ("load_left", "load_right"):
        s, b = ctx.term(role, "S"), ctx.term(role, "B")
        ctx.wire(vdd_net, [s, (s[0], rail_y)])
        ctx.wire(vdd_net, [b, s])
    ctx.wire(vdd_net, [(0.0, rail_y), (gap, rail_y)])
    ctx.wire(mirror_net, [g_left, g_right])
    tie_x = snap(g_left[0] + 0.5 * h, ctx.grid)
    d_left = ctx.term("load_left", "D")
    ctx.wire(mirror_net, [(tie_x, g_left[1]), (tie_x, d_left[1]), d_left])
    bias_p = ctx.term("bias_device", "P")
    approach_y = snap(d_left[1] + 2 * ctx.grid, ctx.grid)
    ctx.wire(mirror_net, [bias_p, (bias_p[0], approach_y), (0.0, approach_y), d_left])
    n_return = ctx.term("bias_device", "N")
    vss_net = ctx.connections("bias_device")["N"]
    ctx.stub(vss_net, n_return, (n_return[0], snap(n_return[1] - 0.125, ctx.grid)))
    ctx.routing_policy = {"allow_label_islands": True}
    ctx.set_pin("power", (snap(gap / 2, ctx.grid), rail_y))


def compose_ota5_groups(source, masters, *, target, structure=None, grid=GRID):
    """Compose the certified OTA5 topology as two linked groups (loads +
    pair/tail), proving the group interface path on a known structure."""
    from .layout_groups import compose_groups

    base = dict(OTA5_STRUCTURE if structure is None else structure)
    roles = base["roles"]
    pins = base["pins"]
    instances = {instance["name"]: instance for instance in source["instances"]}
    mirror_net = instances[roles["load_left"]["instance"]]["connections"]["D"]
    out_net = instances[roles["input_right"]["instance"]]["connections"]["D"]
    pair_roles = {key: roles[key] for key in ("input_left", "input_right", "tail")}
    load_roles = {key: roles[key] for key in ("load_left", "load_right", "bias_device")}
    groups = [
        {"name": "pair_tail", "compose": _group_ota5_pair_tail, "roles": pair_roles,
         "pins": {key: pins[key] for key in ("input_left", "input_right", "bias", "ground")}},
        {"name": "loads", "compose": _group_ota5_loads, "roles": load_roles,
         "pins": {"power": pins["power"]}},
    ]
    links = [
        {"net": mirror_net, "from": ("pair_tail", "input_left", "D"), "to": ("loads", "load_left", "D"),
         "axis": "y", "gap_H": 3.0, "label": True, "label_dy_H": 0.5},
        {"net": out_net, "from": ("pair_tail", "input_right", "D"), "to": ("loads", "load_right", "D"),
         "axis": "y", "gap_H": 3.0, "tap": {"pin_role": "output", "pin": pins["output"], "side": "right", "length_H": 2.0}},
    ]
    result = compose_groups(source, masters, target=target, groups=groups, links=links, grid=grid)
    result["roles"] = {"structure": "ota5", "via": "groups", "assignments": roles, "pins": pins}
    result["H"] = None
    from .layout_geometry import group_interface

    result["interface"] = group_interface(result["plan"], masters)
    return result


__all__ = ["INVERTER_STRUCTURE", "MIRROR2_STRUCTURE", "OTA5_STRUCTURE", "RECIPES", "REFERENCE_DESCRIPTIONS",
           "TGATE_STRUCTURE", "compose_ota5_groups", "compose_structured", "recipe_lint_waivers"]
