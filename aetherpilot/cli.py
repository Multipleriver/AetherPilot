"""Machine-readable CLI with nonzero failure exits."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .artifacts import ArtifactOps, atomic_json, empty_result, redact
from .contract import no_duplicate_keys, validate_plan
from .layout import diff_layout_readbacks, layout_summary, select_layout_objects, validate_layout_plan
from .errors import HarnessError, ValidationError
from .harness import Harness
from .backend import backend_status
from .planner import compile_wires
from .capabilities import capabilities as declared_capabilities


def capabilities(config=None):
    value = declared_capabilities()
    # Static implementation scope and installation-specific backend state are
    # separate facts.  Keeping the latter under its own key lets callers
    # inspect capabilities without accidentally treating native certification
    # claims as an installation check.
    value["backend"] = backend_status(config)
    return value


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Trusted private runtime JSON")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output (always enabled)")
    root = argparse.ArgumentParser(prog="aetherpilot", description=__doc__, parents=[common])
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("preflight", "capabilities", "backend-status", "backend", "libraries", "pdk-catalog", "pdk-verify"):
        commands.add_parser(command, parents=[common])
    library_get = commands.add_parser("library-get", parents=[common])
    library_get.add_argument("name")
    library_create = commands.add_parser("library-create", parents=[common])
    library_create.add_argument("name")
    library_create.add_argument("--path")
    library_create.add_argument("--technology-library")
    library_delete = commands.add_parser("library-delete", parents=[common])
    library_delete.add_argument("name")
    library_delete.add_argument("--approve", action="store_true")
    library_rename = commands.add_parser("library-rename", parents=[common])
    library_rename.add_argument("name")
    library_rename.add_argument("new_name")
    library_rename.add_argument("--approve", action="store_true")
    library_technology = commands.add_parser("library-set-technology", parents=[common])
    library_technology.add_argument("name")
    library_technology.add_argument("technology_library")
    library_technology.add_argument("--approve", action="store_true")
    category_list = commands.add_parser("category-list", parents=[common])
    category_list.add_argument("library")
    category_create = commands.add_parser("category-create", parents=[common])
    category_create.add_argument("library")
    category_create.add_argument("category")
    category_create.add_argument("--cells", type=Path)
    category_create.add_argument("--approve", action="store_true")
    category_delete = commands.add_parser("category-delete", parents=[common])
    category_delete.add_argument("library")
    category_delete.add_argument("category")
    category_delete.add_argument("--approve", action="store_true")
    category_cells = commands.add_parser("category-cells", parents=[common])
    category_cells.add_argument("library")
    category_cells.add_argument("category")
    category_add = commands.add_parser("category-add-cell", parents=[common])
    category_add.add_argument("library")
    category_add.add_argument("category")
    category_add.add_argument("cell")
    category_add.add_argument("--approve", action="store_true")
    category_remove = commands.add_parser("category-remove-cell", parents=[common])
    category_remove.add_argument("library")
    category_remove.add_argument("category")
    category_remove.add_argument("cell")
    category_remove.add_argument("--approve", action="store_true")
    category_rename = commands.add_parser("category-rename", parents=[common])
    category_rename.add_argument("library")
    category_rename.add_argument("category")
    category_rename.add_argument("new_name")
    category_rename.add_argument("--approve", action="store_true")
    schema = commands.add_parser("schema", parents=[common])
    schema.add_argument("kind", choices=("plan", "result"))
    for command in ("run", "validate"):
        child = commands.add_parser(command, parents=[common])
        child.add_argument("plan", type=Path)
    layout_run = commands.add_parser("layout-run", parents=[common])
    layout_run.add_argument("plan", type=Path)
    layout_validate = commands.add_parser("validate-layout", parents=[common])
    layout_validate.add_argument("plan", type=Path)
    symbol_run = commands.add_parser("symbol-run", parents=[common])
    symbol_run.add_argument("plan", type=Path)
    for command in ("symbol-append", "layout-append"):
        append = commands.add_parser(command, parents=[common])
        append.add_argument("plan", type=Path)
        append.add_argument("--base-sha256", required=True)
    symbol_generate = commands.add_parser("symbol-generate", parents=[common])
    symbol_generate.add_argument("--library", required=True)
    symbol_generate.add_argument("--cell", required=True)
    symbol_generate.add_argument("--sides", type=Path)
    symbol_generate.add_argument("--symbol-shape", default="transmission")
    symbol_generate.add_argument("--pin-space", type=float, default=0.125)
    symbol_generate.add_argument("--pin-length", type=float, default=0.25)
    symbol_generate.add_argument("--pin-shape", default="round")
    symbol_generate.add_argument("--pin-origin", default="topLeft")
    symbol_generate.add_argument("--sort-pins", choices=("alphanumeric", "geometric"))
    symbol_generate.add_argument("--overwrite", action="store_true")
    symbol_read = commands.add_parser("symbol-read", parents=[common])
    symbol_read.add_argument("--library", required=True)
    symbol_read.add_argument("--cell", required=True)
    symbol_read.add_argument("--view", default="symbol")
    maestro = commands.add_parser("maestro", parents=[common])
    maestro.add_argument("action")
    maestro.add_argument("--library", required=True)
    maestro.add_argument("--cell", required=True)
    maestro.add_argument("--view", default="state")
    maestro.add_argument("--request", type=Path)
    maestro.add_argument("--output-dir", type=Path)
    maestro_run = commands.add_parser("maestro-run", parents=[common])
    maestro_run.add_argument("--library", required=True)
    maestro_run.add_argument("--cell", required=True)
    maestro_run.add_argument("--view", default="state")
    maestro_run.add_argument("--test", default="")
    maestro_run.add_argument("--output-dir", type=Path)
    commands.add_parser("maestro-capabilities", parents=[common])
    for command in ("layout-read", "layout-session", "gds-export"):
        child = commands.add_parser(command, parents=[common])
        child.add_argument("--library", required=True)
        child.add_argument("--cell", required=True)
        if command == "layout-session":
            child.add_argument("actions", type=Path)
        elif command == "gds-export":
            child.add_argument("--output", type=Path, required=True)
            child.add_argument("--layer-map", type=Path, required=True)
            child.add_argument("--overwrite", action="store_true")
    layout_summary_command = commands.add_parser("layout-summary", parents=[common])
    layout_summary_command.add_argument("readback", type=Path)
    layout_select = commands.add_parser("layout-select", parents=[common])
    layout_select.add_argument("readback", type=Path)
    layout_select.add_argument("--bbox", nargs=4, type=float)
    layout_select.add_argument("--layer")
    layout_select.add_argument("--purpose")
    layout_select.add_argument("--net")
    layout_select.add_argument("--intersects", action="store_true")
    prepare = commands.add_parser("change-prepare", parents=[common])
    prepare.add_argument("plan", type=Path)
    prepare.add_argument("--base-sha256")
    publish = commands.add_parser("change-publish", parents=[common])
    publish.add_argument("candidate_id")
    publish.add_argument("--domain", choices=("layout", "schematic"), required=True)
    publish.add_argument("--candidate-sha256", required=True)
    publish.add_argument("--base-sha256")
    publish.add_argument("--destination", type=Path)
    publish.add_argument("--approve", action="store_true")
    publish.add_argument("--allow-unmanaged", action="store_true")
    recover = commands.add_parser("change-recover", parents=[common])
    recover.add_argument("publication_operation_id")
    recover.add_argument("--domain", choices=("schematic",), default="schematic")
    recover.add_argument("--approve", action="store_true")
    simulate = commands.add_parser("simulate", parents=[common])
    simulate.add_argument("setup", type=Path)
    simulate.add_argument("--library", required=True)
    simulate.add_argument("--cell", required=True)
    validate_analysis = commands.add_parser("validate-analysis", parents=[common])
    validate_analysis.add_argument("setup", type=Path)
    export_netlist = commands.add_parser("netlist-export", parents=[common])
    export_netlist.add_argument("--library", required=True)
    export_netlist.add_argument("--cell", required=True)
    export_netlist.add_argument("--output", type=Path)
    export_native_netlist = commands.add_parser("netlist-export-native", parents=[common])
    export_native_netlist.add_argument("--library", required=True)
    export_native_netlist.add_argument("--cell", required=True)
    export_native_netlist.add_argument("--view", default="schematic")
    export_native_netlist.add_argument("--output-dir", type=Path)
    export_native_netlist.add_argument("--temperature", type=float, default=27)
    export_native_netlist.add_argument("--format", choices=("hspice", "spectre"), default="hspice")
    import_netlist = commands.add_parser("netlist-import", parents=[common])
    import_netlist.add_argument("netlist", type=Path)
    import_netlist.add_argument("--library", required=True)
    import_netlist.add_argument("--cell", required=True)
    import_netlist.add_argument("--require-metadata", action="store_true")
    hierarchical_export = commands.add_parser("netlist-export-hierarchical", parents=[common])
    hierarchical_export.add_argument("--library", required=True)
    hierarchical_export.add_argument("--cell", required=True)
    hierarchical_export.add_argument("--output", type=Path)
    hierarchical_import = commands.add_parser("netlist-import-hierarchical", parents=[common])
    hierarchical_import.add_argument("netlist", type=Path)
    hierarchical_import.add_argument("--library", required=True)
    hierarchical_import.add_argument("--cell", required=True)
    hierarchical_import.add_argument("--format", choices=("auto", "spice", "cdl", "spectre"), default="auto")
    hierarchical_import.add_argument("--materialize", action="store_true", help="request native multi-cell publication (currently gated by capability evidence)")
    migrate_netlist = commands.add_parser("netlist-migrate", parents=[common])
    migrate_netlist.add_argument("netlist", type=Path)
    migrate_netlist.add_argument("manifest", type=Path)
    migrate_netlist.add_argument("--library", required=True)
    migrate_netlist.add_argument("--cell", required=True)
    migrate_netlist.add_argument("--approve", action="store_true")
    migrate_netlist.add_argument("--materialize", action="store_true")
    natural = commands.add_parser("natural-plan", parents=[common])
    natural.add_argument("description", nargs="?")
    natural.add_argument("--description-file", type=Path)
    natural.add_argument("--library", required=True)
    natural.add_argument("--cell", required=True)
    natural.add_argument("--technology", default="018um_PDK")
    natural.add_argument("--constraints", type=Path)
    natural_apply = commands.add_parser("natural-apply", parents=[common])
    natural_apply.add_argument("description", nargs="?")
    natural_apply.add_argument("--description-file", type=Path)
    natural_apply.add_argument("--library", required=True)
    natural_apply.add_argument("--cell", required=True)
    natural_apply.add_argument("--technology", default="018um_PDK")
    natural_apply.add_argument("--constraints", type=Path)
    normalize_waveform = commands.add_parser("waveform-normalize", parents=[common])
    normalize_waveform.add_argument("input", type=Path)
    normalize_waveform.add_argument("--analysis", required=True)
    normalize_waveform.add_argument("--source", type=Path)
    validate_waveform = commands.add_parser("waveform-validate", parents=[common])
    validate_waveform.add_argument("input", type=Path)
    validate_waveform.add_argument("--analysis")
    planning = commands.add_parser("schematic-plan", parents=[common])
    planning.add_argument("plan", type=Path)
    planning.add_argument("constraints", type=Path)
    planning.add_argument("--routing", type=Path)
    via = commands.add_parser("layout-find-via", parents=[common])
    via.add_argument("name")
    via.add_argument("--library", required=True)
    via.add_argument("--cell", required=True)
    delete_cell = commands.add_parser("layout-delete-cell", parents=[common])
    delete_cell.add_argument("baselines", type=Path)
    delete_cell.add_argument("--library", required=True)
    delete_cell.add_argument("--cell", required=True)
    delete_cell.add_argument("--approve", action="store_true")
    cell_inventory = commands.add_parser("cell-inspect", parents=[common])
    cell_inventory.add_argument("--library", required=True)
    cell_inventory.add_argument("--cell", required=True)
    for command in ("read", "snapshot"):
        child = commands.add_parser(command, parents=[common])
        child.add_argument("--library", required=True)
        child.add_argument("--cell", required=True)
        child.add_argument("--view", default="schematic")
        child.add_argument("--max-depth", type=int, default=20)
        child.add_argument("--no-recursive", action="store_true")
        child.add_argument("--view-configuration", type=Path)
    review = commands.add_parser("review", parents=[common], help="read-only structural/operating-point review")
    review.add_argument("--readback", type=Path, help="saved readback/result JSON")
    review.add_argument("--library", help="read a saved schematic from Aether before reviewing")
    review.add_argument("--cell", help="cell used with --library")
    review.add_argument("--view", default="schematic")
    review.add_argument("--max-depth", type=int, default=20)
    review.add_argument("--no-recursive", action="store_true")
    review.add_argument("--op", type=Path, help="immutable .op0 or structured OP JSON")
    review.add_argument("--intent", type=Path, help="explicit design-intent JSON")
    review.add_argument("--netlist", type=Path, help="native netlist anchoring the OP")
    review.add_argument("--output-dir", type=Path, help="optional directory for review.json/review.md")
    diff = commands.add_parser("diff", parents=[common])
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    layout_diff = commands.add_parser("layout-diff", parents=[common])
    layout_diff.add_argument("before", type=Path)
    layout_diff.add_argument("after", type=Path)
    change = commands.add_parser("validate-change", parents=[common])
    change.add_argument("readback", type=Path)
    change.add_argument("change_set", type=Path)
    commands.add_parser("snapshots", parents=[common])
    snapshot_store_inspect = commands.add_parser("snapshot-store-inspect", parents=[common])
    snapshot_store_inspect.add_argument("snapshot_id")
    snapshot_retain = commands.add_parser("snapshot-retain", parents=[common])
    snapshot_retain.add_argument("snapshot_id")
    snapshot_retain.add_argument("reference")
    snapshot_inspect = commands.add_parser("snapshot-inspect", parents=[common])
    snapshot_inspect.add_argument("snapshot", type=Path)
    smoke = commands.add_parser("smoke", parents=[common])
    smoke.add_argument("--lib", required=True)
    smoke.add_argument("--cell", required=True)
    smoke.add_argument("--technology", default="018um_PDK")
    for command in ("inspect", "export-summary"):
        child = commands.add_parser(command, parents=[common])
        child.add_argument("operation_id")
        if command == "export-summary":
            child.add_argument("--output", type=Path, required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "capabilities":
            result = capabilities(getattr(args, "config", None))
        elif args.command in {"backend-status", "backend"}:
            result = backend_status(getattr(args, "config", None))
        elif args.command == "schema":
            result = json.loads((Path(__file__).with_name("schemas") / (args.kind + ".schema.json")).read_text())
        elif args.command == "validate":
            plan = validate_plan(args.plan)
            segments = compile_wires(plan)
            result = {"ok": True, "scope": "static-only; master geometry and OA readback require run", "segments": len(segments)}
        elif args.command == "validate-layout":
            plan = validate_layout_plan(args.plan)
            result = {"ok": True, "scope": "static-only; native layout readback requires layout-run", "counts": {key: len(plan[key]) for key in ("instances", "mosaics", "paths", "rects", "polygons", "labels", "vias", "pins")}}
        elif args.command == "layout-diff":
            result = diff_layout_readbacks(_read_json(args.before), _read_json(args.after))
        elif args.command == "layout-summary":
            result = layout_summary(_read_json(args.readback))
        elif args.command == "layout-select":
            result = {"ok": True, "selected": select_layout_objects(_read_json(args.readback), bbox=args.bbox, layer=args.layer, purpose=args.purpose, net=args.net, inside=not args.intersects)}
        elif args.command == "validate-analysis":
            from .analyses import validate_analysis_request

            result = {"ok": True, "scope": "static-only", "setup": validate_analysis_request(_read_json(args.setup))}
        elif args.command == "waveform-normalize":
            from .results import read_waveform_csv

            result = read_waveform_csv(args.input, analysis=args.analysis,
                                       source=_read_json(args.source) if args.source else None)
        elif args.command == "waveform-validate":
            from .results import validate_waveform_result

            value = _read_json(args.input)
            errors = validate_waveform_result(value, expected_analysis=args.analysis)
            result = {"ok": not errors, "valid": not errors, "errors": errors}
        else:
            harness = Harness(getattr(args, "config", None))
            if args.command == "preflight":
                result = harness.preflight()
            elif args.command == "pdk-catalog":
                result = harness.pdk_catalog()
            elif args.command == "pdk-verify":
                result = harness.verify_pdk()
            elif args.command == "libraries":
                result = harness.libraries()
            elif args.command == "library-get":
                result = harness.library_get(args.name)
            elif args.command == "library-create":
                result = harness.library_create(args.name, path=args.path, technology_library=args.technology_library)
            elif args.command == "library-delete":
                result = harness.library_delete(args.name, approved=args.approve)
            elif args.command == "library-rename":
                result = harness.library_rename(args.name, args.new_name, approved=args.approve)
            elif args.command == "library-set-technology":
                result = harness.library_set_technology(args.name, args.technology_library, approved=args.approve)
            elif args.command == "category-list":
                result = harness.categories(args.library)
            elif args.command == "category-create":
                result = harness.category_create(args.library, args.category, cells=_read_json(args.cells) if args.cells else None, approved=args.approve)
            elif args.command == "category-delete":
                result = harness.category_delete(args.library, args.category, approved=args.approve)
            elif args.command == "category-cells":
                result = harness.category_cells(args.library, args.category)
            elif args.command == "category-add-cell":
                result = harness.category_add_cell(args.library, args.category, args.cell, approved=args.approve)
            elif args.command == "category-remove-cell":
                result = harness.category_remove_cell(args.library, args.category, args.cell, approved=args.approve)
            elif args.command == "category-rename":
                result = harness.category_rename(args.library, args.category, args.new_name, approved=args.approve)
            elif args.command == "run":
                result = harness.run(args.plan)
            elif args.command == "layout-run":
                result = harness.run_layout(args.plan)
            elif args.command == "symbol-run":
                result = harness.run_symbol(args.plan)
            elif args.command == "symbol-append":
                result = harness.append_symbol(args.plan, base_sha256=args.base_sha256)
            elif args.command == "layout-append":
                result = harness.append_layout(args.plan, base_sha256=args.base_sha256)
            elif args.command == "symbol-generate":
                result = harness.generate_symbol({"library": args.library, "cell": args.cell},
                                                  sides=_read_json(args.sides) if args.sides else None,
                                                  symbol_shape=args.symbol_shape, pin_space=args.pin_space,
                                                  pin_length=args.pin_length, pin_shape=args.pin_shape,
                                                  pin_origin=args.pin_origin, overwrite=args.overwrite, sort_pins=args.sort_pins)
            elif args.command == "symbol-read":
                result = harness.read_symbol({"library": args.library, "cell": args.cell, "view": args.view})
            elif args.command == "maestro":
                values = _read_json(args.request) if args.request else {}
                if not isinstance(values, dict):
                    raise ValidationError("Maestro request must be a JSON object", stage="plan", code="invalid_maestro_request")
                values.pop("action", None)
                result = harness.maestro_action(
                    {"library": args.library, "cell": args.cell, "view": args.view}, args.action,
                    runtime_options={"output_dir": str(args.output_dir.absolute()) if args.output_dir else None},
                    **values,
                )
            elif args.command == "maestro-run":
                result = harness.maestro_run({"library": args.library, "cell": args.cell, "view": args.view},
                                             test=args.test, output_dir=args.output_dir)
            elif args.command == "maestro-capabilities":
                result = harness.maestro_capabilities()
            elif args.command == "layout-read":
                result = harness.read_layout({"library": args.library, "cell": args.cell})
            elif args.command == "layout-session":
                result = harness.layout_session({"library": args.library, "cell": args.cell}, _read_json(args.actions))
            elif args.command == "gds-export":
                result = harness.export_gds({"library": args.library, "cell": args.cell}, args.output, args.layer_map, overwrite=args.overwrite)
            elif args.command == "change-prepare":
                result = harness.prepare_change(args.plan, base_sha256=args.base_sha256)
            elif args.command == "change-publish":
                result = harness.publish_change(args.candidate_id, domain=args.domain, approved=args.approve, candidate_sha256=args.candidate_sha256,
                                                base_sha256=args.base_sha256, destination=_read_json(args.destination) if args.destination else None,
                                                allow_unmanaged=args.allow_unmanaged)
            elif args.command == "change-recover":
                result = harness.recover_change(args.publication_operation_id, domain=args.domain, approved=args.approve)
            elif args.command == "simulate":
                result = harness.simulate({"library": args.library, "cell": args.cell}, args.setup)
            elif args.command == "netlist-export":
                result = harness.export_netlist({"library": args.library, "cell": args.cell}, output=args.output)
            elif args.command == "netlist-import":
                result = harness.import_netlist(args.netlist, {"library": args.library, "cell": args.cell}, require_metadata=args.require_metadata)
            elif args.command == "netlist-export-hierarchical":
                result = harness.export_hierarchical_netlist({"library": args.library, "cell": args.cell}, output=args.output)
            elif args.command == "netlist-import-hierarchical":
                result = harness.import_hierarchical_netlist(args.netlist, {"library": args.library, "cell": args.cell}, netlist_format=args.format, materialize=args.materialize)
            elif args.command == "netlist-migrate":
                from .netlist_migration import load_manifest

                result = harness.migrate_netlist(args.netlist, {"library": args.library, "cell": args.cell},
                                                 load_manifest(args.manifest), approved=args.approve, materialize=args.materialize)
            elif args.command == "natural-plan":
                if bool(args.description) == bool(args.description_file):
                    raise ValidationError("Provide exactly one natural-language description source", stage="plan", code="invalid_input")
                description = args.description_file.read_text(encoding="utf-8") if args.description_file else args.description
                constraints = _read_json(args.constraints) if args.constraints else None
                result = harness.natural_language_plan(description, target={"library": args.library, "cell": args.cell, "technology": args.technology}, constraints=constraints)
            elif args.command == "natural-apply":
                if bool(args.description) == bool(args.description_file):
                    raise ValidationError("Provide exactly one natural-language description source", stage="plan", code="invalid_input")
                description = args.description_file.read_text(encoding="utf-8") if args.description_file else args.description
                constraints = _read_json(args.constraints) if args.constraints else None
                result = harness.natural_language_apply(description, target={"library": args.library, "cell": args.cell, "technology": args.technology}, constraints=constraints)
            elif args.command == "netlist-export-native":
                result = harness.export_native_netlist({"library": args.library, "cell": args.cell, "view": args.view}, output_dir=args.output_dir, temperature=args.temperature, netlist_format=args.format)
            elif args.command == "schematic-plan":
                result = harness.plan_schematic(args.plan, _read_json(args.constraints), routing=_read_json(args.routing) if args.routing else None)
            elif args.command == "layout-find-via":
                result = harness.find_layout_via({"library": args.library, "cell": args.cell}, args.name)
            elif args.command == "layout-delete-cell":
                baselines = _read_json(args.baselines)
                result = harness.delete_layout_cell({"library": args.library, "cell": args.cell}, baselines.get("readback", baselines), approved=args.approve)
            elif args.command == "cell-inspect":
                result = harness.inspect_cell({"library": args.library, "cell": args.cell})
            elif args.command == "read":
                result = harness.read({"library": args.library, "cell": args.cell, "view": args.view}, recursive=not args.no_recursive, max_depth=args.max_depth,
                                      configuration=_read_json(args.view_configuration) if args.view_configuration else None)
            elif args.command == "review":
                if args.readback is not None:
                    result = harness.review_files(args.readback, op_path=args.op, intent_path=args.intent, netlist_path=args.netlist)
                elif args.library and args.cell:
                    read_result = harness.read({"library": args.library, "cell": args.cell, "view": args.view}, recursive=not args.no_recursive, max_depth=args.max_depth)
                    if not read_result.get("ok"):
                        result = read_result
                    else:
                        from .reviewer import review_design

                        op_value = None
                        if args.op is not None:
                            try:
                                op_value = _read_json(args.op)
                            except (OSError, ValueError, TypeError):
                                op_value = str(args.op)
                        intent_value = _read_json(args.intent) if args.intent else None
                        result = harness.review(read_result.get("readback", {}), op=op_value, intent=intent_value,
                                                netlist=str(args.netlist) if args.netlist else None)
                else:
                    raise ValidationError("review requires --readback or both --library and --cell", stage="plan", code="invalid_review_input")
                if args.output_dir and result.get("ok"):
                    output = Path(args.output_dir).expanduser().absolute()
                    output.mkdir(parents=True, exist_ok=True)
                    review_value = result.get("readback", {})
                    json_path = output / "review.json"
                    markdown_path = output / "review.md"
                    if json_path.exists() or markdown_path.exists():
                        raise ValidationError("Review output already exists", stage="export", code="target_exists")
                    from .artifacts import atomic_text
                    from .reviewer import render_review_markdown

                    atomic_json(json_path, review_value)
                    atomic_text(markdown_path, render_review_markdown(review_value))
            elif args.command == "snapshot":
                result = harness.snapshot({"library": args.library, "cell": args.cell, "view": args.view}, recursive=not args.no_recursive, max_depth=args.max_depth,
                                          configuration=_read_json(args.view_configuration) if args.view_configuration else None)
            elif args.command == "diff":
                result = harness.diff_operation(_read_json(args.before), _read_json(args.after))
            elif args.command == "validate-change":
                result = harness.validate_change_operation(_read_json(args.readback), _read_json(args.change_set))
            elif args.command == "snapshot-inspect":
                result = harness.inspect_snapshot(_read_json(args.snapshot))
            elif args.command == "snapshots":
                result = harness.list_snapshots()
            elif args.command == "snapshot-store-inspect":
                result = harness.inspect_stored_snapshot(args.snapshot_id)
            elif args.command == "snapshot-retain":
                result = harness.retain_snapshot(args.snapshot_id, args.reference)
            elif args.command == "smoke":
                result = harness.smoke(args.lib, args.cell, args.technology)
            elif args.command == "inspect":
                result = harness.inspect(args.operation_id)
            else:
                result = ArtifactOps.public_summary(ArtifactOps.inspect(harness.config.artifact_root, args.operation_id))
                if args.output.exists():
                    raise ValidationError("Summary destination already exists", stage="export", code="target_exists")
                atomic_json(args.output, result)
    except HarnessError as exc:
        result = empty_result(args.command)
        result["stage"] = exc.stage
        result["errors"] = [exc.to_dict()]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = empty_result(args.command)
        result["errors"] = [ValidationError("Invalid input or inaccessible file", stage="plan", code="invalid_input", details={"exception": type(exc).__name__}).to_dict()]
    output = result if args.command in ("schema", "capabilities") else redact(result)
    print(json.dumps(output, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("ok", True) else 1


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)


if __name__ == "__main__":
    raise SystemExit(main())
