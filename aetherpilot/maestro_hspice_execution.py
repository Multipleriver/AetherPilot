"""Native legacy MDE runs with immutable raw data and explicit evaluations."""

from __future__ import annotations

import math
import copy
import re
import uuid

from .analyses import parse_operating_point
from .artifacts import atomic_json, file_digest
from .contract import digest, engineering, identifier
from .errors import OperationError, ValidationError
from .maestro import normalize_native_result, state_digest
from .maestro_results import parse_native_result_table, verify_simulator_log
from .maestro_specs import evaluate_outputs, resolve_signal_name, spec_text
from .native_tools import native_tool_environment
from .psf import read_native_waveform
from .results import normalize_waveform, safe_result_path


def _only(paths, what):
    paths = [path for path in paths if not path.is_symlink()]
    if len(paths) != 1:
        raise ValidationError("Native HSPICE result is missing or ambiguous", stage="results", code="result_incomplete", details={"kind": what, "count": len(paths)})
    return safe_result_path(paths[0])


def operating_point_bundle(source, directory, outputs, provenance):
    source = safe_result_path(source, max_bytes=32 * 1024 ** 2)
    fingerprint = file_digest(source)
    text = source.read_text(encoding="utf-8")
    names = {match.group(1).lower() for match in re.finditer(r"(?:^|\s)0\s*:\s*([A-Za-z0-9_]+)\s*=\s*\S+", text, re.M)}
    if not names:
        raise ValidationError("Native operating point has no identifiable node voltages", stage="results", code="result_incomplete")
    columns = ["v(" + name + ")" for name in sorted(names)]
    mapping = {"v(" + name + ")": name for name in names}
    rows = parse_operating_point(text, {"signals": columns}, mapping)
    if file_digest(source) != fingerprint:
        raise ValidationError("Native operating point changed while reading", stage="results", code="result_provenance")
    waveform = normalize_waveform(rows, columns, analysis="op", source={**provenance, "native_result_sha256": fingerprint, "parser": "ALPS_op0"}, units={name: "V" for name in columns}, fresh=True)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    return {"waveform": waveform, "complex_signals": {}, "raw_path": str(source), "raw_sha256": fingerprint, "raw_result_authoritative": True,
            "conversion": {"format": "ALPS_op0_text", "lossless": False, "precision": "native_report_precision"}}


def result_points(tables, *, test, corners, selected, bundles, expected_parameters):
    parameter_sections = [section for table in tables for section in table["sections"] if section["columns"][0] == "Parameter"]
    if len(parameter_sections) != 1:
        raise ValidationError("Native HSPICE parameter table is missing or ambiguous", stage="results", code="result_incomplete")
    section = parameter_sections[0]
    columns = {name: index for index, name in enumerate(section["columns"][1:], 1) if name.strip()}
    if set(columns) != set(corners):
        raise ValidationError("Native HSPICE corner completion set differs", stage="results", code="result_incomplete")
    points, verdicts = [], []
    table_hashes = [table["sha256"] for table in tables]
    for index, corner in enumerate(corners, 1):
        parameters = {}
        for row in section["rows"]:
            if row[0] in parameters or not row[0].strip():
                raise ValidationError("Native HSPICE parameter names are ambiguous", stage="results", code="result_parse")
            parameters[row[0]] = row[columns[corner]]
        for name, value in expected_parameters[corner].items():
            if name not in parameters or not math.isclose(engineering(parameters[name]), engineering(value), rel_tol=1e-6, abs_tol=0):
                raise ValidationError("Native HSPICE parameter table differs from the setup", stage="results", code="result_provenance", details={"corner": corner, "parameter": name})
        outputs = {}
        for name, output in selected["outputs"].items():
            expression = output["signal_name"] or output["expr"]
            available = []
            for bundle in bundles:
                if bundle["corner"] != corner:
                    continue
                try:
                    resolved = resolve_signal_name(bundle, expression)
                except ValidationError:
                    if output["save"]:
                        raise
                else:
                    available.append({"analysis": bundle["analysis"], "signal": resolved, "raw_sha256": bundle["raw_sha256"]})
            spec = selected["specs"].get(name, {})
            item = {"test": test, "value": "", "numeric_value": None, "spec": spec_text(spec), "weight": "", "pass_fail": "", "native_aggregate_status": "",
                    "native_scalar_available": False, "kind": "waveform", "available_analyses": available, "table_sha256": table_hashes[0]}
            if spec_text(spec):
                if not spec.get("analysis") or not spec.get("reduction"):
                    raise ValidationError("Legacy HSPICE waveform specifications require an explicit analysis and reduction", stage="results", code="spec_requires_scalar")
                evaluated = evaluate_outputs({name: {**output, "spec": spec}}, bundles, analysis=spec["analysis"], corner=corner)[0]
                evaluated["evaluation"]["kind"] = "explicit_waveform_reduction"
                item.update(numeric_value=evaluated["value"], value=format(evaluated["value"], ".17g"), pass_fail=evaluated["status"], evaluation=evaluated["evaluation"])
                verdicts.append(evaluated)
            outputs[name] = item
        points.append({"point": index, "test": test, "corner": corner, "parameters": parameters, "outputs": outputs})
    return points, verdicts


def run_test(execution, state, directory, *, corners, history, campaign_id):
    test = state["current_test"]
    identifier(test, "test")
    for corner in corners:
        identifier(corner, "corner")
    selected = state["tests"][test]
    analyses = {name: definition for name, definition in selected["analyses"].items() if definition["enabled"]}
    runtime_state = copy.deepcopy(state)
    for output in runtime_state["tests"][test]["outputs"].values():
        output["plot"] = False
    session = None
    try:
        with native_tool_environment(directory / "tools", ["emy2netlist"]) as tools:
            session = execution.prepare(runtime_state, directory)
            netlists = execution.netlist_files(session.createDesignNetlist(str(directory / "netlist")) or (), directory)
            native = session.netlistAndRun()
            result = normalize_native_result(native)
            if not result["valid"] or result["errors"]:
                raise OperationError("Native HSPICE MDE run is invalid", stage="results", code="native_result_invalid", details=result)
            csv_files = native.saveResultsToDir(str(directory / "csv"))
            if not csv_files:
                raise OperationError("Native HSPICE result tables are empty", stage="results", code="native_result_export_failed")
            tables = [parse_native_result_table(str(path)) for path in csv_files]
    finally:
        if session is not None:
            execution.native._close(session)
    execution.check_source_unchanged()
    run_id = uuid.uuid4().hex
    bundles, waveforms, logs = [], [], []
    result_directories = [path for path in directory.rglob("*.sp.sim") if path.is_dir() and not path.is_symlink()]
    if len(result_directories) != len(corners):
        raise ValidationError("Native HSPICE result directory set differs", stage="results", code="result_incomplete")
    for corner in corners:
        matches = [path for path in result_directories if path.name.endswith("_" + corner + ".sp.sim")]
        if len(matches) != 1:
            raise ValidationError("Native HSPICE corner result is ambiguous", stage="results", code="ambiguous_result")
        result_directory = matches[0]
        log = verify_simulator_log(_only(result_directory.glob("*.sp.log"), "simulator_log"))
        logs.append({**log, "corner": corner})
        for analysis, setup in analyses.items():
            suffix = {"dc": "sw0", "ac": "ac0", "tran": "tr0", "op": "op0"}[analysis]
            raw = _only(result_directory.glob("*." + suffix), analysis)
            destination = directory / "waveforms" / corner / analysis
            provenance = {"run_id": run_id, "test": test, "corner": corner, "state_sha256": state_digest(execution.state), "setup_sha256": digest(setup), "log_sha256": log["sha256"], "netlist_format": "hspice"}
            bundle = operating_point_bundle(raw, destination, selected["outputs"], provenance) if analysis == "op" else read_native_waveform(raw, destination, analysis=analysis, provenance=provenance, timeout=min(20, execution.options.get("simulator_timeout_seconds", 20)))
            axis = bundle["waveform"]["axis"]["values"]
            for position, field in ((0, "start"), (-1, "stop")):
                if field in setup["options"] and not math.isclose(axis[position], engineering(setup["options"][field]), rel_tol=0.0006, abs_tol=0):
                    raise ValidationError("Native HSPICE waveform bounds differ from the setup", stage="results", code="result_incomplete", details={"analysis": analysis, "field": field})
            path = destination / "waveform.json"
            atomic_json(path, bundle)
            bundles.append({"corner": corner, "analysis": analysis, **bundle})
            waveforms.append({"corner": corner, "analysis": analysis, "path": str(path), "raw_path": str(raw), "rows": len(axis)})
    variables = {**state["parameters"], **state["variables"], **selected["variables"]}
    expected_parameters = {corner: {"temperature": state["corners"][corner]["temperature"], **{name: value["value"] for name, value in variables.items()}, **state["corners"][corner]["variables"]} for corner in corners}
    points, spec_results = result_points(tables, test=test, corners=corners, selected=selected, bundles=bundles, expected_parameters=expected_parameters)
    return {"run_id": run_id, "campaign_id": campaign_id, "history": history, "test": test, "target": execution.target,
            "state_sha256": state_digest(execution.state), "selected_state_sha256": state_digest(state), "native_result": result,
            "tables": tables, "netlists": netlists, "waveforms": waveforms, "logs": logs, "tools": tools, "result_records": [], "points": points, "spec_results": spec_results,
            "overall_spec": ("passed" if all(item["status"] == "pass" for item in spec_results) else "failed") if spec_results else None,
            "spec_policy": "native_scalar_or_explicit_waveform_reduction", "native_output_semantics": "legacy_waveform_not_scalar",
            "fresh": True, "local_max_jobs": 1, "automatic_gui": False, "saved_plot_flags_preserved": True,
            "configuration": str(directory / "configuration/setting"), "netlist_format": "hspice"}
