"""Public orchestration and the common operation result boundary."""

from __future__ import annotations

import json
from pathlib import Path

from .artifacts import ArtifactOps, empty_result, redact
from .backend import backend_status as describe_backend
from .config import RuntimeConfig
from .contract import identifier, no_duplicate_keys, validate_plan
from .errors import ArtifactError, HarnessError, OperationError, ValidationError
from .planner import compile_wires
from .changes import validate_candidate
from .layout import diff_layout_readbacks, validate_layout_plan
from .readback import diff_readbacks, validate_snapshot
from .snapshots import SnapshotStore
from .pdk import catalog
from .runtime import AetherRuntime, workspace_lock


class Harness:
    def __init__(self, config=None):
        self.config = RuntimeConfig.load(config)

    def backend_status(self):
        """Return optional backend availability without launching Aether."""
        return describe_backend(self.config)

    @property
    def backend(self):
        """Return the selected backend facade without launching Aether."""
        from .backend import get_backend

        return get_backend(self.config)

    def run(self, plan):
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("schematic.apply", source=plan)

    def run_layout(self, plan):
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("layout.apply", source=plan)

    def run_symbol(self, plan):
        """Apply a declarative symbol plan through the native database."""
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("symbol.apply", source=plan)

    def append_symbol(self, plan, *, base_sha256):
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("symbol.append", source={**plan, "operation": "symbol.append", "base_sha256": base_sha256})

    def generate_symbol(self, target, *, schematic_view="schematic", symbol_view="symbol", sides=None,
                        symbol_shape="transmission", pin_space=0.125, pin_length=0.25,
                        pin_shape="round", pin_origin="topLeft", overwrite=False, sort_pins=None):
        return self._execute("symbol.generate", target={**target, "view": symbol_view}, source={
            "schematic_view": schematic_view, "symbol_view": symbol_view, "sides": sides,
            "symbol_shape": symbol_shape, "pin_space": pin_space, "pin_length": pin_length,
            "pin_shape": pin_shape, "pin_origin": pin_origin, "overwrite": overwrite, "sort_pins": sort_pins,
        })

    def read_symbol(self, target):
        return self._execute("symbol.read", target={**target, "view": target.get("view", "symbol")})

    def maestro_action(self, target, action, *, runtime_options=None, attached_api=None, **values):
        """Execute one validated local Maestro-compatible action."""
        source = {"action": action, **values}
        return self._execute("maestro." + action, target={**target, "view": target.get("view", "state")},
                             source=source, options=runtime_options, attached_api=attached_api)

    def maestro_read(self, target, *, options=None, session_id="", attached_api=None):
        return self.maestro_action(target, "read_state", runtime_options=options, session_id=session_id, attached_api=attached_api)

    def maestro_open_session(self, target, *, options=None, persistent=False, attached_api=None):
        return self.maestro_action(target, "open_session", runtime_options=options, persistent=persistent, attached_api=attached_api)

    def maestro_close_session(self, target, *, session_id="", options=None, attached_api=None):
        values = {"session_id": session_id} if session_id else {}
        return self.maestro_action(target, "close_session", runtime_options=options, attached_api=attached_api, **values)

    def maestro_find_open_session(self, target=None, *, session_id="", options=None, attached_api=None):
        return self._execute("maestro.find_open_session", target=target,
                             source={"action": "find_open_session", "session_id": session_id}, options=options, attached_api=attached_api)

    def maestro_capabilities(self, target=None, *, options=None, attached_api=None):
        """Discover the runtime MDE class/writer matrix without mutating state."""
        return self._execute("maestro.capabilities", target=target, source={"action": "capabilities"},
                             options=options, attached_api=attached_api)

    def maestro_save_setup(self, target, *, options=None):
        return self.maestro_action(target, "save_setup", runtime_options=options)

    def maestro_create_test(self, target, test, *, library, cell, view="schematic", simulator="spectre", options=None):
        return self.maestro_action(target, "create_test", test=test, design_library=library, design_cell=cell,
                                   design_view=view, simulator=simulator, runtime_options=options)

    def maestro_set_design(self, target, test, *, library, cell, view="schematic", options=None):
        return self.maestro_action(target, "set_design", test=test, design_library=library, design_cell=cell,
                                   design_view=view, runtime_options=options)

    def maestro_run(self, target, *, test="", output_dir=None, options=None):
        values = {"test": test}
        if output_dir is not None:
            values["output_dir"] = str(Path(output_dir).expanduser().absolute())
        return self.maestro_action(target, "run", runtime_options=options, **values)

    def maestro_create_netlist(self, target, *, test="", corner="", output_dir=None, options=None):
        values = {"test": test, "corner": corner}
        if output_dir is not None:
            values["output_dir"] = str(Path(output_dir).expanduser().absolute())
        return self.maestro_action(target, "create_netlist", runtime_options=options, **values)

    def maestro_read_results(self, target, *, history="", test="", options=None):
        return self.maestro_action(target, "read_results", runtime_options=options, history=history, test=test)

    def maestro_export_waveform(self, target, *, analysis, expression, output, history="", test="", corner="", options=None):
        return self.maestro_action(target, "export_waveform", runtime_options=options, analysis=analysis,
                                   expression=expression, output=str(Path(output).expanduser().absolute()),
                                   history=history, test=test, corner=corner)

    def maestro_snapshot(self, target, *, options=None):
        return self.maestro_action(target, "snapshot_state", runtime_options=options)

    def maestro_export_output_view(self, target, *, output, table_view="detail", history="", test="", options=None):
        return self.maestro_action(target, "export_output_view", runtime_options=options, output=str(output),
                                   table_view=table_view, history=history, test=test)

    def maestro_write_script(self, target, *, output, options=None):
        return self.maestro_action(target, "write_script", runtime_options=options, output=str(output))

    def maestro_recover(self, target, *, options=None):
        return self.maestro_action(target, "recover", runtime_options=options)

    def maestro_job_status(self, job_id, *, options=None):
        return self._execute("maestro.job_status", source={"action": "job_status", "job_id": job_id}, options=options)

    def maestro_job_wait(self, job_id, *, timeout=600, options=None):
        return self._execute("maestro.job_wait", source={"action": "job_wait", "job_id": job_id, "timeout": timeout}, options=options)

    def maestro_job_cancel(self, job_id, *, options=None):
        return self._execute("maestro.job_cancel", source={"action": "job_cancel", "job_id": job_id}, options=options)

    def maestro_migrate_setup(self, source_target, target, *, test="imported", options=None):
        return self.maestro_action(target, "migrate_setup", source_target={**source_target, "view": source_target.get("view", "state")}, test=test, runtime_options=options)

    def plan_schematic(self, plan, constraints, *, routing=None):
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("schematic.plan", source={"plan": plan, "constraints": constraints, "routing": routing or {}})

    def natural_language_plan(self, description, *, target, grid=0.0625, constraints=None, routing=None):
        """Compile bounded natural language, then optionally run native planning.

        The compiler is itself an operation so its request, diagnostics and
        deterministic IR are recorded in the same artifact journal as every
        other Harness entry point.
        """
        return self._execute("natural-language.plan", source={
            "description": description, "target": dict(target), "grid": grid,
            "constraints": constraints, "routing": routing or {},
        })

    def natural_language_apply(self, description, *, target, grid=0.0625, constraints=None, routing=None):
        """Compile a bounded description and create a new native schematic.

        This is intentionally a create-only convenience operation.  Existing
        cells are never overwritten; generic device grammar is placed/routed
        through the same native geometry gate as an ordinary schematic plan.
        """
        return self._execute("natural-language.apply", source={
            "description": description, "target": dict(target), "grid": grid,
            "constraints": constraints, "routing": routing or {},
        })

    def prepare_change(self, plan, *, base_sha256=None, attached_api=None):
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("change.prepare", source={"plan": plan, "base_sha256": base_sha256}, attached_api=attached_api)

    def edit_layout(self, target, actions, *, base_sha256):
        return self.prepare_change({"schema_version": 1, "operation": "layout.edit", "target": {**target, "view": "layout", "technology": target.get("technology", "018um_PDK")},
                                    "mode": "modify", "actions": actions, "base_sha256": base_sha256}, base_sha256=base_sha256)

    def append_layout(self, plan, *, base_sha256):
        if isinstance(plan, (str, Path)):
            plan = json.loads(Path(plan).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self.prepare_change({**plan, "operation": "layout.append", "mode": "modify", "base_sha256": base_sha256},
                                   base_sha256=base_sha256)

    def edit_schematic(self, target, changes, *, base_sha256, grid=0.0625):
        return self.prepare_change({"schema_version": 1, "operation": "schematic.edit", "target": target,
                                    "mode": "modify", "changes": changes, "base_sha256": base_sha256, "grid": grid}, base_sha256=base_sha256)

    def publish_change(self, candidate_id, *, domain, approved=False, candidate_sha256, base_sha256=None, destination=None,
                       allow_unmanaged=False, attached_api=None):
        return self._execute("change.publish", source={"candidate_id": candidate_id, "domain": domain, "approved": approved,
                             "candidate_sha256": candidate_sha256, "base_sha256": base_sha256,
                             "destination": destination, "allow_unmanaged": allow_unmanaged}, attached_api=attached_api)

    def recover_change(self, publication_operation_id, *, domain="schematic", approved=False, attached_api=None):
        """Recover a verified, managed schematic baseline after interrupted publication."""
        return self._execute("change.recover", source={"publication_operation_id": publication_operation_id,
                             "domain": domain, "approved": approved}, attached_api=attached_api)

    def layout_session(self, target, actions, *, attached_api=None):
        return self._execute("layout.session", target={**target, "view": "layout"}, source={"actions": actions}, attached_api=attached_api)

    def export_gds(self, target, output, layer_map, *, overwrite=False):
        return self._execute("layout.export_gds", target={**target, "view": "layout"}, source={
            "output": str(Path(output).expanduser().absolute()), "layer_map": str(Path(layer_map).expanduser().absolute()), "overwrite": overwrite})

    def simulate(self, target, setup):
        if isinstance(setup, (str, Path)):
            setup = json.loads(Path(setup).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        return self._execute("simulation.run", target={**target, "view": target.get("view", "schematic")}, source=setup)

    def export_netlist(self, target, *, output=None, device_models=None, recursive=False, netlist_format="flat"):
        """Export schematic interchange text.

        ``recursive=True`` selects the bounded hierarchical exporter.  The
        default remains the exact V1 flat metadata format for backwards
        compatibility.
        """
        source = {"device_models": device_models or {}, "recursive": bool(recursive), "netlist_format": netlist_format}
        if output is not None:
            source["output"] = str(Path(output).expanduser().absolute())
        operation = "schematic.export_hierarchical_netlist" if recursive else "schematic.export_netlist"
        return self._execute(operation, target={**target, "view": "schematic"}, source=source)

    def export_hierarchical_netlist(self, target, *, output=None, netlist_format="spice"):
        return self.export_netlist(target, output=output, recursive=True, netlist_format=netlist_format)

    def export_native_netlist(self, target, *, output_dir=None, model_libraries=None, temperature=27, netlist_format="hspice"):
        source = {"model_libraries": self.config.model_libraries if model_libraries is None else model_libraries, "temperature": temperature, "netlist_format": netlist_format}
        if output_dir is not None:
            source["output_dir"] = str(Path(output_dir).expanduser().absolute())
        return self._execute("schematic.export_native_netlist", target={**target, "view": target.get("view", "schematic")}, source=source)

    def import_netlist(self, source, target, *, require_metadata=False, recursive=False, netlist_format="auto", materialize=False):
        """Parse a certified flat or hierarchical netlist into declarative IR."""
        if isinstance(source, (str, Path)) and isinstance(source, Path):
            source = source.read_text(encoding="utf-8")
        operation = "schematic.import_hierarchical_netlist" if recursive else "schematic.import_netlist"
        return self._execute(operation, target={**target, "view": "schematic"},
                             source={"text": source, "require_metadata": require_metadata, "netlist_format": netlist_format, "materialize": bool(materialize)})

    def import_hierarchical_netlist(self, source, target, *, netlist_format="auto", materialize=False):
        return self.import_netlist(source, target, recursive=True, netlist_format=netlist_format, materialize=materialize)

    def migrate_netlist(self, source, target, manifest, *, approved=False, materialize=False):
        """Migrate a front-end netlist using an explicit, reviewed PDK map."""
        if isinstance(source, Path):
            source = str(source)
        return self._execute("schematic.migrate_netlist", target={**target, "view": "schematic"},
                             source={"source": source, "manifest": manifest, "approved": approved,
                                     "materialize": materialize})

    def normalize_waveform(self, rows, columns, *, analysis, source=None, units=None):
        return self._execute("results.normalize", source={"rows": rows, "columns": columns, "analysis": analysis,
                                                            "source": source or {}, "units": units or {}})

    def read_waveform(self, path, *, analysis, source=None, units=None):
        return self._execute("results.read_waveform", source={"path": str(Path(path).expanduser().absolute()),
                                                               "analysis": analysis, "source": source or {}, "units": units or {}})

    def validate_waveform(self, value, *, expected_analysis=None, expected_source=None):
        return self._execute("results.validate_waveform", source={"result": value, "expected_analysis": expected_analysis,
                                                                    "expected_source": expected_source})

    def find_layout_via(self, target, name):
        return self._execute("layout.find_via", target={**target, "view": "layout"}, source={"name": name})

    def delete_layout_cell(self, target, baselines, *, approved=False):
        if isinstance(baselines, dict) and "inventory_sha256" in baselines:
            return self._execute("cell.delete", target={**target, "view": "layout"}, source={"inventory_sha256": baselines["inventory_sha256"], "approved": approved})
        return self._execute("layout.delete_cell", target={**target, "view": "layout"}, source={"baselines": baselines, "approved": approved})

    def inspect_cell(self, target):
        return self._execute("cell.inspect", target={**target, "view": target.get("view", "layout")})

    def pdk_catalog(self):
        return self._execute("pdk.catalog")

    def verify_pdk(self):
        return self._execute("pdk.verify")

    def preflight(self):
        return self._execute("preflight")

    def libraries(self):
        return self._execute("library.list")

    def library_get(self, name):
        return self._execute("library.get", source={"name": name})

    def library_create(self, name, *, path=None, technology_library=None):
        return self._execute("library.create", source={"name": name, "path": path, "technology_library": technology_library})

    def library_delete(self, name, *, approved=False):
        return self._execute("library.delete", source={"name": name, "approved": approved})

    def library_rename(self, name, new_name, *, approved=False):
        return self._execute("library.rename", source={"name": name, "new_name": new_name, "approved": approved})

    def library_set_technology(self, name, technology_library, *, approved=False):
        return self._execute("library.set_technology", source={"name": name, "technology_library": technology_library, "approved": approved})

    def categories(self, library):
        return self._execute("category.list", source={"library": library})

    def category_create(self, library, category, *, cells=None, approved=False):
        return self._execute("category.create", source={"library": library, "category": category, "cells": cells or [], "approved": approved})

    def category_delete(self, library, category, *, approved=False):
        return self._execute("category.delete", source={"library": library, "category": category, "approved": approved})

    def category_cells(self, library, category):
        return self._execute("category.cells", source={"library": library, "category": category})

    def category_add_cell(self, library, category, cell, *, approved=False):
        return self._execute("category.add_cell", source={"library": library, "category": category, "cell": cell, "approved": approved})

    def category_remove_cell(self, library, category, cell, *, approved=False):
        return self._execute("category.remove_cell", source={"library": library, "category": category, "cell": cell, "approved": approved})

    def category_rename(self, library, category, new_name, *, approved=False):
        return self._execute("category.rename", source={"library": library, "category": category, "new_name": new_name, "approved": approved})

    def read(self, target, *, recursive=True, max_depth=20, include_geometry=True, include_properties=True, configuration=None):
        return self._execute("schematic.read", target=target, options={
            "recursive": recursive,
            "max_depth": max_depth,
            "include_geometry": include_geometry,
            "include_properties": include_properties,
            "configuration": configuration,
        })

    def read_schematic(self, target, *, recursive=True, max_depth=20, include_geometry=True,
                       include_properties=True, configuration=None):
        """Bridge-style name for the guarded Aether schematic read operation."""
        return self.read(target, recursive=recursive, max_depth=max_depth,
                         include_geometry=include_geometry, include_properties=include_properties,
                         configuration=configuration)

    def review(self, readback, *, op=None, result=None, intent=None, source=None, netlist=None):
        """Run the bounded read-only Design Reviewer on supplied facts.

        ``readback`` may be an in-memory saved readback or a JSON path.  This
        method only creates private review evidence; it never dispatches an
        Aether write operation or starts a simulator.  Use ``review_files``
        when the OP/readback inputs are paths and should be provenance-bound
        to their adjacent immutable run manifest.
        """
        if op is not None and result is not None:
            raise ValueError("provide only one of op and result")
        if isinstance(readback, (str, Path)) and Path(readback).expanduser().is_file():
            return self.review_files(readback, op_path=op if isinstance(op, (str, Path)) else None,
                                     intent_path=intent if isinstance(intent, (str, Path)) else None,
                                     netlist_path=netlist if isinstance(netlist, (str, Path)) else None)
        op_value = op if result is None else result
        if isinstance(op_value, Path):
            op_value = str(op_value)
        intent_value = str(intent) if isinstance(intent, Path) else intent
        payload = {"readback": readback, "op": op_value, "intent": intent_value,
                   "source": source or {}, "netlist": str(netlist) if isinstance(netlist, Path) else netlist}
        return self._execute("review", source=payload)

    def review_files(self, readback_path, *, op_path=None, intent_path=None, netlist_path=None):
        """Review immutable JSON/OP files through the normal evidence boundary."""
        return self._execute("review", source={"readback_file": str(Path(readback_path).expanduser().absolute()),
                                                "op_file": str(Path(op_path).expanduser().absolute()) if op_path is not None else None,
                                                "intent_file": str(Path(intent_path).expanduser().absolute()) if intent_path is not None else None,
                                                "netlist_file": str(Path(netlist_path).expanduser().absolute()) if netlist_path is not None else None})

    def read_layout(self, target, *, include_properties=True):
        return self._execute("layout.read", target={**target, "view": target.get("view", "layout")}, options={
            "include_properties": include_properties,
        })

    def snapshot(self, target, *, recursive=True, max_depth=20, source_kind="saved", configuration=None, automatic=False, attached_api=None):
        return self._execute("snapshot.create", target=target, attached_api=attached_api, options={
            "recursive": recursive,
            "max_depth": max_depth,
            "source_kind": source_kind,
            "configuration": configuration,
            "automatic": automatic,
        })

    def snapshot_scheduler(self, target, *, interval_seconds=None, idle_seconds=None, attached_api=None):
        from .capture import SnapshotScheduler

        return SnapshotScheduler(lambda: self.snapshot(target, automatic=True, attached_api=attached_api, source_kind="edited" if attached_api is not None else "saved"),
                                 interval_seconds=self.config.snapshot_interval_seconds if interval_seconds is None else interval_seconds,
                                 idle_seconds=self.config.snapshot_idle_seconds if idle_seconds is None else idle_seconds)

    @staticmethod
    def diff(before, after):
        return diff_readbacks(before, after)

    def diff_operation(self, before, after):
        try:
            return self._execute("design.diff", source={"before": before, "after": after})
        except ValueError as exc:
                raise ValidationError("Invalid readback diff input", stage="plan", code="invalid_diff_input") from exc

    @staticmethod
    def diff_layout(before, after):
        return diff_layout_readbacks(before, after)

    def diff_layout_operation(self, before, after):
        return self._execute("layout.diff", source={"before": before, "after": after})

    @staticmethod
    def validate_change(readback, change_set):
        return validate_candidate(readback, change_set)

    def validate_change_operation(self, readback, change_set):
        try:
            return self._execute("change.validate", source={"readback": readback, "change_set": change_set})
        except ValueError as exc:
            raise ValidationError("Invalid candidate change input", stage="plan", code="invalid_change_input") from exc

    def inspect_snapshot(self, snapshot):
        return self._execute("snapshot.inspect", source=snapshot)

    def list_snapshots(self):
        return self._execute("snapshot.list")

    def inspect_stored_snapshot(self, snapshot_id):
        return self._execute("snapshot.store.inspect", source={"snapshot_id": snapshot_id})

    def retain_snapshot(self, snapshot_id, reference):
        return self._execute("snapshot.retain", source={"snapshot_id": snapshot_id, "reference": reference})

    def smoke(self, library, cell, technology):
        return self._execute("schematic.smoke", target={"library": library, "cell": cell, "technology": technology, "view": "schematic"})

    def inspect(self, operation_id):
        return redact(ArtifactOps.inspect(self.config.artifact_root, operation_id))

    def run_attached(self, api, plan):
        """Run in the configured workspace's initialized Aether, without taking ownership."""
        return self._execute("schematic.apply", source=plan, attached_api=api)

    def _execute(self, operation, *, source=None, target=None, attached_api=None, options=None):
        result = empty_result(operation)
        artifacts = None
        try:
            artifacts = ArtifactOps(self.config.artifact_root, result["operation_id"])
            artifacts.event("plan", "started")
            request = {"operation": operation, "operation_id": result["operation_id"]}
            if source is not None:
                if operation == "schematic.apply":
                    try:
                        plan = validate_plan(source)
                        compile_wires(plan)
                    except (TypeError, KeyError, ValueError, OverflowError) as exc:
                        raise ValidationError("Malformed declarative plan", stage="plan", code="invalid_plan") from exc
                    artifacts.write("plan.json", plan)
                    request["plan"] = plan
                elif operation == "layout.apply":
                    try:
                        plan = validate_layout_plan(source)
                    except (TypeError, KeyError, ValueError, OverflowError, ValidationError) as exc:
                        if isinstance(exc, ValidationError):
                            raise
                        raise ValidationError("Malformed declarative layout plan", stage="plan", code="invalid_layout_plan") from exc
                    artifacts.write("layout-plan.json", plan)
                    request["plan"] = plan
                elif operation in {"symbol.apply", "symbol.append"}:
                    from .symbol_append import validate_symbol_append
                    from .symbol import validate_symbol_plan

                    try:
                        plan = (validate_symbol_append if operation == "symbol.append" else validate_symbol_plan)(source)
                    except (TypeError, KeyError, ValueError, OverflowError) as exc:
                        raise ValidationError("Malformed declarative symbol plan", stage="plan", code="invalid_symbol_plan") from exc
                    artifacts.write("symbol-plan.json", plan)
                    request["plan"] = plan
                else:
                    if operation.startswith("library."):
                        from .library import validate_library_request

                        source = validate_library_request(source, operation=operation)
                    elif operation.startswith("category."):
                        from .library import validate_category_request

                        source = validate_category_request(source, operation=operation)
                    elif operation.startswith("maestro."):
                        from .maestro import validate_maestro_request

                        source = validate_maestro_request(source, operation=operation)
                        if source["action"] == "open" and source["persistent"] and attached_api is None:
                            raise ValidationError("Retained sessions require an explicit initialized in-process API", stage="plan", code="persistent_session_requires_attachment")
                    if operation == "simulation.run":
                        from .analyses import validate_analysis_request

                        source = validate_analysis_request(source)
                    if operation == "schematic.export_native_netlist":
                        from .netlist_native import validate_native_export

                        source = validate_native_export(source)
                    if operation == "symbol.generate":
                        from .symbol import validate_symbol_generation

                        source = validate_symbol_generation(source)
                    if operation == "change.prepare":
                        if not isinstance(source, dict) or set(source) != {"plan", "base_sha256"} or not isinstance(source["plan"], dict):
                            raise ValidationError("Invalid candidate request", stage="plan", code="invalid_candidate_request")
                        from .layout_edit import validate_layout_edit
                        from .layout_append import validate_layout_append
                        from .schematic_edit import validate_schematic_edit

                        validator = {"layout.apply": validate_layout_plan, "layout.edit": validate_layout_edit, "layout.append": validate_layout_append,
                                     "schematic.edit": validate_schematic_edit}.get(source["plan"].get("operation"), validate_plan)
                        source = {**source, "plan": validator(source["plan"])}
                    if operation == "change.publish" and (source.get("approved") is not True or source.get("domain") not in {"layout", "schematic"}):
                        raise ValidationError("Publication requires an explicit approval and domain", stage="plan", code="approval_required")
                    if operation == "change.recover" and (not isinstance(source, dict) or set(source) != {"publication_operation_id", "domain", "approved"} or
                                                          source.get("approved") is not True or source.get("domain") != "schematic" or
                                                          not isinstance(source.get("publication_operation_id"), str)):
                        raise ValidationError("Recovery requires a schematic publication ID and explicit approval", stage="plan", code="approval_required")
                    artifacts.write("input.json", source)
                    request["source"] = source
            if target is not None:
                for name in target.values():
                    identifier(name)
                request["target"] = target
            if options is not None:
                request["options"] = options
            if operation == "design.diff":
                if not isinstance(source, dict) or set(source) != {"before", "after"}:
                    raise ValidationError("design.diff requires before and after readbacks", stage="plan", code="invalid_diff_input")
                try:
                    result["readback"] = diff_readbacks(source["before"], source["after"])
                except (TypeError, ValueError) as exc:
                    raise ValidationError("Invalid readback diff input", stage="plan", code="invalid_diff_input") from exc
                artifacts.write("diff.json", result["readback"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "natural-language.plan" and isinstance(source, dict) and source.get("constraints") is None and not source.get("routing"):
                # The deterministic compiler is part of the public offline
                # core.  Native geometry planning is still required for a
                # generic device grammar when callers provide constraints or
                # routing options, so that path continues to the backend.
                from .natural_language import natural_language_to_plan

                compiled = natural_language_to_plan(source["description"], target=source["target"], grid=source.get("grid", 0.0625))
                result["readback"] = compiled
                artifacts.write("natural-language-plan.json", compiled)
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "layout.diff":
                if not isinstance(source, dict) or set(source) != {"before", "after"}:
                    raise ValidationError("layout.diff requires before and after readbacks", stage="plan", code="invalid_layout_diff_input")
                try:
                    result["readback"] = diff_layout_readbacks(source["before"], source["after"])
                except (TypeError, ValueError) as exc:
                    raise ValidationError("Invalid layout readback diff input", stage="plan", code="invalid_layout_diff_input") from exc
                artifacts.write("layout-diff.json", result["readback"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "results.normalize":
                from .results import normalize_waveform

                if not isinstance(source, dict) or set(source) != {"rows", "columns", "analysis", "source", "units"}:
                    raise ValidationError("results.normalize input is malformed", stage="plan", code="invalid_result_input")
                result["readback"] = normalize_waveform(source["rows"], source["columns"], analysis=source["analysis"],
                                                         source=source["source"], units=source["units"])
                artifacts.write("waveform.json", result["readback"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "results.read_waveform":
                from .results import read_waveform_csv

                if not isinstance(source, dict) or set(source) != {"path", "analysis", "source", "units"}:
                    raise ValidationError("results.read_waveform input is malformed", stage="plan", code="invalid_result_input")
                result["readback"] = read_waveform_csv(source["path"], analysis=source["analysis"], source=source["source"], units=source["units"])
                artifacts.write("waveform.json", result["readback"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "results.validate_waveform":
                from .results import validate_waveform_result

                if not isinstance(source, dict) or set(source) != {"result", "expected_analysis", "expected_source"}:
                    raise ValidationError("results.validate_waveform input is malformed", stage="plan", code="invalid_result_input")
                errors = validate_waveform_result(source["result"], expected_analysis=source["expected_analysis"], expected_source=source["expected_source"])
                result["readback"] = {"valid": not errors, "errors": errors}
                artifacts.write("waveform-validation.json", result["readback"])
                result["ok"] = not errors
                result["stage"] = "complete" if not errors else "results"
                if errors:
                    raise ValidationError("Waveform result failed validation", stage="results", code="result_provenance", details={"errors": errors})
            elif operation == "review":
                from .reviewer import ReviewInputError, render_review_markdown, review_design, review_from_files

                if not isinstance(source, dict):
                    raise ValidationError("review input is malformed", stage="plan", code="invalid_review_input")
                try:
                    if source.get("readback_file"):
                        review = review_from_files(source["readback_file"], op_path=source.get("op_file"),
                                                   intent_path=source.get("intent_file"), netlist_path=source.get("netlist_file"))
                    else:
                        review = review_design(source.get("readback"), op=source.get("op"), intent=source.get("intent"),
                                               source=source.get("source"), netlist=source.get("netlist"))
                except ReviewInputError as exc:
                    raise ValidationError(str(exc), stage="review", code="invalid_review_input") from exc
                result["readback"] = review
                artifacts.write("review.json", review)
                artifacts.write_text("review.md", render_review_markdown(review))
                result["metrics"] = {"finding_count": len(review.get("findings", [])),
                                      "coverage_complete": review.get("coverage", {}).get("complete", False),
                                      "pairing_status": review.get("coverage", {}).get("pairing_status", "not_requested")}
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "pdk.catalog":
                result["readback"] = catalog(self.config.pdk_root)
                artifacts.write("pdk-catalog.json", result["readback"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "change.validate":
                if not isinstance(source, dict) or set(source) != {"readback", "change_set"}:
                    raise ValidationError("change.validate requires readback and change_set", stage="plan", code="invalid_change_input")
                try:
                    result["readback"] = validate_candidate(source["readback"], source["change_set"])
                except (TypeError, ValueError) as exc:
                    raise ValidationError("Invalid candidate change input", stage="plan", code="invalid_change_input") from exc
                artifacts.write("candidate.json", result["readback"]["candidate"])
                artifacts.write("diff.json", result["readback"]["diff"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "snapshot.inspect":
                if not isinstance(source, dict):
                    raise ValidationError("snapshot.inspect requires a snapshot object", stage="plan", code="invalid_snapshot_input")
                errors = validate_snapshot(source)
                if errors:
                    raise ValidationError("Snapshot integrity or completeness validation failed", stage="snapshot", code="invalid_snapshot", details={"errors": errors})
                result["readback"] = source
                artifacts.write("snapshot.json", source)
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "snapshot.list":
                store = SnapshotStore(self.config.snapshot_root, retention_count=self.config.snapshot_retention_count,
                                      retention_hours=self.config.snapshot_retention_hours, quota_bytes=self.config.snapshot_quota_bytes)
                result["readback"] = {"snapshot_root": str(store.root), "snapshot_ids": store.list()}
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "snapshot.store.inspect":
                if not isinstance(source, dict) or set(source) != {"snapshot_id"}:
                    raise ValidationError("snapshot.store.inspect requires a snapshot ID", stage="plan", code="invalid_snapshot_input")
                store = SnapshotStore(self.config.snapshot_root, retention_count=self.config.snapshot_retention_count,
                                      retention_hours=self.config.snapshot_retention_hours, quota_bytes=self.config.snapshot_quota_bytes)
                result["readback"] = store.inspect(source["snapshot_id"])
                artifacts.write("snapshot.json", result["readback"])
                result["ok"] = True
                result["stage"] = "complete"
            elif operation == "snapshot.retain":
                if not isinstance(source, dict) or set(source) != {"snapshot_id", "reference"}:
                    raise ValidationError("snapshot.retain requires snapshot_id and reference", stage="plan", code="invalid_snapshot_input")
                store = SnapshotStore(self.config.snapshot_root, retention_count=self.config.snapshot_retention_count,
                                      retention_hours=self.config.snapshot_retention_hours, quota_bytes=self.config.snapshot_quota_bytes)
                store.retain(source["snapshot_id"], source["reference"])
                result["readback"] = {"snapshot_id": source["snapshot_id"], "reference": source["reference"], "retained": True}
                result["ok"] = True
                result["stage"] = "complete"
            elif operation.startswith("maestro.") and source and source.get("action") in {"job_status", "job_wait", "job_cancel"}:
                from . import maestro_jobs

                if source["action"] == "job_status":
                    result["readback"] = maestro_jobs.status(self.config, source["job_id"])
                elif source["action"] == "job_wait":
                    result["readback"] = maestro_jobs.wait(self.config, source["job_id"], source["timeout"])
                else:
                    result["readback"] = maestro_jobs.cancel(self.config, source["job_id"])
                result["ok"] = True
                result["stage"] = "complete"
                artifacts.write("maestro-readback.json", result["readback"])
            elif operation.startswith("maestro.") and source and source.get("action") == "run" and source.get("wait") is False:
                if attached_api is not None:
                    raise ValidationError("Detached Maestro jobs cannot borrow an attached API session", stage="maestro", code="async_attachment_unavailable")
                from . import maestro_jobs

                request["options"] = {**self.config.worker_options(), **(options or {})}
                result["readback"] = maestro_jobs.launch(self.config, request, artifacts)
                result["changes"].append({"type": "maestro_job_started", "job_id": result["readback"]["job_id"]})
                result["ok"] = True
                result["stage"] = "complete"
                artifacts.write("maestro-readback.json", result["readback"])
            elif attached_api is None:
                result = AetherRuntime(self.config).execute(request, artifacts)
            else:
                from .worker import dispatch

                if Path.cwd().resolve() != self.config.workspace.resolve():
                    raise OperationError("Attached session must already use the configured workspace", stage="session", code="workspace_mismatch")
                request["options"] = {**self.config.worker_options(), **(options or {})}
                artifacts.write("session.json", {"owner": "user", "transport": "in-process", "shutdown_permitted": False})
                with workspace_lock(self.config.workspace):
                    dispatch(attached_api, request, artifacts, result, owned=False)
        except HarnessError as exc:
            if artifacts is not None:
                try:
                    checkpoint = json.loads((artifacts.path / "checkpoint.json").read_text())
                    if checkpoint.get("operation_id") == result["operation_id"]:
                        result["changes"] = checkpoint["changes"]
                        result["readback"] = checkpoint["readback"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            result["ok"] = False
            result["stage"] = exc.stage
            result["errors"].append(exc.to_dict())
        except Exception as exc:
            result["ok"] = False
            result["errors"].append(OperationError("Operation could not complete", stage=result["stage"], code="operation_failed", details={"exception": type(exc).__name__}).to_dict())
        result = redact(result)
        if artifacts is not None:
            try:
                artifacts.finish(result)
            except (HarnessError, OSError) as exc:
                result["ok"] = False
                result["stage"] = "artifacts"
                error = exc if isinstance(exc, HarnessError) else ArtifactError("Cannot finalize evidence", stage="artifacts", code="write_failed")
                result["errors"].append(error.to_dict())
        return result
