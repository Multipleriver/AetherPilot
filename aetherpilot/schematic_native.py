"""Schematic backend for the shared candidate/publication protocol."""

from .contract import digest, validate_plan
from .errors import ValidationError
from .native import SchematicOps, ValidationOps, require
from .planner import compile_wires
from .readback import diff_readbacks
from .schematic_content import schematic_content


def _safe_figure_type(figure):
    """Read a figure type without dereferencing an invalid native proxy."""
    try:
        return str(getattr(figure, "objType", "")).casefold()
    except Exception:
        return ""


class SchematicTransactionsBackend(SchematicOps):
    domain = "schematic"
    content = staticmethod(schematic_content)
    diff = staticmethod(diff_readbacks)

    def __init__(self, api):
        from .native import LibraryOps

        super().__init__(api)
        self.library = LibraryOps(api)

    def validate(self, plan):
        if plan.get("operation") != "schematic.edit":
            return validate_plan(plan)
        from .schematic_edit import edit_comparison_content, project_schematic_edit, validate_schematic_edit

        value = validate_schematic_edit({key: item for key, item in plan.items() if not key.startswith("_")})
        current = self.read(value["target"])
        changed_masters = [{"master": change["value"]} for change in value["changes"] if change["type"] == "instance.set_master"]
        masters = self.master_geometry({"instances": changed_masters}) if changed_masters else {}
        expected = project_schematic_edit(current, value, masters)
        value["_expected_content"] = edit_comparison_content(expected, value["changes"])
        return value

    def materialize(self, plan, target, owner, artifacts):
        if plan.get("operation") == "schematic.edit":
            from .schematic_edit import execute_schematic_edits, has_geometry_delta

            self.copy(plan["target"], target)
            design = self.open(target, "a")
            try:
                before_native = self.read(target)
                require(self.api.dbCrtProp(design, "aetherpilot_owner", owner, "string"), "dbCrtProp", "schematic")
                # Detach stale topology before a real geometry edit.  Native
                # dbMoveFig may otherwise carry old wire/junction markers
                # along with the symbol, producing phantom floating-net
                # diagnostics even after a valid reroute.
                geometry_requested = False
                for change in plan["changes"]:
                    if change["type"] not in {"instance.move", "instance.rotate", "instance.set_master", "pin.set"}:
                        continue
                    collection = before_native["pins"] if change["type"] == "pin.set" else before_native["instances"]
                    old = next((item for item in collection if item.get("name") == change["target"]), {})
                    if change["type"] == "instance.move":
                        geometry_requested |= old.get("origin") != change["value"]
                    elif change["type"] == "instance.rotate":
                        geometry_requested |= old.get("orientation") != change["value"]
                    elif change["type"] == "instance.set_master":
                        geometry_requested |= old.get("master") != change["value"]
                    else:
                        geometry_requested |= any(old.get(field) != change["value"].get(field) for field in ("position", "orientation") if field in change["value"])
                if geometry_requested:
                    for figure in list(design.shapes):
                        if _safe_figure_type(figure) in {"line", "ellipse"}:
                            require(self.api.dbDelete(figure), "dbDelete", "schematic")
                execute_schematic_edits(self, design, plan)
                # Moving/rotating/re-mastering a symbol or moving a pin leaves
                # the old wire endpoints stale.  Re-route the complete net
                # graph while the candidate design is open, using the exact
                # native terminal geometry just written above.  This mirrors
                # project_schematic_edit() and keeps native check/save from
                # rejecting an otherwise authorized geometry delta.
                after_native = self.read(target)
                geometry_delta = has_geometry_delta(before_native, after_native, plan["changes"])
                explicit_wire_replacement = any(change["type"] == "wire.replace" for change in plan["changes"])
                if geometry_delta and not explicit_wire_replacement:
                    from .routing import route_schematic

                    actual = self.read(target)
                    contacts = [(item["connections"][terminal], tuple(records[0]["world_center"]), item["name"] + ":" + terminal)
                                for item in actual["instances"]
                                for terminal, records in item.get("terminal_geometry", {}).items()
                                if records and item.get("connections", {}).get(terminal) is not None]
                    route_source = {"grid": plan.get("grid", 0.0625), "instances": actual["instances"], "nets": actual["nets"], "pins": actual["pins"], "wires": []}
                    obstacles = [{"bbox": item["bbox"]} for item in actual["instances"] if item.get("bbox") is not None]
                    routed_result = route_schematic(route_source, contacts, obstacles)
                    routed = routed_result["plan"]["wires"]
                    # Use the compiler's split segments, not just the
                    # multi-point paths: Aether's connectivity checker
                    # requires every tee/junction to be an explicit figure
                    # endpoint.
                    routed_segments = routed_result["segments"]
                    nets = {str(net.name): net for net in design.nets}
                    for figure in list(design.shapes):
                        object_type = _safe_figure_type(figure)
                        if object_type in {"line", "ellipse"}:
                            require(self.api.dbDelete(figure), "dbDelete", "schematic")
                    for wire in routed_segments:
                        for figure in require(self.api.dbCrtSchWire(design, wire["points"]), "dbCrtSchWire", "schematic"):
                            figure.addToNet(nets[wire["net"]])
                            require(self.api.dbAddFigToNet(figure, nets[wire["net"]]), "dbAddFigToNet", "schematic")
                require(self.api.dbCheckAndSaveDesign(design, str(artifacts.path / "check-report.txt")), "dbCheckAndSaveDesign", "check_save")
                if self.api.dbHasMarker(design, "error") or self.api.dbHasMarker(design, "warning"):
                    raise ValidationError("Native edit checker reported a warning or error", stage="check_save", code="native_check_failed")
                require(self.api.dbSaveCV(design), "dbSaveCV", "check_save")
            finally:
                self.close(design)
            return
        contacts = self.master_contacts(plan)
        segments = compile_wires(plan, contacts)
        artifacts.write("candidate-compiled-plan.json", {"contacts": contacts, "segments": segments})
        self.build(plan, target, segments, owner, artifacts.path / "check-report.txt")

    def compare(self, plan, actual):
        if plan.get("operation") == "schematic.edit":
            from .readback import readback_is_complete
            from .schematic_edit import edit_comparison_content

            if not readback_is_complete(actual) or edit_comparison_content(actual, plan["changes"], native=True) != plan["_expected_content"]:
                raise ValidationError("Native schematic edit differs from its authorized projection", stage="readback", code="schematic_edit_mismatch")
            contacts = self.master_contacts({"instances": actual["instances"]})
            compile_wires(actual, contacts)
            return {"ok": True, "checks": ["authorized_fields_only", "unchanged_content", "native_check_save_reopen", "master_terminal_geometry", "physical_connectivity"],
                    "parameter_property_updates": "only explicitly changed parameter names"}
        contacts = self.master_contacts(plan)
        return ValidationOps.compare(plan, actual, compile_wires(plan, contacts))
