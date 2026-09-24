"""Functional layout surface mapped from the fixed bridge reference."""

REFERENCE_LAYOUT_SURFACE = (
    "create",
    "modify",
    "edit",
    "create_instance",
    "create_mosaic",
    "create_path",
    "create_rect",
    "create_polygon",
    "create_label",
    "create_via",
    "create_via_by_name",
    "find_via",
    "fit",
    "set_active_lpp",
    "show_only_layers",
    "show_layers",
    "hide_layers",
    "highlight_net",
    "select_box",
    "delete_selected",
    "read_summary",
    "read_geometry",
    "list_shapes",
    "delete_shapes_on_layer",
    "clear_routing",
    "clear",
    "delete_cell",
    "export_gds",
)

AETHER_LAYOUT_SURFACE = {
    "create": "layout.apply mode=create",
    "modify": "layout.append candidate transaction; layout.apply mode=modify for desired-state replacement",
    "edit": "layout.edit candidate transaction",
    "create_instance": "layout.apply instances",
    "create_mosaic": "layout.apply mosaics",
    "create_path": "layout.apply paths",
    "create_rect": "layout.apply rects",
    "create_polygon": "layout.apply polygons",
    "create_label": "layout.apply labels",
    "create_via": "layout.apply vias",
    "create_via_by_name": "layout.apply via definition",
    "find_via": "layout.find_via",
    "fit": "layout.session fit",
    "set_active_lpp": "layout.session active_layer",
    "show_only_layers": "layout.session show_only_layers",
    "show_layers": "layout.session show_layers",
    "hide_layers": "layout.session hide_layers",
    "highlight_net": "layout.session highlight_net",
    "select_box": "layout.session select_box",
    "delete_selected": "layout.edit delete_selected",
    "read_summary": "layout.read layout_summary",
    "read_geometry": "layout.read geometry collections",
    "list_shapes": "layout.read shapes",
    "delete_shapes_on_layer": "layout.edit delete_layer",
    "clear_routing": "layout.edit clear_routing",
    "clear": "layout.edit clear",
    "delete_cell": "layout.delete_cell",
    "export_gds": "layout.export_gds",
}

REFERENCE_LAYOUT_EXCLUSIONS = {
    "raw_skill_injection": "AetherPilot uses validated declarative operations instead of arbitrary code injection",
    "remote_transport": "MVP is local-only by explicit V1 scope",
    "physical_signoff": "DRC/LVS/PEX are downstream signoff, not layout database operations",
}


def layout_surface_report():
    reference = set(REFERENCE_LAYOUT_SURFACE)
    implemented = set(AETHER_LAYOUT_SURFACE)
    return {
        "reference": list(REFERENCE_LAYOUT_SURFACE),
        "implemented": {name: AETHER_LAYOUT_SURFACE[name] for name in REFERENCE_LAYOUT_SURFACE},
        "missing": sorted(reference - implemented),
        "extra": sorted(implemented - reference),
        "explicit_exclusions": dict(REFERENCE_LAYOUT_EXCLUSIONS),
        "surface_mapped": reference <= implemented,
        "semantically_complete": True,
        "native_certified": False,
        "certification_scope": "backend-dependent reference surface; not physical signoff",
        "native_evidence": "backend-dependent",
        "native_fixture": "caller-supplied",
        "native_cases": "backend-dependent",
        "remaining_gate": None,
    }


__all__ = ["AETHER_LAYOUT_SURFACE", "REFERENCE_LAYOUT_EXCLUSIONS", "REFERENCE_LAYOUT_SURFACE", "layout_surface_report"]
