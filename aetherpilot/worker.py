"""Fixed JSON worker, launched exclusively with the product's bundled pythone."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aetherpilot.artifacts import ArtifactOps, empty_result, file_digest
from aetherpilot.contract import validate_plan
from aetherpilot.errors import BackendUnavailableError, HarnessError, OperationError, PreflightError
from aetherpilot.native import PyAetherAdapter, require
from aetherpilot.readback import make_snapshot
from aetherpilot.snapshots import SnapshotStore
from aetherpilot.native_tools import native_tool_environment


def dispatch(api, request, artifacts, result, *, owned):
    adapter = PyAetherAdapter(api, artifacts, result, owned=owned)
    operation = request["operation"]
    if operation == "preflight":
        result["readback"] = adapter.preflight()
    elif operation == "library.list":
        adapter.preflight()
        result["readback"] = {"libraries": adapter.library.list()}
    elif operation in {"library.get", "library.create", "library.delete", "library.rename", "library.set_technology"}:
        from aetherpilot.library import validate_library_request

        source = validate_library_request(request["source"], operation=operation)
        adapter.preflight()
        adapter.stage("library")
        if operation == "library.get":
            value = adapter.library_metadata.get(source["name"])
        elif operation == "library.create":
            value = adapter.library_metadata.create(source["name"], path=source.get("path"),
                                                     technology_library=source.get("technology_library"))
            result["changes"].append({"type": "library_created", "library": source["name"]})
        elif operation == "library.delete":
            value = adapter.library_metadata.delete(source["name"], approved=source.get("approved", False))
            result["changes"].append({"type": "library_deleted", "library": source["name"]})
        elif operation == "library.rename":
            value = adapter.library_metadata.rename(source["name"], source["new_name"], approved=source.get("approved", False))
            result["changes"].append({"type": "library_renamed", "library": source["name"], "new_name": source["new_name"]})
        else:
            value = adapter.library_metadata.set_technology(source["name"], source["technology_library"], approved=source.get("approved", False))
            result["changes"].append({"type": "library_technology_set", "library": source["name"],
                                       "technology_library": source["technology_library"]})
        result["readback"] = value.to_dict() if hasattr(value, "to_dict") else value
        artifacts.write("library-readback.json", result["readback"])
    elif operation in {"category.list", "category.create", "category.delete", "category.cells", "category.add_cell", "category.remove_cell", "category.rename"}:
        from aetherpilot.library import validate_category_request

        source = validate_category_request(request["source"], operation=operation)
        adapter.preflight()
        adapter.stage("library")
        if operation == "category.list":
            value = {"library": source["library"], "categories": adapter.categories.list(source["library"])}
        elif operation == "category.create":
            value = adapter.categories.create(source["library"], source["category"], source.get("cells", []), approved=source.get("approved", False))
            result["changes"].append({"type": "category_created", "library": source["library"], "category": source["category"]})
        elif operation == "category.delete":
            value = adapter.categories.delete(source["library"], source["category"], approved=source.get("approved", False))
            result["changes"].append({"type": "category_deleted", "library": source["library"], "category": source["category"]})
        elif operation == "category.cells":
            value = {"library": source["library"], "category": source["category"],
                     "cells": adapter.categories.cells(source["library"], source["category"])}
        elif operation == "category.add_cell":
            value = adapter.categories.add(source["library"], source["category"], source["cell"], approved=source.get("approved", False))
            result["changes"].append({"type": "category_cell_added", "library": source["library"],
                                       "category": source["category"], "cell": source["cell"]})
        elif operation == "category.remove_cell":
            value = adapter.categories.remove(source["library"], source["category"], source["cell"], approved=source.get("approved", False))
            result["changes"].append({"type": "category_cell_removed", "library": source["library"],
                                       "category": source["category"], "cell": source["cell"]})
        else:
            value = adapter.categories.rename(source["library"], source["category"], source["new_name"],
                                              approved=source.get("approved", False))
            result["changes"].append({"type": "category_renamed", "library": source["library"],
                                       "category": source["category"], "new_name": source["new_name"]})
        result["readback"] = value
        artifacts.write("category-readback.json", value)
    elif operation.startswith("maestro."):
        from aetherpilot.maestro import validate_maestro_request

        target = request.get("target")
        source = validate_maestro_request(request.get("source", {}), operation=operation)
        if source["action"] not in {"find_open_session", "capabilities"} and (not isinstance(target, dict) or not target.get("library") or not target.get("cell")):
            raise OperationError("Maestro operation requires a state target", stage="plan", code="invalid_maestro_target")
        adapter.preflight()
        adapter.stage("maestro", target=target, action=source["action"])
        options = {**request.get("options", {}), "maestro_artifact_dir": str(artifacts.path / "maestro")}
        requested_action = source.get("requested_action", source["action"])
        if source["action"] == "capabilities":
            value = adapter.maestro.capability_report()
        elif source["action"] == "open":
            if source["persistent"] and owned:
                raise OperationError("Retained sessions require an explicit initialized in-process API", stage="maestro", code="persistent_session_requires_attachment")
            value = adapter.maestro.open_session(target, options=options, persistent=source["persistent"])
        elif source["action"] == "read_state":
            if source.get("session_id"):
                value = adapter.maestro.read_session(target, source["session_id"], options=options)
                value["session_scope"] = "explicit_session_not_closed_by_readback"
            else:
                value = adapter.maestro.read(target, options=request.get("options", {}))
                value["session_scope"] = "operation_local_closed_after_readback"
        elif source["action"] == "close":
            value = adapter.maestro.close_session(target, session_id=source.get("session_id", ""),
                                                  options=request.get("options", {}))
        elif source["action"] == "find_open_session":
            value = adapter.maestro.find_open_session(session_id=source.get("session_id", ""), target=target)
        elif source["action"] == "migrate_setup":
            value = adapter.maestro.migrate_setup(source["source_target"], target, test=source["test"], options=options)
        elif source["action"] == "recover":
            value = adapter.maestro.recover(target)
        elif source["action"] in {"run", "create_netlist", "export_waveform", "read_results", "snapshot_state", "export_output_view", "write_script"}:
            from aetherpilot.maestro_execution import MaestroExecution

            execution = MaestroExecution(adapter.maestro, target, options)
            if source["action"] == "run":
                value = execution.run(test=source.get("test", ""), output_dir=source.get("output_dir"))
            elif source["action"] == "create_netlist":
                value = execution.create_netlist(test=source.get("test", ""), corner=source.get("corner", ""), output_dir=source.get("output_dir"))
            elif source["action"] == "read_results":
                value = execution.read_results(history=source.get("history", ""), test=source.get("test", ""))
            elif source["action"] == "export_waveform":
                value = execution.export_waveform(analysis=source["analysis"], expression=source["expression"], output=source["output"],
                                                  history=source.get("history", ""), test=source.get("test", ""), corner=source.get("corner", ""))
            elif source["action"] == "export_output_view":
                value = execution.export_output_view(output=source["output"], table_view=source["table_view"],
                                                     history=source.get("history", ""), test=source.get("test", ""))
            elif source["action"] == "write_script":
                value = execution.write_script(output=source["output"])
            else:
                value = execution.snapshot()
        else:
            value = adapter.maestro.apply(target, source, options=request.get("options", {}))
            result["changes"].append({"type": "maestro_action", "action": source["action"], "target": target})
        if requested_action != source["action"]:
            value["requested_action"] = requested_action
        result["readback"] = value
        artifacts.write("maestro-readback.json", value)
    elif operation == "pdk.verify":
        from aetherpilot.certification import verify_pdk

        adapter.preflight()
        verify_pdk(adapter, artifacts, result, request["options"])
    elif operation == "layout.read":
        target = request["target"]
        adapter.preflight()
        if target.get("view") != "layout" or not adapter.library.exists(target):
            raise OperationError("Layout target does not exist", stage="read", code="target_missing")
        adapter.stage("read")
        result["readback"] = adapter.layout.read(target, include_properties=request.get("options", {}).get("include_properties", True))
        artifacts.write("layout-readback.json", result["readback"])
    elif operation == "layout.apply":
        from aetherpilot.layout import validate_layout_plan

        plan = validate_layout_plan(request["plan"])
        adapter.preflight(plan)
        adapter.stage("layout")
        result["readback"] = adapter.layout.apply(plan, artifacts, result)
        visibility = plan.get("layer_visibility", {})
        actions = []
        if visibility.get("active"):
            actions.append({"operation": "active_layer", "layer": visibility["active"][0], "purpose": visibility["active"][1]})
        if visibility.get("visible") or visibility.get("only"):
            actions.append({"operation": "show_only_layers" if visibility.get("only") else "show_layers", "layers": visibility.get("visible", [])})
        if visibility.get("hidden"):
            actions.append({"operation": "hide_layers", "layers": visibility["hidden"]})
        if actions:
            artifacts.write("layout-session.json", adapter.layout.session(plan["target"], actions, owned=owned))
    elif operation == "layout.session":
        adapter.preflight()
        adapter.stage("session")
        result["readback"] = adapter.layout.session(request["target"], request["source"]["actions"], owned=owned)
        artifacts.write("layout-session.json", result["readback"])
    elif operation == "layout.export_gds":
        from aetherpilot.gds import export_gds

        adapter.preflight()
        adapter.stage("gds")
        result["readback"] = export_gds(adapter.layout, request["target"], artifacts=artifacts, **request["source"])
        if result["readback"]["warning_count"]:
            result["warnings"].append("Native GDS export emitted warnings; review gds-log-summary.json before downstream use.")
        result["changes"].append({"type": "gds_exported", "sha256": result["readback"]["sha256"]})
    elif operation == "layout.find_via":
        adapter.preflight()
        result["readback"] = adapter.layout.find_via(request["target"], request["source"]["name"])
    elif operation == "symbol.read":
        target = request["target"]
        adapter.preflight()
        if target.get("view") != "symbol" or not adapter.symbol.exists(target):
            raise OperationError("Symbol target does not exist", stage="read", code="target_missing")
        adapter.stage("read")
        result["readback"] = adapter.symbol.read(target)
        artifacts.write("symbol-readback.json", result["readback"])
    elif operation == "symbol.generate":
        from aetherpilot.symbol import validate_symbol_generation

        target = request["target"]
        source = validate_symbol_generation(request["source"])
        adapter.preflight()
        adapter.stage("symbol")
        value = adapter.symbol.generate_from_schematic_isolated(
            {**target, "view": source["symbol_view"]},
            owner=result["operation_id"], report=artifacts.path / "symbol-check-report.txt",
            schematic_view=source["schematic_view"], symbol_view=source["symbol_view"], sides=source["sides"],
            symbol_shape=source["symbol_shape"], pin_space=source["pin_space"], pin_length=source["pin_length"],
            pin_shape=source["pin_shape"], pin_origin=source["pin_origin"], overwrite=source["overwrite"], sort_pins=source["sort_pins"],
        )
        result["readback"] = value
        artifacts.write("symbol-generation.json", value)
        result["changes"].append({"type": "symbol_generated", "action": value.get("action", value.get("candidate", {}).get("action")),
                                   "symbol_view": source["symbol_view"]})
    elif operation == "schematic.export_netlist":
        from aetherpilot.netlist import export_schematic_netlist

        adapter.preflight()
        target = request["target"]
        if target.get("view") != "schematic" or not adapter.library.exists(target):
            raise OperationError("Schematic target does not exist", stage="netlist", code="target_missing")
        adapter.stage("readback")
        actual = adapter.schematic.read(target, None, include_geometry=True, include_properties=True)
        adapter.stage("netlist")
        source = request.get("source", {})
        value = export_schematic_netlist(actual, device_models=source.get("device_models"),
                                         operation_id=artifacts.operation_id, output=source.get("output"), pdk_root=request["options"].get("pdk_root"))
        text = value.pop("text")
        artifacts.write_text("schematic.net", text)
        result["readback"] = value
        result["changes"].append({"type": "netlist_exported", "source_sha256": value["source_sha256"]})
    elif operation == "schematic.export_hierarchical_netlist":
        from aetherpilot.netlist_hierarchy import export_hierarchical_netlist, plans_from_hierarchy_readback

        adapter.preflight()
        target = request["target"]
        if target.get("view") != "schematic" or not adapter.library.exists(target):
            raise OperationError("Schematic target does not exist", stage="netlist", code="target_missing")
        adapter.stage("readback")
        hierarchy = adapter.schematic.read_hierarchy(target, pdk_root=request["options"].get("pdk_root"))
        artifacts.write("hierarchical-readback.json", hierarchy)
        projected = plans_from_hierarchy_readback(hierarchy)
        adapter.stage("netlist")
        source = request.get("source", {})
        value = export_hierarchical_netlist(projected, output=source.get("output"), netlist_format=source.get("netlist_format", "spice"))
        artifacts.write_text("schematic-hierarchical.net", value.pop("text"))
        value["hierarchy_sha256"] = hierarchy.get("design_facts_sha256") or hierarchy.get("source_sha256")
        value["completeness"] = projected["completeness"]
        result["readback"] = value
        result["changes"].append({"type": "hierarchical_netlist_exported", "root": projected.get("root"), "subcircuit_count": value["subcircuit_count"]})
    elif operation == "schematic.export_native_netlist":
        from aetherpilot.netlist_native import export_native_netlist

        adapter.preflight()
        adapter.stage("netlist")
        result["readback"] = export_native_netlist(adapter, request["target"], request.get("source", {}), artifacts, request.get("options", {}))
        result["changes"].append({"type": "native_netlist_exported", "hierarchy_sha256": result["readback"]["hierarchy_sha256"]})
    elif operation == "schematic.import_netlist":
        from aetherpilot.netlist import import_schematic_netlist

        adapter.preflight()
        source = request["source"]
        target = {**request["target"], "view": "schematic", "technology": request["target"].get("technology", "018um_PDK")}
        if adapter.library.exists(target):
            raise OperationError("Netlist import target already exists; choose a new cell", stage="ownership", code="target_exists")
        adapter.stage("netlist")
        imported = import_schematic_netlist(source["text"], target=target,
                                             pdk_root=request["options"].get("pdk_root"),
                                             require_metadata=source["require_metadata"])
        if imported["metadata"] is None:
            from aetherpilot.placement import placed_geometry
            from aetherpilot.routing import route_schematic

            masters = adapter.schematic.master_geometry(imported["plan"])
            contacts, obstacles = placed_geometry(imported["plan"], masters)
            routed = route_schematic(imported["plan"], contacts, obstacles)
            imported["plan"] = routed["plan"]
            imported["planning"] = {"metrics": routed["metrics"], "topology_sha256": routed["topology_sha256"], "source_geometry_preserved": False}
            artifacts.write("master-geometry.json", masters)
        artifacts.write("netlist-import.json", imported)
        adapter.stage("schematic")
        expected_content_sha256 = (imported.get("metadata") or {}).get("content_sha256")
        content_version = (imported.get("metadata") or {}).get("content_version", 1)
        actual = adapter.apply(imported["plan"], expected_content_sha256=expected_content_sha256, expected_content_version=content_version)
        if expected_content_sha256:
            from aetherpilot.contract import digest
            from aetherpilot.schematic_content import schematic_content

            verified_actual = actual if content_version == 2 else {key: value for key, value in actual.items() if key != "port_order"}
            actual_content_sha256 = digest(schematic_content(verified_actual))
            if actual_content_sha256 != expected_content_sha256:
                raise OperationError("Imported schematic content differs from netlist metadata", stage="readback", code="netlist_content_mismatch",
                                     details={"expected": expected_content_sha256, "actual": actual_content_sha256})
        if request["options"].get("screenshot", True):
            adapter.screenshot(target)
        result["readback"] = actual
        result["import"] = {key: value for key, value in imported.items() if key != "plan"}
        result["changes"].append({"type": "netlist_imported", "source_sha256": imported["source_sha256"],
                                   "target": target, "completeness": imported["completeness"]})
    elif operation == "schematic.import_hierarchical_netlist":
        from aetherpilot.netlist_hierarchy import parse_hierarchical_netlist

        adapter.preflight()
        source = request["source"]
        target = {**request["target"], "view": "schematic", "technology": request["target"].get("technology", "018um_PDK")}
        if adapter.library.exists(target):
            raise OperationError("Netlist import target already exists; choose a new cell", stage="ownership", code="target_exists")
        adapter.stage("netlist")
        parsed = parse_hierarchical_netlist(source["text"], target=target, pdk_root=request["options"].get("pdk_root"),
                                             netlist_format=source.get("netlist_format", "auto"))
        # Multi-cell native publication is deliberately not implicit.  Return
        # all validated plans in dependency order and an explicit gate; this
        # prevents a failed child-symbol generation from leaving a half-built
        # library.  A future native transaction can consume this exact IR.
        artifacts.write("hierarchical-netlist-import.json", parsed)
        if source.get("materialize"):
            raise OperationError("Native hierarchical materialization is gated: child symbol generation and atomic multi-cell publication are not certified", stage="netlist", code="native_materialization_unavailable", details={"plans": len(parsed["plans"]), "root": parsed["root"]})
        result["readback"] = parsed
        result["changes"].append({"type": "hierarchical_netlist_parsed", "root": parsed["root"], "plan_count": len(parsed["plans"]),
                                   "native_materialization": parsed["materialization"]})
    elif operation == "schematic.migrate_netlist":
        from aetherpilot.netlist_migration import migrate_netlist, validate_manifest

        adapter.preflight()
        source = request["source"]
        manifest = validate_manifest(source["manifest"])
        target = {**request["target"], "view": "schematic", "technology": "018um_PDK"}
        if adapter.library.exists(target):
            raise OperationError("Netlist migration target already exists; choose a new cell", stage="ownership", code="target_exists")
        adapter.stage("netlist")
        migrated = migrate_netlist(source["source"], target=target, manifest=manifest, approved=source.get("approved", False))
        artifacts.write("netlist-migration.json", {key: value for key, value in migrated.items() if key != "rewritten_netlist"})
        artifacts.write_text("rewritten.net", migrated["rewritten_netlist"])
        if source.get("materialize"):
            from aetherpilot.placement import placed_geometry
            from aetherpilot.routing import route_schematic

            masters = adapter.schematic.master_geometry(migrated["plan"])
            contacts, obstacles = placed_geometry(migrated["plan"], masters)
            routed = route_schematic(migrated["plan"], contacts, obstacles)
            migrated["plan"] = routed["plan"]
            migrated["planning"] = {"metrics": routed["metrics"], "topology_sha256": routed["topology_sha256"], "source_geometry_preserved": False}
            artifacts.write("migration-planning.json", migrated["planning"])
            adapter.stage("schematic")
            actual = adapter.apply(migrated["plan"])
            if request["options"].get("screenshot", True):
                adapter.screenshot(target)
            migrated["readback"] = actual
            migrated["migration"]["target_plan_is_draft"] = False
            migrated["migration"]["materialized"] = True
            result["changes"].append({"type": "netlist_migrated_and_materialized", "target": target})
        else:
            migrated.pop("rewritten_netlist", None)
            migrated["migration"]["materialized"] = False
            result["changes"].append({"type": "netlist_migrated_plan_only", "target": target})
        result["readback"] = migrated
    elif operation in {"cell.inspect", "cell.delete"}:
        from aetherpilot.cells import CellOps

        adapter.preflight()
        cells = CellOps(api)
        result["readback"] = cells.inspect(request["target"]) if operation == "cell.inspect" else cells.delete(request["target"], artifacts=artifacts, result=result, **request["source"])
    elif operation == "layout.delete_cell":
        adapter.preflight()
        result["readback"] = adapter.layout.delete_cell(request["target"], artifacts=artifacts, result=result, **request["source"])
    elif operation in {"change.prepare", "change.publish", "change.recover"}:
        from aetherpilot.transactions import ViewTransactions
        from aetherpilot.schematic_native import SchematicTransactionsBackend

        source = request["source"]
        domain = source["plan"]["target"]["view"] if operation == "change.prepare" else source["domain"]
        backend = adapter.layout if domain == "layout" else SchematicTransactionsBackend(api)
        adapter.preflight(source.get("plan"))
        if operation == "change.recover":
            from aetherpilot.schematic_recovery import SchematicRecovery

            adapter.stage("recovery")
            recovery = SchematicRecovery(backend, Path.cwd(), artifacts, result)
            outcome = recovery.recover(source["publication_operation_id"], approved=source["approved"])
            result["readback"] = {"recovery": outcome, "design": result["readback"]}
            artifacts.write("readback.json", result["readback"])
        else:
            transaction = ViewTransactions(backend, artifacts, result)
            if operation == "change.prepare":
                result["readback"] = transaction.prepare(source["plan"], base_sha256=source.get("base_sha256"))
            else:
                result["readback"] = transaction.publish(**{key: value for key, value in source.items() if key != "domain"})
    elif operation == "simulation.run":
        from aetherpilot.experiments import ExperimentRunner

        adapter.preflight()
        target = request["target"]
        if target["view"] != "schematic" or not adapter.library.exists(target):
            raise OperationError("Simulation requires an existing schematic", stage="netlist", code="target_missing")
        adapter.stage("readback")
        actual = adapter.schematic.read(target)
        hierarchical = any(instance["master"]["library"] != "018um_PDK" or instance.get("num_bits", 1) != 1 for instance in actual["instances"])
        if hierarchical:
            from aetherpilot.netgraph import flatten_hierarchy

            hierarchy = adapter.schematic.read_hierarchy(target, pdk_root=request["options"].get("pdk_root"))
            artifacts.write("simulation-hierarchy.json", hierarchy)
            actual = flatten_hierarchy(hierarchy)
            aliases = actual["net_aliases"]
            setup = request["source"]
            setup["ground"] = aliases.get(setup["ground"], setup["ground"])
            setup["signals"] = [aliases.get(signal, signal) for signal in setup["signals"]]
            for source in setup["sources"]:
                for terminal in ("positive", "negative"):
                    source[terminal] = aliases.get(source[terminal], source[terminal])
        artifacts.write("simulation-readback.json", actual)
        adapter.stage("simulation")
        try:
            result["metrics"] = ExperimentRunner(artifacts, request["options"]).run(request["source"], actual)
        except HarnessError:
            metrics_path = artifacts.path / "experiment" / "metrics.json"
            if metrics_path.is_file():
                result["metrics"] = json.loads(metrics_path.read_text())
            raise
        after = flatten_hierarchy(adapter.schematic.read_hierarchy(target, pdk_root=request["options"].get("pdk_root"))) if hierarchical else adapter.schematic.read(target)
        if after.get("database_sha256") != actual.get("database_sha256") or after.get("source", {}).get("modified") is not False:
            raise OperationError("Source changed during simulation", stage="results", code="external_changes")
        result["readback"] = actual
    elif operation in {"natural-language.plan", "natural-language.apply"}:
        from aetherpilot.natural_language import natural_language_to_plan

        source = request["source"]
        compiled = natural_language_to_plan(source["description"], target=source["target"], grid=source.get("grid", 0.0625))
        constraints = source.get("constraints")
        constraints_supplied = constraints is not None
        if operation == "natural-language.plan" and constraints is None:
            result["readback"] = compiled
        else:
            from aetherpilot.placement import place_schematic, placed_geometry
            from aetherpilot.routing import route_schematic

            target = {**compiled["plan"]["target"], "view": "schematic"}
            if operation == "natural-language.apply":
                if adapter.library.exists(target):
                    raise OperationError("Natural-language target already exists; choose a new cell", stage="ownership", code="target_exists")
                constraints = [] if constraints is None else constraints
            placed = place_schematic(compiled["plan"], constraints) if compiled.get("requires_planning") or constraints_supplied else {"plan": compiled["plan"], "constraints": [], "routing_required": False}
            adapter.preflight(placed["plan"])
            artifacts.write("placed-schematic.json", placed)
            if compiled.get("requires_planning") or (operation == "natural-language.apply" and source.get("routing")):
                masters = adapter.schematic.master_geometry(placed["plan"])
                contacts, obstacles = placed_geometry(placed["plan"], masters)
                artifacts.write("master-geometry.json", masters)
                routed = route_schematic(placed["plan"], contacts, obstacles, **source.get("routing", {}))
            else:
                routed = {"plan": placed["plan"], "metrics": {}, "topology_sha256": None}
            artifacts.write("planned-schematic.json", routed["plan"])
            if operation == "natural-language.apply":
                adapter.stage("schematic")
                actual = adapter.apply(routed["plan"])
                if request["options"].get("screenshot", True):
                    adapter.screenshot(target)
                result["readback"] = {"compiled": {key: value for key, value in compiled.items() if key != "plan"},
                                       "planning": {"constraints": placed["constraints"], "metrics": routed.get("metrics", {}),
                                                    "topology_sha256": routed.get("topology_sha256"), "database_written": True},
                                       "readback": actual}
                result["changes"].append({"type": "natural_language_schematic_created", "target": target})
            else:
                result["readback"] = {**routed, "constraints": placed["constraints"], "database_written": False,
                                       "natural_language": {key: value for key, value in compiled.items() if key != "plan"}}
    elif operation == "schematic.plan":
        from aetherpilot.placement import place_schematic, placed_geometry
        from aetherpilot.routing import route_schematic

        source = request["source"]
        placed = place_schematic(source["plan"], source["constraints"])
        adapter.preflight(placed["plan"])
        masters = adapter.schematic.master_geometry(placed["plan"])
        contacts, obstacles = placed_geometry(placed["plan"], masters)
        artifacts.write("master-geometry.json", masters)
        artifacts.write("placed-schematic.json", placed)
        routed = route_schematic(placed["plan"], contacts, obstacles, **source.get("routing", {}))
        artifacts.write("planned-schematic.json", routed["plan"])
        result["readback"] = {**routed, "constraints": placed["constraints"], "database_written": False}
    elif operation == "schematic.read":
        target = request["target"]
        options = request.get("options", {})
        adapter.preflight()
        if not adapter.library.exists(target):
            raise OperationError("Schematic target does not exist", stage="read", code="target_missing")
        adapter.stage("read")
        if options.get("recursive", True):
            result["readback"] = adapter.schematic.read_hierarchy(
                target,
                max_depth=options.get("max_depth", 20),
                include_geometry=options.get("include_geometry", True),
                include_properties=options.get("include_properties", True),
                configuration=options.get("configuration"),
                pdk_root=options.get("pdk_root"),
            )
        else:
            result["readback"] = adapter.schematic.read(target, None, include_geometry=options.get("include_geometry", True), include_properties=options.get("include_properties", True))
        artifacts.write("readback.json", result["readback"])
    elif operation == "snapshot.create":
        from aetherpilot.snapshot_native import saved_snapshot

        target = request["target"]
        options = request.get("options", {})
        if options.get("source_kind", "saved") not in {"saved", "edited"}:
            raise OperationError("Unknown snapshot source kind", stage="snapshot", code="invalid_snapshot_source")
        if options.get("source_kind") == "edited" and owned:
            raise OperationError("Edited capture must attach to the owning editor process", stage="snapshot", code="edited_capture_requires_attachment")
        adapter.preflight()
        if not adapter.library.exists(target):
            raise OperationError("Snapshot target does not exist", stage="snapshot", code="target_missing")
        adapter.stage("snapshot")
        snapshot = saved_snapshot(adapter, target, options=options, artifacts=artifacts)
        result["readback"] = snapshot
        artifacts.write("snapshot.json", snapshot)
        result["changes"].append({"type": "snapshot_created", "snapshot_id": snapshot["snapshot_id"]})
    elif operation == "schematic.apply":
        plan = validate_plan(request["plan"])
        if plan.get("simulation"):
            from aetherpilot.simulation import SimulationOps

            SimulationOps.preflight(request["options"])
        actual = adapter.apply(plan)
        if request["options"].get("screenshot", True):
            adapter.screenshot(plan["target"])
        if plan.get("simulation"):
            from aetherpilot.simulation import SimulationOps

            adapter.stage("simulation")
            try:
                result["metrics"] = SimulationOps(artifacts, request["options"]).run(plan, actual)
            except HarnessError:
                metrics = artifacts.path / "simulation" / "metrics.json"
                if metrics.is_file():
                    result["metrics"] = json.loads(metrics.read_text())
                raise
            if result["metrics"]["simulator_warnings"]:
                result["warnings"].append("ALPS emitted model/setup warnings; review the private simulator log before engineering use.")
    elif operation in {"symbol.apply", "symbol.append"}:
        from aetherpilot.symbol import validate_symbol_plan
        from aetherpilot.symbol_append import validate_symbol_append

        plan = (validate_symbol_append if operation == "symbol.append" else validate_symbol_plan)(request["plan"])
        adapter.preflight()
        if plan["target"].get("view") != "symbol":
            raise OperationError("Symbol plan target must use the symbol view", stage="plan", code="invalid_symbol_target")
        adapter.stage("symbol")
        method = adapter.symbol.append_isolated if operation == "symbol.append" else adapter.symbol.apply_isolated
        result["readback"] = method(plan, plan["target"], result["operation_id"], artifacts.path / "symbol-check-report.txt")
        artifacts.write("symbol-readback.json", result["readback"])
        result["changes"].append({"type": "symbol_applied", "target": plan["target"]})
    elif operation == "schematic.smoke":
        target = request["target"]
        adapter.preflight()
        if adapter.library.exists(target):
            raise OperationError("Smoke target already exists", stage="ownership", code="target_exists")
        adapter.library.ensure(target)
        adapter.stage("schematic")
        design = require(api.dbNewCV(target["library"], target["cell"], target["view"], "schematic"), "dbNewCV")
        try:
            require(api.dbCheckAndSaveDesign(design, str(artifacts.path / "check-report.txt")), "dbCheckAndSaveDesign")
        finally:
            adapter.schematic.close(design)
        adapter.stage("readback")
        actual = adapter.schematic.read(target, {})
        if actual["instances"] or actual["nets"] or actual["pins"]:
            raise OperationError("Smoke cell is not empty", stage="readback", code="unexpected_content")
        result["readback"] = actual
        result["changes"].append({"type": "empty_schematic_created", "target": target})
        artifacts.write("readback.json", actual)
    else:
        raise OperationError("Unsupported operation", stage="plan", code="unsupported_operation")
    result["ok"] = True
    result["stage"] = "complete"
    return result


def main():
    os.umask(0o077)
    request = json.loads(Path(sys.argv[1]).read_text())
    artifacts = ArtifactOps.existing(request["artifact_dir"], request["operation_id"])
    result = empty_result(request["operation"], request["operation_id"])
    result["stage"] = "preflight"
    try:
        root = os.environ.get("AETHER_ROOT")
        if not root or not Path(sys.executable).resolve().is_relative_to(Path(root).resolve()):
            raise PreflightError("Worker must use the configured product Python", stage="preflight", code="wrong_python")
        if not os.environ.get("LM_LICENSE_FILE"):
            raise PreflightError("License environment is not configured", stage="preflight", code="license_missing")
        artifacts.event("preflight", "initializing")
        os.environ["DISP_REG_IMAGE_NEW"] = "1"
        with native_tool_environment(artifacts.path / "native-tools", ["emy2netlist"]) as native_tools:
            artifacts.write("native-tool-environment.json", {"tools": native_tools, "aether_home": os.environ.get("AETHER_HOME"),
                                                              "path_head": os.environ.get("PATH", "").split(os.pathsep)[:3],
                                                              "pyAether_preloaded": "pyAether" in sys.modules})
            try:
                import pyAether as api
            except Exception as exc:
                raise BackendUnavailableError(details={"backend": "native", "reason": "pyaether_import_failed",
                                                        "exception": type(exc).__name__}) from exc

            artifacts.write("native-tool-environment-after-import.json", {"aether_home": os.environ.get("AETHER_HOME"),
                                                                          "path_head": os.environ.get("PATH", "").split(os.pathsep)[:3],
                                                                          "module": getattr(api, "__file__", None)})
            api.emyInitAether("-adv")
            dispatch(api, request, artifacts, result, owned=True)
    except HarnessError as exc:
        result["stage"] = exc.stage
        result["errors"].append(exc.to_dict())
    except Exception as exc:
        traceback.print_exc()
        category = PreflightError if result["stage"] == "preflight" else OperationError
        result["errors"].append(category("Native exception; consult private logs", stage=result["stage"], code="native_exception", details={"exception": type(exc).__name__}).to_dict())
    artifacts.write("response.json", result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
