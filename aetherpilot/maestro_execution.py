"""Fresh, serial native MDE execution with immutable evidence publication."""

from __future__ import annotations

import copy
import math
import re
import shutil
import uuid
from pathlib import Path

from .artifacts import atomic_json, atomic_text, file_digest
from .contract import digest, engineering
from .errors import OperationError, ValidationError
from .maestro import MaestroStore, normalize_native_result, state_digest
from .maestro_specs import evaluate_native_outputs
from .maestro_results import native_output_records, native_result_points, parse_native_result_table, save_run_manifest, select_run, select_runs, select_waveform, verify_simulator_log
from .native_tools import native_tool_environment
from .psf import read_native_waveform
from .results import safe_result_path, write_waveform_csv


class MaestroExecution:
    def __init__(self, native, target, options=None):
        self.native = native
        self.target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        self.options = options or {}
        self.store = MaestroStore(native._library_path(target["library"]), target["library"], target["cell"], self.target["view"])
        if not self.store.path.is_file():
            raise OperationError("Native execution requires a managed MDE state", stage="maestro", code="unmanaged_state")
        self.state = self.store.load()
        self.setting_path = safe_result_path(self.store.directory / "setting", max_bytes=8 * 1024 ** 2)
        self.setting_hash = file_digest(self.setting_path)
        if self.setting_hash != self.state["metadata"].get("native_setting_sha256"):
            raise OperationError("Native MDE state changed outside the harness", stage="maestro", code="baseline_changed")
        if (self.store.directory / ".aetherpilot-transaction.json").exists():
            raise OperationError("MDE state needs transaction recovery", stage="maestro", code="recovery_required")

    def directory(self, action):
        root = self.options.get("maestro_artifact_dir")
        root = Path(root) if root else self.native.workspace / ".aetherpilot-maestro-results" / uuid.uuid4().hex
        directory = safe_result_path(root / action, must_exist=False)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        return directory

    def selected_state(self, test):
        test = test or self.state["current_test"]
        if test not in self.state["tests"]:
            raise OperationError("MDE test does not exist", stage="maestro", code="test_missing")
        state = copy.deepcopy(self.state)
        state["current_test"] = test
        selected = state["tests"][test]
        if not selected["enabled"]:
            raise ValidationError("Disabled MDE test cannot run", stage="maestro", code="test_disabled")
        state["design"] = copy.deepcopy(selected["design"])
        for name in ("analyses", "outputs", "specs", "environment_options", "simulator_options"):
            state[name] = copy.deepcopy(selected[name])
        return state

    def prepare(self, state, directory):
        session = self.native._open(self.target)
        try:
            configuration = directory / "configuration"
            configuration.mkdir(mode=0o700)
            session = self.native._project_native(session, state, configuration) or session
            setting = session.getSetting()
            if not setting.setProjectDirectory(str(directory)) or not setting.saveSetting(str(configuration)):
                raise OperationError("Native run directory could not be isolated", stage="maestro", code="native_setting_write_failed")
            return session
        except Exception:
            if session.isValid():
                self.native._close(session)
            raise

    def check_source_unchanged(self):
        if file_digest(self.setting_path) != self.setting_hash or state_digest(self.store.load()) != state_digest(self.state):
            raise OperationError("MDE source changed during execution", stage="maestro", code="baseline_changed")

    @staticmethod
    def netlist_files(paths, directory):
        files = []
        for value in paths:
            path = safe_result_path(str(value))
            if directory.resolve() not in path.parents or not path.stat().st_size:
                raise OperationError("Native netlist is empty or outside its run", stage="maestro", code="native_netlist_invalid")
            files.append({"path": str(path), "sha256": file_digest(path), "bytes": path.stat().st_size})
        if not files:
            raise OperationError("Native MDE did not produce a netlist", stage="maestro", code="native_netlist_failed")
        return files

    @staticmethod
    def export_directory(directory, output):
        if not output:
            return None
        destination = safe_result_path(output, must_exist=False)
        if destination.exists() or directory.resolve() in destination.parents or destination in directory.resolve().parents:
            raise ValidationError("Export destination already exists or overlaps the run", stage="results", code="target_exists")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValidationError("Run contains unsafe export links", stage="results", code="unsafe_result_path")
        shutil.copytree(directory, destination, symlinks=False)
        for path in directory.rglob("*"):
            if path.is_file() and file_digest(path) != file_digest(destination / path.relative_to(directory)):
                raise OperationError("Export copy failed verification", stage="results", code="result_provenance")
        return str(destination)

    def create_netlist(self, *, test="", corner="", output_dir=None):
        state = self.selected_state(test)
        if corner and corner not in state["corners"]:
            raise ValidationError("Netlist corner does not exist", stage="maestro", code="corner_missing")
        directory = self.directory("netlist")
        session = self.prepare(state, directory)
        try:
            with native_tool_environment(directory / "tools", ["emy2netlist"]) as tools:
                paths = session.createDesignNetlist(str(directory / "export"))
                if not paths:
                    raise OperationError("Native design netlisting failed", stage="maestro", code="native_netlist_failed", details={"error": str(session.lastError())})
                if corner:
                    simulation_paths = session.createSimulationNetlist(corner, "", str(directory / "export"))
                    if not simulation_paths:
                        raise OperationError("Native corner netlisting failed", stage="maestro", code="native_netlist_failed", details={"error": str(session.lastError())})
                    paths = [*paths, *simulation_paths]
                files = self.netlist_files(paths or (), directory)
        finally:
            self.native._close(session)
        self.check_source_unchanged()
        exported = self.export_directory(directory, output_dir)
        return {"target": self.target, "action": "create_netlist", "test": state["current_test"], "corner": corner,
                "kind": "simulation" if corner else "design", "files": files, "tools": tools,
                "state_sha256": state_digest(self.state), "output_dir": exported or str(directory)}

    def run(self, *, test="", output_dir=None):
        tests = [test] if test else [name for name, selected in self.state["tests"].items() if selected["enabled"]]
        corners = [name for name, value in self.state["corners"].items() if value["enabled"]]
        if not tests or not corners or len(tests) * len(corners) > 128:
            raise ValidationError("Native run requires enabled tests/corners within the local budget", stage="maestro", code="resource_budget_exceeded")
        directory = self.directory("run")
        campaign_id = uuid.uuid4().hex
        history = "Local." + campaign_id
        entries, runs = [], []
        for test_name in tests:
            state = self.selected_state(test_name)
            selected = state["tests"][test_name]
            analyses = {name: value for name, value in selected["analyses"].items() if value["enabled"]}
            if not analyses:
                raise ValidationError("Native simulation requires an enabled analysis", stage="maestro", code="analysis_missing")
            test_directory = directory / test_name
            test_directory.mkdir(mode=0o700)
            from .maestro_hspice import is_hspice

            if is_hspice(selected):
                from .maestro_hspice_execution import run_test

                payload = run_test(self, state, test_directory, corners=corners, history=history, campaign_id=campaign_id)
                entries.append(save_run_manifest(test_directory, payload))
                runs.append(payload)
                continue
            session = self.prepare(state, test_directory)
            try:
                with native_tool_environment(test_directory / "tools", ["emy2netlist"]) as tools:
                    netlists = self.netlist_files(session.createDesignNetlist(str(test_directory / "netlist")) or (), test_directory)
                    native_result = session.netlistAndRun()
                    result = normalize_native_result(native_result)
                    if not result["valid"] or result["errors"]:
                        raise OperationError("Native MDE did not complete a valid simulation", stage="results", code="native_result_invalid", details=result)
                    csv_files = native_result.saveResultsToDir(str(test_directory / "csv"))
                    if not csv_files:
                        raise OperationError("Native MDE result export is empty", stage="results", code="native_result_export_failed")
                    tables = [parse_native_result_table(str(path)) for path in csv_files]
                    result_records = [record for table in tables for record in native_output_records(table)]
            finally:
                self.native._close(session)
            self.check_source_unchanged()
            run_id = uuid.uuid4().hex
            waveforms, bundles, logs = [], [], []
            found_logs = list(test_directory.rglob("alps.out"))
            by_corner = {}
            for path in found_logs:
                corner = path.parent.parent.name
                if corner in by_corner:
                    raise ValidationError("Multiple native results exist for one corner", stage="results", code="ambiguous_result")
                by_corner[corner] = path
            if set(by_corner) != set(corners):
                raise OperationError("Native corner completion set differs from the request", stage="results", code="result_incomplete")
            for corner, log_path in sorted(by_corner.items()):
                log = verify_simulator_log(log_path)
                logs.append(log)
                for analysis, setup in analyses.items():
                    raw = log_path.parent / (analysis + "." + analysis)
                    provenance = {"run_id": run_id, "test": test_name, "corner": corner, "state_sha256": state_digest(self.state),
                                  "setup_sha256": digest(setup), "log_sha256": log["sha256"]}
                    bundle = read_native_waveform(raw, test_directory / "waveforms" / corner / analysis,
                                                  analysis=analysis, provenance=provenance,
                                                  timeout=min(20, self.options.get("simulator_timeout_seconds", 20)))
                    axis = bundle["waveform"]["axis"]["values"]
                    options = setup["options"]
                    if "stop" in options and not math.isclose(axis[-1], engineering(options["stop"]), rel_tol=0.0006, abs_tol=0):
                        raise ValidationError("Native waveform does not reach its requested stop", stage="results", code="result_incomplete")
                    if "start" in options and not math.isclose(axis[0], engineering(options["start"]), rel_tol=0.0006, abs_tol=0):
                        raise ValidationError("Native waveform starts at the wrong sweep value", stage="results", code="result_provenance")
                    available = {item["name"] for item in bundle["waveform"]["signals"]} | set(bundle["complex_signals"])
                    for output in selected["outputs"].values():
                        if output["output_type"] == "Signal" and output["save"]:
                            expression = output["signal_name"] or output["expr"]
                            match = re.fullmatch(r"[vViI]\(\s*([^()]+)\s*\)", expression)
                            signal = match.group(1).strip().strip('"') if match else expression
                            if signal not in available:
                                raise ValidationError("Saved native output is missing from waveform", stage="results", code="signal_missing", details={"output": output["name"], "analysis": analysis})
                    waveform_path = test_directory / "waveforms" / corner / analysis / "waveform.json"
                    atomic_json(waveform_path, bundle)
                    waveforms.append({"corner": corner, "analysis": analysis, "path": str(waveform_path), "raw_path": str(raw), "rows": len(axis)})
                    bundles.append({"corner": corner, "analysis": analysis, **bundle})
            points = native_result_points(tables, test=test_name, expected_corners=corners, expected_outputs=selected["outputs"])
            spec_results = evaluate_native_outputs(points, selected["outputs"], selected["specs"], bundles)
            payload = {"run_id": run_id, "campaign_id": campaign_id, "history": history, "test": test_name, "target": self.target,
                       "state_sha256": state_digest(self.state), "selected_state_sha256": state_digest(state),
                       "native_result": result, "tables": tables, "netlists": netlists, "waveforms": waveforms, "logs": logs, "tools": tools,
                       "result_records": result_records, "points": points, "spec_results": spec_results, "overall_spec": ("passed" if spec_results and all(item["status"] == "pass" for item in spec_results) else "failed" if spec_results else None),
                       "spec_policy": "native_scalar_or_explicit_waveform_reduction",
                       "fresh": True, "local_max_jobs": 1, "configuration": str(test_directory / "configuration/setting")}
            entries.append(save_run_manifest(test_directory, payload))
            runs.append(payload)
        self.check_source_unchanged()
        exported = self.export_directory(directory, output_dir)
        new_state = copy.deepcopy(self.state)
        new_state["results"].extend(entries)
        self.store.save(new_state)
        return {"target": self.target, "action": "run", "campaign_id": campaign_id, "history": history, "runs": runs, "registry": entries, "fresh": True,
                "output_dir": exported or str(directory), "source_native_unchanged": True}

    def read_results(self, *, history="", test=""):
        runs = select_runs(self.state, history=history, test=test)
        records, points, waveforms, spec_results = [], [], [], []
        for run in runs:
            if run.get("spec_results") and run.get("spec_policy") != "native_scalar_or_explicit_waveform_reduction":
                raise ValidationError("Historical specification verdict used legacy implicit waveform reduction; rerun with explicit scalar semantics",
                                      stage="results", code="legacy_spec_semantics")
            outputs = run.get("result_records")
            if outputs is None:
                outputs = [record for table in run.get("tables", []) for record in native_output_records(table)]
            records.extend({"test": run["test"], **copy.deepcopy(output)} for output in outputs)
            for point in run.get("points", []):
                points.append({**copy.deepcopy(point), "point": len(points) + 1})
            waveforms.extend({"test": run["test"], **copy.deepcopy(waveform)} for waveform in run.get("waveforms", []))
            spec_results.extend({"test": run["test"], **copy.deepcopy(result)} for result in run.get("spec_results", []))
        overall = "failed" if any(run.get("overall_spec") == "failed" for run in runs) else "passed" if any(run.get("overall_spec") == "passed" for run in runs) else None
        flattened = [{"name": name, "point": point["point"], "corner": point["corner"], "spec_status": output["pass_fail"], **copy.deepcopy(output)}
                     for point in points for name, output in point["outputs"].items()]
        return {"target": self.target, "action": "read_results", "run": runs[0] if len(runs) == 1 else None, "runs": runs,
                "outputs": flattened, "native_output_records": records, "overall_yield": None,
                "history": runs[0]["history"], "tests": [run["test"] for run in runs], "points": points,
                "waveforms": waveforms, "spec_results": spec_results, "overall_spec": overall, "integrity_verified": True}

    def export_waveform(self, *, analysis, expression, output, history="", test="", corner=""):
        run = select_run(self.state, history=history, test=test)
        selected = select_waveform(run, analysis=analysis, expression=expression, corner=corner)
        exported = write_waveform_csv(selected["waveform"], output)
        return {"target": self.target, "action": "export_waveform", "run_id": run["run_id"], "export": exported, **selected}

    def export_output_view(self, *, output, table_view="detail", history="", test=""):
        from .maestro_exports import export_output_table

        run = select_run(self.state, history=history, test=test)
        return {"target": self.target, "action": "export_output_view", "export": export_output_table(run, table_view, output)}

    def write_script(self, *, output):
        from .maestro_exports import replay_script, write_export

        exported = write_export(output, replay_script(self.state).encode("utf-8"))
        self.check_source_unchanged()
        return {"target": self.target, "action": "write_script", "export": exported, "state_sha256": state_digest(self.state),
                "format": "python", "destination_policy": "new_view_only", "history_policy": "configuration_only"}

    def snapshot(self):
        directory = self.directory("snapshot")
        atomic_text(directory / "setting", self.setting_path.read_text(encoding="utf-8"))
        atomic_json(directory / "aetherpilot-maestro.json", self.state)
        for name in ("master.tag", "adeL2View.file", "adeView.file"):
            path = self.store.directory / name
            if path.is_file():
                shutil.copyfile(safe_result_path(path), directory / name)
        self.check_source_unchanged()
        for entry in self.state["results"]:
            select_run(self.state, history=entry["run_id"])
        files = [{"path": path.name, "sha256": file_digest(path)} for path in sorted(directory.iterdir())]
        manifest = {"target": self.target, "state_sha256": state_digest(self.state), "files": files, "immutable": True,
                    "results_policy": "verified_external_run_references"}
        atomic_json(directory / "snapshot.json", manifest)
        manifest["manifest_sha256"] = file_digest(directory / "snapshot.json")
        return {"action": "snapshot_state", "path": str(directory), **manifest}
