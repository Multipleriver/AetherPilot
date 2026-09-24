"""Local, declarative Maestro-compatible state and result semantics."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contract import engineering, identifier, no_duplicate_keys
from .errors import OperationError, ValidationError
from .results import safe_result_path


MAESTRO_SCHEMA_VERSION = 1
MAESTRO_VIEW = "state"
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ANALYSES = {"op", "dc", "ac", "tran", "noise", "stb", "pss", "pnoise", "pac", "pxf", "hb"}
_EXPR_TYPES = {"Expression", "Signal", "OPParam"}
_EVAL_TYPES = {"Point", "Corners", "Sweeps", "All"}
_RUN_MODES = {"interactive", "batch", "local"}
_MAESTRO_ACTION_ALIASES = {
    "open_session": "open",
    "close_session": "close",
    "snapshot": "snapshot_state",
    "run_simulation": "run",
    "run_and_wait": "run",
    "create_netlist_for_corner": "create_netlist",
    "set_current_run_mode": "set_run_mode",
    "set_job_control_mode": "set_job_control",
}


def _analysis_number(value: Any, field: str) -> None:
    try:
        engineering(value)
    except ValidationError as exc:
        raise ValidationError(f"{field} must be a numeric engineering value", stage="plan", code="invalid_maestro_analysis") from exc


def _analysis_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    try:
        return _text(value, field, allow_empty=allow_empty)
    except ValidationError as exc:
        raise ValidationError(f"{field} must be a valid text value", stage="plan", code="invalid_maestro_analysis") from exc


def normalize_maestro_analysis_options(analysis: str, source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize the portable analysis option vocabulary."""
    options = _mapping(source, "options")
    analysis = analysis.lower()
    common = {"start", "stop", "step", "points", "sweep"}
    allowed = set(common)
    if analysis == "tran":
        allowed = {"stop", "accuracy", "start", "step", "use_initial_conditions"}
    elif analysis == "dc":
        allowed.update({"variable", "source", "parameter"})
    elif analysis == "noise":
        allowed.update({
            "output_mode", "output", "probe", "positive_output", "negative_output",
            "input_mode", "input_source", "input_voltage_source", "input_current_source",
            "input_port_source", "noise_separation",
        })
    elif analysis == "stb":
        allowed.update({"probe", "mode_type", "mode_type_enabled", "local_ground"})
    elif analysis == "op":
        allowed = set()
    if set(options) - allowed:
        raise ValidationError("Unsupported analysis option", stage="plan", code="invalid_maestro_analysis",
                              details={"analysis": analysis, "options": sorted(set(options) - allowed)})
    result = dict(options)
    if analysis == "op":
        return result
    if analysis == "tran":
        if "stop" not in result:
            raise ValidationError("Transient requires a stop", stage="plan", code="invalid_maestro_analysis")
        _analysis_number(result["stop"], "stop")
        if engineering(result["stop"]) <= 0:
            raise ValidationError("Transient stop must be positive", stage="plan", code="invalid_maestro_analysis")
        if "start" in result:
            _analysis_number(result["start"], "start")
            if not 0 <= engineering(result["start"]) < engineering(result["stop"]):
                raise ValidationError("Transient start must precede stop", stage="plan", code="invalid_maestro_analysis")
        if "step" in result:
            _analysis_number(result["step"], "step")
            if not 0 < engineering(result["step"]) <= engineering(result["stop"]) - engineering(result.get("start", 0)):
                raise ValidationError("Transient step is outside its interval", stage="plan", code="invalid_maestro_analysis")
        if "use_initial_conditions" in result and type(result["use_initial_conditions"]) is not bool:
            raise ValidationError("Transient initial-condition flag must be boolean", stage="plan", code="invalid_maestro_analysis")
        if "accuracy" in result:
            result["accuracy"] = _analysis_text(result["accuracy"], "accuracy")
            if result["accuracy"] not in {"conservative", "moderate", "liberal"}:
                raise ValidationError("Invalid accuracy level", stage="plan", code="invalid_maestro_analysis")
        return result
    if "start" not in result or "stop" not in result:
        raise ValidationError("Sweep requires start and stop", stage="plan", code="invalid_maestro_analysis")
    for field in ("start", "stop"):
        _analysis_number(result[field], field)
    if engineering(result["stop"]) <= engineering(result["start"]):
        raise ValidationError("Sweep limits must increase", stage="plan", code="invalid_maestro_analysis")
    if "step" in result and "points" in result:
        raise ValidationError("Sweep resolution must be unambiguous", stage="plan", code="invalid_maestro_analysis")
    sweep = result.get("sweep", "linear")
    result["sweep"] = _analysis_text(sweep, "sweep").lower()
    if result["sweep"] not in {"linear", "decade"}:
        raise ValidationError("Unsupported sweep type", stage="plan", code="invalid_maestro_analysis")
    if "step" in result:
        _analysis_number(result["step"], "step")
        if engineering(result["step"]) <= 0:
            raise ValidationError("Sweep step must be positive", stage="plan", code="invalid_maestro_analysis")
    if "points" in result:
        if type(result["points"]) is not int or not 1 <= result["points"] <= 100001:
            raise ValidationError("Sweep points are outside the local bound", stage="plan", code="invalid_maestro_analysis")
    if "step" not in result and "points" not in result:
        raise ValidationError("Sweep requires step or points", stage="plan", code="invalid_maestro_analysis")
    if result["sweep"] == "decade":
        if engineering(result["start"]) <= 0 or "points" not in result or result["points"] > 10000:
            raise ValidationError("Log sweep requires positive start and bounded points", stage="plan", code="invalid_maestro_analysis")
    elif "points" in result and result["points"] < 2:
        raise ValidationError("Linear sweep requires at least two points", stage="plan", code="invalid_maestro_analysis")
    if analysis == "dc":
        has_source = bool(result.get("source"))
        has_variable = bool(result.get("variable"))
        if has_source == has_variable:
            raise ValidationError("DC requires exactly one source or variable", stage="plan", code="invalid_maestro_analysis")
        for field in ("source", "variable"):
            if field in result:
                _analysis_text(result[field], field)
        if has_source:
            result.setdefault("parameter", "dc")
            _analysis_text(result["parameter"], "parameter")
        elif "parameter" in result:
            raise ValidationError("DC component parameter requires a source", stage="plan", code="invalid_maestro_analysis")
    elif analysis == "noise":
        output_mode = result.get("output_mode")
        if output_mode is None:
            output_mode = "probe" if result.get("probe") else "voltage"
        output_mode = _analysis_text(output_mode, "output_mode").lower()
        if output_mode not in {"probe", "voltage"}:
            raise ValidationError("Noise output_mode must be probe or voltage", stage="plan", code="invalid_maestro_analysis")
        result["output_mode"] = output_mode
        if "output" in result:
            if "positive_output" in result and result["positive_output"] != result["output"]:
                raise ValidationError("Noise output aliases disagree", stage="plan", code="invalid_maestro_analysis")
            result["positive_output"] = result["output"]
        if output_mode == "probe":
            if not result.get("probe"):
                raise ValidationError("Probe noise output requires probe", stage="plan", code="invalid_maestro_analysis")
            _analysis_text(result["probe"], "probe")
            if any(field in result for field in ("positive_output", "negative_output")):
                raise ValidationError("Probe noise output cannot define voltage nodes", stage="plan", code="invalid_maestro_analysis")
        else:
            if not result.get("positive_output"):
                raise ValidationError("Voltage noise output requires positive_output", stage="plan", code="invalid_maestro_analysis")
            result.setdefault("negative_output", "0")
            _analysis_text(result["positive_output"], "positive_output")
            _analysis_text(result["negative_output"], "negative_output")
            if "probe" in result:
                raise ValidationError("Voltage noise output cannot define probe", stage="plan", code="invalid_maestro_analysis")
        input_mode = result.get("input_mode")
        if input_mode is None:
            input_mode = "voltage" if any(field in result for field in ("input_source", "input_voltage_source")) else "none"
        input_mode = _analysis_text(input_mode, "input_mode").lower()
        if input_mode not in {"voltage", "current", "port", "none"}:
            raise ValidationError("Noise input_mode is invalid", stage="plan", code="invalid_maestro_analysis")
        result["input_mode"] = input_mode
        aliases = {"input_source": "input_voltage_source"}
        for alias, target in aliases.items():
            if alias in result:
                if target in result and result[target] != result[alias]:
                    raise ValidationError("Noise input source aliases disagree", stage="plan", code="invalid_maestro_analysis")
                result[target] = result[alias]
                result.pop(alias)
        result.pop("output", None)
        source_fields = {
            "voltage": "input_voltage_source",
            "current": "input_current_source",
            "port": "input_port_source",
        }
        selected_source = source_fields.get(input_mode)
        for field in source_fields.values():
            if field in result:
                _analysis_text(result[field], field)
                if field != selected_source:
                    raise ValidationError("Noise input source does not match input_mode", stage="plan", code="invalid_maestro_analysis")
        if selected_source is None:
            if any(field in result for field in source_fields.values()):
                raise ValidationError("Noise input sources require a matching input_mode", stage="plan", code="invalid_maestro_analysis")
        elif not result.get(selected_source):
            raise ValidationError("Noise input mode requires its source", stage="plan", code="invalid_maestro_analysis")
        if "noise_separation" in result:
            result["noise_separation"] = _bool(result["noise_separation"], "noise_separation")
        else:
            result["noise_separation"] = False
    elif analysis == "stb":
        if not result.get("probe"):
            raise ValidationError("STB requires probe", stage="plan", code="invalid_maestro_analysis")
        _analysis_text(result["probe"], "probe")
        mode_type = _analysis_text(result.get("mode_type", "common"), "mode_type").lower()
        if mode_type not in {"common", "differential"}:
            raise ValidationError("STB mode_type must be common or differential", stage="plan", code="invalid_maestro_analysis")
        result["mode_type"] = mode_type
        result["mode_type_enabled"] = _bool(result.get("mode_type_enabled", False), "mode_type_enabled")
        result["local_ground"] = _analysis_text(result.get("local_ground", ""), "local_ground", allow_empty=True)
    return result


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > 65536 or any(character in value for character in ("\0", "\r", "\n")):
        raise ValidationError(f"{field} must be a nonempty string", stage="plan", code="invalid_maestro_request")
    if value and field in {"test", "name", "corner", "library", "cell", "view", "design_library", "design_cell", "design_view"}:
        identifier(value)
    return value


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{field} must be a boolean", stage="plan", code="invalid_maestro_request")
    return value


def _mapping(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object", stage="plan", code="invalid_maestro_request")
    return value


def _number(value: Any, field: str) -> float | int:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValidationError(f"{field} must be finite numeric", stage="plan", code="invalid_maestro_request")
    return value


def _default_state(library: str, cell: str, view: str) -> dict[str, Any]:
    return {
        "schema_version": MAESTRO_SCHEMA_VERSION,
        "design": {"library": "", "cell": "", "view": "schematic"},
        "simulator": "spectre",
        "tests": {},
        "current_test": "",
        "analyses": {},
        "outputs": {},
        "specs": {},
        "variables": {},
        "parameters": {},
        "environment_options": {},
        "simulator_options": {},
        "corners": {"Nominal": {"enabled": True, "temperature": 27, "models": [], "variables": {}}},
        "run_mode": "interactive",
        "job_control": {"mode": "local", "policy": {"kind": "local", "max_jobs": 1}},
        "results": [],
        "metadata": {"state_view": view, "history": []},
    }


def _normalize_state(state: Mapping[str, Any], *, library: str | None = None, cell: str | None = None,
                     view: str = MAESTRO_VIEW) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        raise ValidationError("Maestro state must be an object", stage="maestro", code="invalid_maestro_state")
    result = copy.deepcopy(dict(state))
    if type(result.get("schema_version")) is not int or result.get("schema_version") != MAESTRO_SCHEMA_VERSION:
        raise ValidationError("Unsupported Maestro state schema", stage="maestro", code="invalid_maestro_state")
    result.setdefault("design", {"library": library or "", "cell": cell or "", "view": "schematic"})
    if not isinstance(result["design"], dict):
        raise ValidationError("Maestro state design must be an object", stage="maestro", code="invalid_maestro_state")
    result["design"].setdefault("library", "")
    result["design"].setdefault("cell", "")
    result["design"].setdefault("view", "schematic")
    result.setdefault("simulator", "spectre")
    for key in ("tests", "analyses", "outputs", "specs", "variables", "parameters", "environment_options", "simulator_options", "corners"):
        if not isinstance(result.get(key), dict):
            raise ValidationError(f"Maestro state field {key} must be an object", stage="maestro", code="invalid_maestro_state")
    if not isinstance(result.get("results"), list) or not isinstance(result.get("metadata"), dict):
        raise ValidationError("Maestro state result fields are malformed", stage="maestro", code="invalid_maestro_state")
    result["metadata"].setdefault("state_view", view)
    result["metadata"].setdefault("history", [])
    if not isinstance(result["metadata"]["history"], list) or len(result["metadata"]["history"]) > 100000:
        raise ValidationError("Maestro history is malformed or too large", stage="maestro", code="invalid_maestro_state")
    if len(result["tests"]) > 256 or len(result["corners"]) > 128 or len(result["results"]) > 10000:
        raise ValidationError("Maestro state exceeds the local budget", stage="maestro", code="invalid_maestro_state")
    for name, test in result["tests"].items():
        _text(name, "test")
        if not isinstance(test, dict) or not isinstance(test.get("design"), dict) or type(test.get("enabled")) is not bool:
            raise ValidationError("Maestro test is malformed", stage="maestro", code="invalid_maestro_state")
        for field in ("library", "cell", "view"):
            _text(test["design"].get(field), field)
        for field in ("analyses", "outputs", "specs", "variables", "environment_options", "simulator_options"):
            if not isinstance(test.get(field), dict):
                raise ValidationError("Maestro test settings are malformed", stage="maestro", code="invalid_maestro_state")
        for analysis_name, analysis in test["analyses"].items():
            if not isinstance(analysis, dict) or type(analysis.get("enabled")) is not bool:
                raise ValidationError("Maestro analysis is malformed", stage="maestro", code="invalid_maestro_state")
            validate_maestro_request({"action": "set_analysis", "test": name, "analysis": analysis_name,
                                      "enable": analysis["enabled"], "options": analysis.get("options", {})})
        for output_name, output in test["outputs"].items():
            if not isinstance(output, dict) or output.get("name") != output_name:
                raise ValidationError("Maestro output is malformed", stage="maestro", code="invalid_maestro_state")
            validate_maestro_request({**output, "action": "add_output", "test": name})
        for output_name, spec in test["specs"].items():
            if output_name not in test["outputs"] or not isinstance(spec, dict):
                raise ValidationError("Maestro specification target is missing", stage="maestro", code="invalid_maestro_state")
            validate_maestro_request({**spec, "action": "set_spec", "test": name, "name": output_name})
        for action, field in (("set_env_option", "environment_options"), ("set_sim_option", "simulator_options")):
            validate_maestro_request({"action": action, "test": name, "options": test[field]})
    if result.get("current_test", "") and result["current_test"] not in result["tests"]:
        raise ValidationError("Selected Maestro test is missing", stage="maestro", code="invalid_maestro_state")
    validate_maestro_request({"action": "set_run_mode", "run_mode": result.get("run_mode")})
    job_control = _mapping(result.get("job_control"), "job_control")
    validate_maestro_request({"action": "set_job_control", "mode": job_control.get("mode")})
    validate_maestro_request({"action": "set_job_policy", "policy": job_control.get("policy")})
    for name, corner in result["corners"].items():
        validate_maestro_request({**corner, "name": name, "action": "setup_corner"}) if isinstance(corner, dict) else _mapping(corner, "corner")
    for variables in (result["variables"], result["parameters"], *[test["variables"] for test in result["tests"].values()]):
        if len(variables) > 10000:
            raise ValidationError("Maestro variables exceed their budget", stage="maestro", code="invalid_maestro_state")
        for name, value in variables.items():
            _text(name, "name")
            _mapping(value, "variable")
            _text(value.get("value"), "value")
    _canonical(result)
    return result


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValidationError("Maestro object keys must be strings", stage="maestro", code="invalid_maestro_state")
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("Maestro state contains non-finite number", stage="maestro", code="invalid_maestro_state")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValidationError("Maestro state contains a non-JSON value", stage="maestro", code="invalid_maestro_state")
    return value


def state_digest(state: Mapping[str, Any]) -> str:
    encoded = json.dumps(_canonical(state), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_maestro_request(source: Any, *, operation: str | None = None) -> dict[str, Any]:
    """Validate one local writer/reader action without executing native code."""
    source = _mapping(source, "source")
    action = operation.rsplit(".", 1)[-1] if operation and operation.startswith("maestro.") else source.get("action")
    action = _text(action, "action")
    if not _ACTION_RE.fullmatch(action):
        raise ValidationError("Invalid Maestro action", stage="plan", code="invalid_maestro_request")
    requested_action = action
    action = _MAESTRO_ACTION_ALIASES.get(action, action)
    allowed = {
        "create_test", "set_design", "set_analysis", "add_output", "set_spec", "set_var", "get_var", "delete_var",
        "get_parameter", "set_parameter", "set_env_option", "set_sim_option", "set_corner", "setup_corner",
        "load_corners", "set_run_mode", "set_job_control", "set_job_policy", "save_setup", "create_netlist",
        "run", "read_results", "export_waveform", "open", "close", "read_state", "snapshot_state",
        "export_output_view", "write_script", "restore_setup", "recover", "find_open_session", "migrate_setup",
        "job_status", "job_wait", "job_cancel", "set_test_enabled", "delete_test", "delete_output",
        "delete_corner", "clear_results",
        "capabilities",
    }
    if action not in allowed:
        raise ValidationError("Unsupported Maestro action", stage="plan", code="unsupported_maestro_action", details={"action": action})
    result = dict(source)
    result["action"] = action
    if requested_action != action:
        result["requested_action"] = requested_action
    for field in ("library", "cell"):
        if field in result:
            _text(result[field], field)
    if "view" in result:
        _text(result["view"], "view")
    if action in {"create_test", "set_design", "open", "close", "read_state", "snapshot_state", "save_setup", "create_netlist", "run", "read_results", "export_waveform"}:
        result.setdefault("view", MAESTRO_VIEW)
    if action == "migrate_setup":
        source_target = _mapping(result.get("source_target"), "source_target")
        from .contract import identifier

        if set(source_target) != {"library", "cell", "view"}:
            raise ValidationError("Migration needs an exact native source target", stage="plan", code="invalid_maestro_request")
        for field, value in source_target.items():
            identifier(value, "source_target." + field)
        result["source_target"] = source_target
        result["test"] = _text(result.get("test", "imported"), "test")
    elif action == "restore_setup":
        result["setup"] = _normalize_state(result.get("setup"))
        if not result["setup"].get("current_test"):
            raise ValidationError("Imported setup needs a selected test", stage="plan", code="test_missing")
    elif action == "recover":
        if result.get("policy", "rollback") != "rollback":
            raise ValidationError("Only verified rollback recovery is supported", stage="plan", code="invalid_maestro_request")
    elif action in {"write_script", "export_output_view"}:
        _text(result.get("output"), "output")
        if action == "export_output_view":
            result["table_view"] = _text(result.get("table_view", "detail"), "table_view").lower()
            if result["table_view"] not in {"detail", "summary", "transpose", "yield"}:
                raise ValidationError("Unsupported result table view", stage="plan", code="invalid_maestro_request")
    elif action == "create_test":
        _text(result.get("test"), "test")
        result.setdefault("design_library", result.get("library", ""))
        result.setdefault("design_cell", result.get("cell", ""))
        _text(result["design_library"], "design_library")
        _text(result["design_cell"], "design_cell")
        result.setdefault("design_view", "schematic")
        _text(result["design_view"], "design_view")
        result.setdefault("simulator", "spectre")
        _text(result["simulator"], "simulator")
    elif action == "set_test_enabled":
        _text(result.get("test"), "test")
        result["enabled"] = _bool(result.get("enabled", True), "enabled")
    elif action == "delete_test":
        _text(result.get("test"), "test")
    elif action == "delete_output":
        _text(result.get("test"), "test")
        _text(result.get("name"), "name")
    elif action == "delete_corner":
        _text(result.get("name"), "name")
    elif action == "clear_results":
        pass
    elif action == "capabilities":
        pass
    elif action == "set_design":
        _text(result.get("test"), "test")
        result.setdefault("design_library", result.get("library", ""))
        result.setdefault("design_cell", result.get("cell", ""))
        _text(result["design_library"], "design_library")
        _text(result["design_cell"], "design_cell")
        result.setdefault("design_view", "schematic")
        _text(result["design_view"], "design_view")
    elif action == "set_analysis":
        _text(result.get("test"), "test")
        analysis = _text(result.get("analysis"), "analysis").lower()
        if analysis not in _ANALYSES:
            raise ValidationError("Unsupported analysis", stage="plan", code="invalid_maestro_analysis")
        result["analysis"] = analysis
        result["enable"] = _bool(result.get("enable", True), "enable")
        result["options"] = normalize_maestro_analysis_options(analysis, result.get("options", {}))
    elif action == "add_output":
        _text(result.get("name"), "name")
        _text(result.get("test"), "test")
        result.setdefault("output_type", "")
        result.setdefault("signal_name", "")
        result.setdefault("expr", "")
        for field in ("output_type", "signal_name", "expr"):
            _text(result[field], field, allow_empty=True)
        if not result["signal_name"] and not result["expr"]:
            raise ValidationError("An output requires a signal or expression", stage="plan", code="invalid_maestro_request")
        if result["signal_name"] and result["expr"]:
            raise ValidationError("An output cannot define both a signal and expression", stage="plan", code="invalid_maestro_request")
        result["output_type"] = result["output_type"] or ("Signal" if result["signal_name"] else "Expression")
        result.setdefault("eval_type", "Point")
        if result["output_type"] not in _EXPR_TYPES or result["eval_type"] not in _EVAL_TYPES:
            raise ValidationError("Invalid output or evaluation type", stage="plan", code="invalid_maestro_request")
        for field in ("save", "plot"):
            result[field] = _bool(result.get(field, True), field)
    elif action == "set_spec":
        _text(result.get("name"), "name")
        _text(result.get("test"), "test")
        for field in ("lt", "gt"):
            result.setdefault(field, "")
            _text(result[field], field, allow_empty=True)
            if result[field]:
                engineering(result[field])
        if result["lt"] and result["gt"] and engineering(result["gt"]) >= engineering(result["lt"]):
            raise ValidationError("Output specification limits are reversed", stage="plan", code="invalid_maestro_request")
        if any(field in result for field in ("analysis", "reduction", "component")):
            if result.get("analysis") not in _ANALYSES or result.get("reduction") not in {"op_first", "point_last", "min", "max", "mean", "rms"}:
                raise ValidationError("Waveform specs require explicit analysis and a supported reduction", stage="plan", code="invalid_maestro_request")
            if not result["lt"] and not result["gt"]:
                raise ValidationError("Waveform reduction requires a specification", stage="plan", code="invalid_maestro_request")
            if result.get("component") not in {None, "real", "imag", "magnitude"}:
                raise ValidationError("Invalid complex waveform component", stage="plan", code="invalid_maestro_request")
    elif action in {"set_var", "get_var", "delete_var"}:
        _text(result.get("name"), "name")
        if action == "set_var":
            _text(result.get("value"), "value", allow_empty=True)
            _text(result.get("type_name", ""), "type_name", allow_empty=True)
            _text(result.get("type_value", ""), "type_value", allow_empty=True)
    elif action in {"get_parameter", "set_parameter"}:
        _text(result.get("name"), "name")
        if action == "set_parameter":
            _text(result.get("value"), "value", allow_empty=True)
    elif action in {"set_env_option", "set_sim_option"}:
        _text(result.get("test"), "test")
        options = _mapping(result.get("options"), "options")
        for name, value in options.items():
            _text(name, "option_name")
            if type(value) not in (str, bool, int, float):
                raise ValidationError("Options must contain scalar values", stage="plan", code="invalid_maestro_request")
            if type(value) is str:
                _text(value, "option_value", allow_empty=True)
            elif type(value) is not bool:
                _number(value, "option_value")
    elif action in {"set_corner", "setup_corner"}:
        _text(result.get("name"), "name")
        if "enabled" in result:
            _bool(result["enabled"], "enabled")
        if "temperature" in result:
            _number(result["temperature"], "temperature")
        if "models" in result:
            if not isinstance(result["models"], list) or any(not isinstance(item, dict) for item in result["models"]):
                raise ValidationError("models must be a list of objects", stage="plan", code="invalid_maestro_request")
        if "variables" in result:
            _mapping(result["variables"], "variables")
            for name, value in result["variables"].items():
                _text(name, "name")
                _text(value, "value")
        for model in result.get("models", []):
            if set(model) != {"path", "section"}:
                raise ValidationError("Models require path and section", stage="plan", code="invalid_maestro_request")
            _text(model["path"], "path")
            _text(model["section"], "section", allow_empty=True)
    elif action == "load_corners":
        _text(result.get("path"), "path")
    elif action == "set_run_mode":
        result["run_mode"] = _text(result.get("run_mode"), "run_mode").lower()
        if result["run_mode"] not in _RUN_MODES:
            raise ValidationError("Unsupported run mode", stage="plan", code="invalid_maestro_request")
    elif action == "set_job_control":
        _text(result.get("mode"), "mode")
        if result["mode"] != "local":
            raise ValidationError("Only local job control is supported", stage="plan", code="remote_deferred")
    elif action == "set_job_policy":
        policy = _mapping(result.get("policy"), "policy")
        if set(policy) - {"kind", "max_jobs"} or policy.get("kind", "local") != "local":
            raise ValidationError("Unsupported job policy", stage="plan", code="invalid_maestro_request")
        if "max_jobs" in policy:
            value = policy["max_jobs"]
            if type(value) is not int or value != 1:
                raise ValidationError("Local acceptance currently requires max_jobs=1", stage="plan", code="invalid_maestro_request")
        result["policy"] = {"kind": "local", "max_jobs": 1}
    elif action in {"create_netlist", "export_waveform"}:
        if action == "create_netlist":
            result.setdefault("test", "")
            _text(result["test"], "test", allow_empty=True)
        else:
            _text(result.get("expression"), "expression")
            _text(result.get("output"), "output")
            result.setdefault("analysis", "ac")
            _text(result["analysis"], "analysis")
            if result["analysis"] not in _ANALYSES:
                raise ValidationError("Unknown waveform analysis", stage="plan", code="invalid_maestro_analysis")
    elif action == "run":
        result.setdefault("test", "")
        _text(result["test"], "test", allow_empty=True)
        if requested_action == "run_simulation":
            result.setdefault("wait", False)
        if requested_action == "run_and_wait":
            result.setdefault("wait", True)
        if "wait" in result:
            _bool(result["wait"], "wait")
            if not result["wait"] and requested_action != "run_simulation":
                raise ValidationError("Detached native MDE execution requires a durable job controller", stage="plan", code="async_execution_unavailable")
        if "callback" in result:
            _text(result["callback"], "callback", allow_empty=True)
        if "timeout" in result:
            _number(result["timeout"], "timeout")
            if engineering(result["timeout"]) <= 0:
                raise ValidationError("Maestro run timeout must be positive", stage="plan", code="invalid_maestro_request")
    elif action in {"job_status", "job_wait", "job_cancel"}:
        _text(result.get("job_id"), "job_id")
        if action == "job_wait":
            result.setdefault("timeout", 600)
            _number(result["timeout"], "timeout")
            if engineering(result["timeout"]) <= 0:
                raise ValidationError("Maestro job wait timeout must be positive", stage="plan", code="invalid_job_timeout")
    elif action == "read_results":
        if "history" in result:
            _text(result["history"], "history", allow_empty=True)
    elif action == "open":
        result["persistent"] = _bool(result.get("persistent", False), "persistent")
    elif action in {"close", "find_open_session", "read_state"}:
        if "session_id" in result:
            _text(result["session_id"], "session_id", allow_empty=True)
            if result["session_id"] and (not re.fullmatch(r"[0-9]{1,10}", result["session_id"]) or int(result["session_id"]) > 2147483647):
                raise ValidationError("Native MDE session id must be a bounded nonnegative integer string", stage="plan", code="invalid_session_id")
    for field in ("test", "corner", "history"):
        if field in result:
            _text(result[field], field, allow_empty=True)
    for field in ("path", "output", "output_dir"):
        if field in result:
            _text(result[field], field)
            result[field] = str(Path(result[field]).expanduser().absolute())
    return result


def _test(state: dict[str, Any], name: str) -> dict[str, Any]:
    test = state["tests"].get(name)
    if test is None:
        raise OperationError("Maestro test does not exist", stage="maestro", code="test_missing", details={"test": name})
    return test


def apply_maestro_action(state: Mapping[str, Any], request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a validated writer action to a state and return state plus readback."""
    current = _normalize_state(state, library=request.get("library"), cell=request.get("cell"), view=request.get("view", MAESTRO_VIEW))
    source = validate_maestro_request(request)
    action = source["action"]
    if action == "restore_setup":
        from .maestro_exports import portable_setup

        current = portable_setup(source["setup"])
        current["metadata"]["state_view"] = source.get("view", MAESTRO_VIEW)
    elif action == "create_test":
        name = source["test"]
        if name in current["tests"]:
            raise OperationError("Maestro test already exists", stage="maestro", code="test_exists", details={"test": name})
        current["tests"][name] = {"name": name, "design": {"library": source["design_library"], "cell": source["design_cell"], "view": source["design_view"]},
                                   "simulator": source["simulator"], "analyses": {}, "outputs": {}, "specs": {}, "variables": {},
                                   "environment_options": {}, "simulator_options": {}, "enabled": True}
        current["current_test"] = name
        current["design"] = copy.deepcopy(current["tests"][name]["design"])
    elif action == "set_test_enabled":
        _test(current, source["test"])["enabled"] = source["enabled"]
    elif action == "delete_test":
        _test(current, source["test"])
        if len(current["tests"]) <= 1:
            raise OperationError("Maestro requires at least one test", stage="maestro", code="last_test_delete")
        del current["tests"][source["test"]]
        if current["current_test"] == source["test"]:
            current["current_test"] = sorted(current["tests"])[0]
            current["design"] = copy.deepcopy(current["tests"][current["current_test"]]["design"])
    elif action == "set_design":
        test = _test(current, source["test"])
        test["design"] = {"library": source["design_library"], "cell": source["design_cell"], "view": source["design_view"]}
        current["design"] = copy.deepcopy(test["design"])
    elif action == "set_analysis":
        test = _test(current, source["test"])
        test["analyses"][source["analysis"]] = {"enabled": source["enable"], "options": copy.deepcopy(source["options"])}
        current["analyses"][source["analysis"]] = copy.deepcopy(test["analyses"][source["analysis"]])
    elif action == "add_output":
        test = _test(current, source["test"])
        output = {"name": source["name"], "output_type": source["output_type"], "signal_name": source["signal_name"], "expr": source["expr"],
                  "save": source["save"], "plot": source["plot"], "eval_type": source["eval_type"]}
        test["outputs"][source["name"]] = output
        current["outputs"][source["name"]] = copy.deepcopy(output)
    elif action == "delete_output":
        test = _test(current, source["test"])
        if source["name"] not in test["outputs"]:
            raise OperationError("Output does not exist", stage="maestro", code="output_missing")
        del test["outputs"][source["name"]]
        test["specs"].pop(source["name"], None)
        current["outputs"].pop(source["name"], None)
        current["specs"].pop(source["name"], None)
    elif action == "set_spec":
        test = _test(current, source["test"])
        if source["name"] not in test["outputs"]:
            raise OperationError("Output does not exist", stage="maestro", code="output_missing")
        spec = {"lt": source["lt"], "gt": source["gt"]}
        spec.update({field: source[field] for field in ("analysis", "reduction", "component") if field in source})
        test["specs"][source["name"]] = spec
        current["specs"][source["name"]] = copy.deepcopy(spec)
    elif action in {"set_var", "get_var", "delete_var"}:
        if action == "set_var":
            value = {"value": source["value"], "type_name": source.get("type_name", ""), "type_value": source.get("type_value", "")}
            test_name = source.get("test")
            if test_name:
                _test(current, test_name)["variables"][source["name"]] = copy.deepcopy(value)
            else:
                current["variables"][source["name"]] = value
        elif action == "delete_var":
            variables = _test(current, source["test"])["variables"] if source.get("test") else current["variables"]
            variables.pop(source["name"], None)
        elif source["name"] not in (_test(current, source["test"])["variables"] if source.get("test") else current["variables"]):
            raise OperationError("Maestro variable does not exist", stage="maestro", code="variable_missing", details={"name": source["name"]})
    elif action in {"get_parameter", "set_parameter"}:
        if action == "set_parameter":
            current["parameters"][source["name"]] = {"value": source["value"], "enabled": True}
        elif source["name"] not in current["parameters"]:
            raise OperationError("Maestro parameter does not exist", stage="maestro", code="parameter_missing", details={"name": source["name"]})
    elif action in {"set_env_option", "set_sim_option"}:
        field = "environment_options" if action == "set_env_option" else "simulator_options"
        test = _test(current, source["test"])
        test[field].update(source["options"])
    elif action in {"set_corner", "setup_corner"}:
        corner = current["corners"].setdefault(source["name"], {"enabled": True, "temperature": 27, "models": [], "variables": {}})
        corner.update({key: copy.deepcopy(source[key]) for key in ("enabled", "temperature", "models", "variables") if key in source})
    elif action == "delete_corner":
        if source["name"] not in current["corners"]:
            raise OperationError("Maestro corner does not exist", stage="maestro", code="corner_missing")
        if len(current["corners"]) <= 1:
            raise OperationError("Maestro requires at least one corner", stage="maestro", code="last_corner_delete")
        del current["corners"][source["name"]]
    elif action == "clear_results":
        current["results"] = []
        current["metadata"]["history"] = []
    elif action == "load_corners":
        path = Path(source["path"])
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError) as exc:
            raise ValidationError("Corner file is not readable JSON", stage="maestro", code="invalid_corner_file") from exc
        if not isinstance(loaded, dict):
            raise ValidationError("Corner file must contain an object", stage="maestro", code="invalid_corner_file")
        for name, corner in loaded.items():
            if not isinstance(corner, dict):
                raise ValidationError("Corner definition must be an object", stage="maestro", code="invalid_corner_file")
            checked = validate_maestro_request({**corner, "action": "setup_corner", "name": name})
            current["corners"][name] = {"enabled": checked.get("enabled", True), "temperature": checked.get("temperature", 27), "models": checked.get("models", []), "variables": checked.get("variables", {})}
    elif action == "set_run_mode":
        current["run_mode"] = source["run_mode"]
    elif action == "set_job_control":
        current["job_control"]["mode"] = source["mode"]
    elif action == "set_job_policy":
        current["job_control"]["policy"] = copy.deepcopy(source["policy"])
    elif action == "save_setup":
        pass
    elif action in {"create_netlist", "run", "read_results", "export_waveform", "open", "close", "read_state", "snapshot_state"}:
        pass
    if source.get("test") and action not in {"get_var", "get_parameter", "delete_test"}:
        selected = _test(current, source["test"])
        current["current_test"] = source["test"]
        current["design"] = copy.deepcopy(selected["design"])
        current["simulator"] = selected["simulator"]
        for key in ("analyses", "outputs", "specs", "environment_options", "simulator_options"):
            current[key] = copy.deepcopy(selected[key])
    current["metadata"]["last_action"] = action
    current["metadata"]["history"].append({"action": action, "request": copy.deepcopy(source), "state_sha256": state_digest(current)})
    return current, maestro_readback(current, action=action)


def maestro_readback(state: Mapping[str, Any], *, action: str | None = None, native: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current = _normalize_state(state)
    result = {
        "schema_version": MAESTRO_SCHEMA_VERSION,
        "action": action,
        "design": copy.deepcopy(current["design"]),
        "simulator": current["simulator"],
        "current_test": current["current_test"],
        "tests": copy.deepcopy(current["tests"]),
        "analyses": copy.deepcopy(current["analyses"]),
        "outputs": copy.deepcopy(current["outputs"]),
        "specs": copy.deepcopy(current["specs"]),
        "variables": copy.deepcopy(current["variables"]),
        "parameters": copy.deepcopy(current["parameters"]),
        "corners": copy.deepcopy(current["corners"]),
        "environment_options": copy.deepcopy(current["environment_options"]),
        "simulator_options": copy.deepcopy(current["simulator_options"]),
        "job_control": copy.deepcopy(current["job_control"]),
        "metadata": copy.deepcopy(current["metadata"]),
        "run_mode": current["run_mode"],
        "job_control": copy.deepcopy(current["job_control"]),
        "results": copy.deepcopy(current["results"]),
        "state_sha256": state_digest(current),
    }
    if native is not None:
        result["native"] = copy.deepcopy(dict(native))
    return result


@dataclass(frozen=True)
class MaestroResult:
    name: str
    valid: bool
    path: str | None = None
    errors: tuple[str, ...] = ()
    files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "valid": self.valid, "path": self.path, "errors": list(self.errors), "files": list(self.files)}


class MaestroStore:
    """Atomic local persistence for a Maestro state view."""

    def __init__(self, root: str | Path, library: str, cell: str, view: str = MAESTRO_VIEW):
        self.root = Path(root).expanduser().resolve()
        self.library = _text(library, "library")
        self.cell = _text(cell, "cell")
        self.view = _text(view, "view")
        self.directory = self.root / self.cell / self.view
        self.path = self.directory / "aetherpilot-maestro.json"

    def load(self) -> dict[str, Any]:
        safe_result_path(self.path, must_exist=False)
        if not self.path.exists():
            return _default_state(self.library, self.cell, self.view)
        if not self.path.is_file() or self.path.stat().st_size > 16 * 1024 ** 2:
            raise OperationError("Maestro state is too large or unsafe", stage="maestro", code="state_corrupt")
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)
        except (OSError, ValueError) as exc:
            raise OperationError("Maestro state is unreadable", stage="maestro", code="state_corrupt") from exc
        return _normalize_state(state, library=self.library, cell=self.cell, view=self.view)

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _normalize_state(state, library=self.library, cell=self.cell, view=self.view)
        safe_result_path(self.path, must_exist=False)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".maestro-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(_canonical(normalized), handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return normalized

    def digest(self) -> str:
        return state_digest(self.load())


def parse_result_csv(path: str | Path, *, history: str = "") -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValidationError("Result CSV is missing or unsafe", stage="results", code="result_missing")
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValidationError("Result CSV is unreadable", stage="results", code="result_invalid") from exc
    if not rows or not rows[0] or any(not isinstance(item, str) or not item for item in rows[0]):
        raise ValidationError("Result CSV has no complete header", stage="results", code="result_invalid")
    columns = rows[0]
    values = []
    for index, row in enumerate(rows[1:], start=2):
        if len(row) != len(columns):
            raise ValidationError("Result CSV row width differs from header", stage="results", code="result_invalid", details={"line": index})
        parsed = []
        for item in row:
            try:
                parsed.append(float(item))
            except ValueError:
                parsed.append(item)
        values.append(parsed)
    return {"schema_version": 1, "kind": "maestro_result", "history": history, "source": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            "columns": columns, "rows": values}


def normalize_native_result(value: Any, *, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Convert an Aether MDE result object into a JSON-safe evidence record."""
    name = ""
    valid = False
    errors: list[str] = []
    if value is not None:
        for attr in ("name", "isValid", "lastError"):
            try:
                member = getattr(value, attr)
                got = member() if callable(member) else member
                if attr == "name":
                    name = str(got or "")
                elif attr == "isValid":
                    valid = bool(got)
                elif got:
                    errors.append(str(got))
            except Exception as exc:
                errors.append(f"{attr}:{type(exc).__name__}")
    files: list[str] = []
    if output_dir is not None:
        root = Path(output_dir)
        if root.is_dir():
            files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
    return MaestroResult(name=name, valid=valid, path=str(output_dir) if output_dir else None, errors=tuple(errors), files=tuple(files)).to_dict()


def local_waveform_result(path: str | Path, *, analysis: str = "ac", history: str = "") -> dict[str, Any]:
    result = parse_result_csv(path, history=history)
    result["analysis"] = analysis.lower()
    return result


__all__ = [
    "MAESTRO_SCHEMA_VERSION", "MAESTRO_VIEW", "MaestroResult", "MaestroStore", "apply_maestro_action",
    "local_waveform_result", "maestro_readback", "normalize_native_result", "parse_result_csv", "state_digest",
    "validate_maestro_request",
]
