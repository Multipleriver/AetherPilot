"""Bounded HSPICE MDE configuration with native field-level verification."""

from __future__ import annotations

import math
import re
from pathlib import Path

from .contract import engineering, identifier
from .errors import ValidationError
from .maestro import normalize_maestro_analysis_options
from .results import safe_input_path, safe_result_path


SIMULATOR_DEFAULTS = {"relTol": "0.001", "absTol": "1e-9", "absv": "5e-6", "gmin": "1e-12", "tnom": "27",
                      "scale": "1", "scalem": "1", "itl1": "200", "itl4": "8", "post": "true", "probe": "true"}
SIMULATOR_OPTIONS = set(SIMULATOR_DEFAULTS) | {"method", "postlvl", "posttop", "ingold"}
_CANONICAL_SECTIONS = {
    "Version", "Other", "Design", "Analysis", "Outputs", "Corner", "Parameter",
    "ModelSetup", "CornerSetup", "SimulatorOptions",
}
_SIGNAL = re.compile(r"[vViI]\(\s*([A-Za-z0-9_!./<>:$*+-]+)\s*\)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_!./<>:$+-]*")


def _unsupported(message, **details):
    return ValidationError(message, stage="maestro", code="native_hspice_unsupported", details=details)


def is_hspice(selected):
    return selected.get("simulator", "spectre").lower() in {"alps", "hspice"}


def empty_sections():
    return {"Version": {"Version": "1.0"}, "Other": {"netlistFormat": "0", "simulator": "Alps", "threadNumber": "1"},
            "Design": {"LibraryName": "", "CellName": "", "ViewName": "schematic"}, "Analysis": {"size": "0"},
            "Outputs": {"size": "0"}, "Corner": {"size": "1", "1\\CornerName": "Nominal", "1\\CornerTemperature": "27", "1\\isEnabled": "1"},
            "Parameter": {"size": "0"}, "ModelSetup": {"size": "0"}, "CornerSetup": {"size": "0"},
            "SimulatorOptions": dict(SIMULATOR_DEFAULTS)}


def _number(value):
    return format(engineering(value), ".17g")


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _unsupported("HSPICE identifiers must be unambiguous native names")
    return value


def _sweep(options, *, frequency=False):
    result = {"Range\\start": _number(options["start"]), "Range\\stop": _number(options["stop"]), "IsValid": "1"}
    start, stop = engineering(options["start"]), engineering(options["stop"])
    if options["sweep"] == "decade":
        if math.log10(stop / start) * options["points"] > 100000:
            raise _unsupported("HSPICE logarithmic sweep exceeds its point budget")
        result.update({"Range\\rangeType": "1", "Range\\name2": str(options["points"])})
    elif "step" in options:
        intervals = (stop - start) / engineering(options["step"])
        if not 1 <= intervals <= 100000:
            raise _unsupported("HSPICE linear sweep exceeds its point budget")
        if frequency:
            if not math.isclose(intervals, round(intervals), abs_tol=1e-9, rel_tol=1e-12):
                raise _unsupported("HSPICE AC step must divide the frequency interval exactly")
            result.update({"Range\\rangeType": "0", "Range\\linearType": "1", "Range\\name1": str(round(intervals) + 1)})
        else:
            result.update({"Range\\rangeType": "0", "Range\\linearType": "0", "Range\\name0": _number(options["step"])})
    elif frequency:
        result.update({"Range\\rangeType": "0", "Range\\linearType": "1", "Range\\name1": str(options["points"])})
    else:
        result.update({"Range\\rangeType": "0", "Range\\linearType": "0", "Range\\name0": _number((stop - start) / (options["points"] - 1))})
    return result


def analysis_fields(name, definition):
    options = normalize_maestro_analysis_options(name, definition["options"])
    fields = {"AnalysisTaskType": name.upper(), "IsEnabled": "1" if definition["enabled"] else "0"}
    if name == "op":
        return fields
    if name == "tran":
        stop = engineering(options["stop"])
        start = engineering(options.get("start", 0))
        step = engineering(options.get("step", stop / 1000))
        if (stop - start) / step > 100000:
            raise _unsupported("HSPICE transient exceeds its requested output point budget")
        fields.update({"Stop": _number(stop), "Start": _number(start), "Step": _number(step), "UseIC": "1" if options.get("use_initial_conditions", False) else "0",
                       "Errpreset": {"conservative": "0", "moderate": "1", "liberal": "2"}[options.get("accuracy", "moderate")], "Sweep\\size": "3"})
    elif name in {"dc", "ac"}:
        fields["Sweep\\size"] = "3" if name == "dc" else "2"
        sweep = _sweep(options, frequency=name == "ac")
        if name == "dc":
            variable = options.get("source", options.get("variable"))
            if options.get("parameter", "dc").lower() != "dc":
                raise _unsupported("HSPICE DC source sweep requires the dc parameter")
            _name(variable)
            sweep.update({"Var\\varType": "0" if variable.lower() == "temp" else "1", "Var\\name0": "temp", "Var\\name1": variable if variable.lower() != "temp" else ""})
        fields.update({"Sweep\\1\\" + key: value for key, value in sweep.items()})
    else:
        raise _unsupported("This native HSPICE analysis has not been verified", analysis=name)
    return fields


def output_fields(output):
    if output["output_type"] != "Signal" or output["eval_type"] != "Point":
        raise _unsupported("HSPICE native outputs currently require point-evaluated signals", output=output["name"])
    expression = output["signal_name"] or output["expr"]
    match = _SIGNAL.fullmatch(expression)
    if not match:
        raise _unsupported("HSPICE output must be a native voltage/current selector", output=output["name"])
    return {"outputNode": expression, "outputType": "Signal", "save": str(output["save"]).lower(), "plot": str(output["plot"]).lower()}


def project_sections(state, *, check_models=True):
    selected = state["tests"][state["current_test"]]
    if not is_hspice(selected) or any(not is_hspice(test) for test in state["tests"].values()):
        raise _unsupported("One native state must use one netlist dialect")
    sections = empty_sections()
    design = selected["design"]
    sections["Design"] = {field: design[key] for field, key in (("LibraryName", "library"), ("CellName", "cell"), ("ViewName", "view"))}
    sections["Analysis"] = {"size": str(len(selected["analyses"]))}
    for index, (name, definition) in enumerate(selected["analyses"].items(), 1):
        sections["Analysis"].update({str(index) + "\\" + key: value for key, value in analysis_fields(name, definition).items()})
    sections["Outputs"] = {"size": str(len(selected["outputs"]))}
    seen = set()
    for index, output in enumerate(selected["outputs"].values(), 1):
        fields = output_fields(output)
        if fields["outputNode"].casefold() in seen:
            raise _unsupported("HSPICE output selectors must be unique")
        seen.add(fields["outputNode"].casefold())
        sections["Outputs"].update({str(index) + "\\" + key: value for key, value in fields.items()})
    corners = state["corners"]
    for name in corners:
        identifier(name, "corner")
    if "Nominal" not in corners:
        raise _unsupported("The native nominal corner must remain explicitly represented")
    variables = {**state["parameters"], **state["variables"], **selected["variables"]}
    corner_index = 0
    for name, corner in corners.items():
        nominal = name == "Nominal"
        values = {**{key: item["value"] for key, item in variables.items()}, **corner["variables"]}
        if nominal:
            sections["Corner"] = {"size": "1", "1\\CornerName": name, "1\\CornerTemperature": _number(corner["temperature"]), "1\\isEnabled": "1" if corner["enabled"] else "0"}
            sections["ModelSetup"] = {"size": str(len(corner["models"]))}
            sections["Parameter"] = {"size": str(len(values))}
        else:
            corner_index += 1
            prefix = str(corner_index) + "\\"
            sections["CornerSetup"].update({prefix + "CornerName": name, prefix + "Temperature": _number(corner["temperature"]), prefix + "isEnabled": "1" if corner["enabled"] else "0",
                                            prefix + "MODEL\\size": str(len(corner["models"])), prefix + "PARAM\\size": str(len(values))})
        for index, model in enumerate(corner["models"], 1):
            if check_models:
                safe_input_path(model["path"])
            if nominal:
                prefix = str(index) + "\\"
                sections["ModelSetup"].update({prefix + "ModelFile": model["path"], prefix + "isEnabled": "true", prefix + "Corner\\size": "1",
                                              prefix + "Corner\\1\\name": "Nominal", prefix + "Corner\\1\\value": model["section"]})
            else:
                prefix = str(corner_index) + "\\MODEL\\" + str(index) + "\\"
                sections["CornerSetup"].update({prefix + "ModelFile": model["path"], prefix + "Sections": model["section"], prefix + "isModelEnabled": "1"})
        for index, (variable, value) in enumerate(sorted(values.items()), 1):
            _name(variable)
            _number(value)
            if nominal:
                prefix = str(index) + "\\"
                sections["Parameter"].update({prefix + "ParameterName": variable, prefix + "Corner\\size": "1", prefix + "Corner\\1\\name": "Nominal", prefix + "Corner\\1\\value": str(value)})
            else:
                prefix = str(corner_index) + "\\PARAM\\" + str(index) + "\\"
                sections["CornerSetup"].update({prefix + "ParamName": variable, prefix + "Value": str(value), prefix + "isParamEnabled": "1"})
    sections["CornerSetup"]["size"] = str(corner_index)
    environment = selected["environment_options"]
    fields = {"netlist_views": ("SimulatorOptions", "hspiceViewList"), "stop_views": ("SimulatorOptions", "hspiceStopList"), "project_directory": ("Other", "projectDirectory")}
    if set(environment) - fields.keys():
        raise _unsupported("Unsupported HSPICE environment option")
    for key, value in environment.items():
        section, field = fields[key]
        sections[section][field] = str(value)
    if set(selected["simulator_options"]) - SIMULATOR_OPTIONS:
        raise _unsupported("Unsupported HSPICE simulator option")
    for key, value in selected["simulator_options"].items():
        if key in {"post", "probe"}:
            if str(value).lower() not in {"true", "false", "1", "0"}:
                raise _unsupported("HSPICE simulator boolean is invalid", option=key)
            value = "true" if str(value).lower() in {"true", "1"} else "false"
        elif key == "method":
            if value not in {"trap", "gear"}:
                raise _unsupported("Unsupported HSPICE integration method")
        else:
            _number(value)
        sections["SimulatorOptions"][key] = str(value)
    opaque = state.get("metadata", {}).get("native_hspice_opaque_sections", {})
    if opaque:
        if not isinstance(opaque, dict):
            raise _unsupported("Legacy HSPICE opaque sections are malformed")
        for section, fields in opaque.items():
            if not isinstance(section, str) or not isinstance(fields, dict):
                raise _unsupported("Legacy HSPICE opaque sections are malformed")
            target = sections.setdefault(section, {})
            for key, value in fields.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise _unsupported("Legacy HSPICE opaque fields are malformed")
                target.setdefault(key, value)
    return sections


def opaque_sections(sections):
    """Return native sections not represented by the portable HSPICE model.

    Legacy MDE files contain simulator-specific sections such as convergence,
    stimulus and digital-output settings.  They are deliberately carried as
    opaque data instead of being guessed into the portable schema.  Canonical
    sections remain owned by the serializer so edits to known fields cannot be
    silently shadowed by stale legacy values.
    """
    if not isinstance(sections, dict):
        raise _unsupported("Legacy HSPICE sections are malformed")
    result = {}
    for section, fields in sections.items():
        if section in _CANONICAL_SECTIONS:
            continue
        if not isinstance(section, str) or not isinstance(fields, dict):
            raise _unsupported("Legacy HSPICE sections are malformed")
        result[section] = {str(key): str(value) for key, value in fields.items()}
    return result


def verify_sections(state, native):
    expected = project_sections(state)
    actual = native["setting_sections"]
    differences = []
    if native["netlist_format"] != 0:
        differences.append("netlist_format")
    for section, fields in expected.items():
        for field, value in fields.items():
            observed = actual.get(section, {}).get(field)
            if section in {"ModelSetup", "CornerSetup"} and field.endswith("\\ModelFile"):
                base = state.get("metadata", {}).get("native_hspice_model_base")
                try:
                    observed = str(safe_input_path(observed if not base or str(observed).startswith("/") else str(Path(base) / observed)).resolve())
                except ValidationError:
                    pass
                try:
                    value = str(safe_input_path(value).resolve())
                except ValidationError:
                    pass
            if observed == value:
                continue
            numeric = (section == "SimulatorOptions" and field not in {"method", "hspiceViewList", "hspiceStopList", "post", "probe"}
                       or field.endswith("Temperature") or section == "Parameter" and field.endswith("\\value") or section == "CornerSetup" and "\\PARAM\\" in field and field.endswith("\\Value")
                       or section == "Analysis" and (field.rsplit("\\", 1)[-1] in {"Start", "Stop", "Step"} or "\\Range\\" in field and field.rsplit("\\", 1)[-1] in {"start", "stop", "name0", "name1", "name2"}))
            try:
                equivalent = numeric and observed is not None and math.isclose(engineering(observed), engineering(value), rel_tol=1e-13, abs_tol=0)
            except (TypeError, ValueError, ValidationError):
                equivalent = False
            if not equivalent:
                differences.append(section + ":" + field)
    for section, notes in state.get("metadata", {}).get("native_notes", {}).items():
        if actual.get(section) != notes:
            differences.append("native_notes:" + section)
    if differences:
        raise ValidationError("Reopened native HSPICE settings differ from the request", stage="maestro", code="native_state_mismatch", details={"fields": differences})


def verify_legacy_semantics(state, native):
    """Verify an imported legacy HSPICE setting by meaning, not serializer defaults."""
    from .maestro_settings import native_analysis_entries

    selected = state["tests"][state["current_test"]]
    sections = native["setting_sections"]
    facts = native_fields(sections)
    differences = []

    if native.get("netlist_format") != 0:
        differences.append("netlist_format")
    expected_design = [selected["design"][key] for key in ("library", "cell", "view")]
    actual_design = [sections.get("Design", {}).get(key, "") for key in ("LibraryName", "CellName", "ViewName")]
    if actual_design != expected_design:
        differences.append("design")

    try:
        actual_analyses = native_analysis_entries(sections)
    except ValidationError:
        raise
    if set(actual_analyses) != set(selected["analyses"]):
        differences.append("analysis_names")
    for name, expected in selected["analyses"].items():
        actual = actual_analyses.get(name)
        if actual is None:
            continue
        imported = import_analysis(name, actual)
        if bool(imported["enabled"]) != bool(expected["enabled"]):
            differences.append("analysis_enabled:" + name)
        expected_options = normalize_maestro_analysis_options(name, expected["options"])
        actual_options = normalize_maestro_analysis_options(name, imported["options"])
        for key in sorted(set(expected_options) | set(actual_options)):
            if not _same_value(actual_options.get(key), expected_options.get(key)):
                differences.append("analysis:" + name + ":" + key)

    expected_outputs = list(selected["outputs"].values())
    if len(facts["outputs"]) != len(expected_outputs):
        differences.append("output_count")
    for index, expected in enumerate(expected_outputs):
        if index >= len(facts["outputs"]):
            continue
        actual = facts["outputs"][index]
        expected_expression = expected["signal_name"] or expected["expr"]
        if actual["expr"] != expected_expression:
            differences.append("output_expression:" + expected["name"])
        if actual["expr_type"] != expected["output_type"]:
            differences.append("output_type:" + expected["name"])
        if bool(actual["save"]) != bool(expected["save"]):
            differences.append("output_save:" + expected["name"])
        if bool(actual["plot"]) != bool(expected["plot"]):
            differences.append("output_plot:" + expected["name"])

    expected_corners = state["corners"]
    actual_corners = {corner["name"]: corner for corner in facts["corners"]}
    if set(actual_corners) != set(expected_corners):
        differences.append("corner_names")
    variables = {**state["parameters"], **state["variables"], **selected["variables"]}
    base = state.get("metadata", {}).get("native_hspice_model_base")
    for name, expected in expected_corners.items():
        actual = actual_corners.get(name)
        if actual is None:
            continue
        if bool(actual["enabled"]) != bool(expected["enabled"]):
            differences.append("corner_enabled:" + name)
        if not _same_value(actual["temperature"], expected["temperature"]):
            differences.append("corner_temperature:" + name)
        expected_models = {_model_identity(item["path"], item["section"], base) for item in expected["models"]}
        actual_models = {_model_identity(item["path"], item["section"], base) for item in actual["models"]}
        if actual_models != expected_models:
            differences.append("corner_models:" + name)
        expected_values = {key: item["value"] for key, item in variables.items()}
        expected_values.update(expected["variables"])
        actual_values = {item["name"]: item["value"] for item in actual["parameters"] if item["enabled"]}
        if set(actual_values) != set(expected_values):
            differences.append("corner_parameters:" + name)
        for parameter, value in expected_values.items():
            if not _same_value(actual_values.get(parameter), value):
                differences.append("parameter:" + name + ":" + parameter)

    simulator = sections.get("SimulatorOptions", {})
    for key, expected in selected["simulator_options"].items():
        actual = simulator.get(key, SIMULATOR_DEFAULTS.get(key))
        if not _same_value(actual, expected):
            differences.append("simulator_option:" + key)
    environment = selected["environment_options"]
    environment_actual = {
        "netlist_views": simulator.get("hspiceViewList"),
        "stop_views": simulator.get("hspiceStopList"),
        "project_directory": sections.get("Other", {}).get("projectDirectory"),
    }
    for key, expected in environment.items():
        if environment_actual.get(key) != expected:
            differences.append("environment_option:" + key)
    for section, notes in state.get("metadata", {}).get("native_notes", {}).items():
        if sections.get(section) != notes:
            differences.append("native_notes:" + section)
    if differences:
        raise ValidationError("Imported legacy HSPICE settings differ from the requested semantics", stage="maestro",
                              code="native_state_mismatch", details={"fields": differences})


def _same_value(actual, expected):
    if actual is None or expected is None:
        return actual == expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return str(actual).lower() == str(expected).lower()
    try:
        return math.isclose(engineering(actual), engineering(expected), rel_tol=1e-13, abs_tol=0)
    except (TypeError, ValueError, ValidationError):
        return str(actual) == str(expected)


def _model_identity(path, section, base):
    candidate = Path(str(path))
    if not candidate.is_absolute() and base:
        candidate = Path(base) / candidate
    try:
        candidate = safe_input_path(candidate).resolve()
    except ValidationError:
        candidate = Path(str(path))
    return str(candidate), str(section)


def _entries(section, *, maximum=10000):
    try:
        count = int(section.get("size", "0"))
        if not 0 <= count <= maximum:
            raise ValueError("native list budget exceeded")
    except (TypeError, ValueError) as error:
        raise ValidationError("Native HSPICE list is malformed", stage="maestro", code="state_corrupt") from error
    return [{field[len(str(index)) + 1:]: value for field, value in section.items() if field.startswith(str(index) + "\\")} for index in range(1, count + 1)]


def _boolean(value):
    if str(value).lower() not in {"0", "1", "true", "false"}:
        raise ValidationError("Native HSPICE boolean is malformed", stage="maestro", code="state_corrupt")
    return str(value).lower() in {"1", "true"}


def native_fields(sections):
    corners = []
    for corner in _entries(sections.get("Corner", {}), maximum=128):
        name = corner.get("CornerName", "")
        if not name or any(item["name"] == name for item in corners):
            raise _unsupported("Native HSPICE corners are missing or duplicated")
        corners.append({"name": name, "temperature": corner.get("CornerTemperature", ""), "enabled": _boolean(corner.get("isEnabled", "1")), "models": [], "parameters": []})
    by_name = {corner["name"]: corner for corner in corners}
    model_files = []
    for section_name, field, collection in (("ModelSetup", "ModelFile", "models"), ("Parameter", "ParameterName", "parameters")):
        for entry in _entries(sections.get(section_name, {})):
            if field not in entry:
                raise _unsupported("Native HSPICE entry lacks its identity", section=section_name)
            enabled = _boolean(entry.get("isEnabled", "true"))
            if collection == "models":
                model_files.append(entry[field])
            definitions = _entries({key[len("Corner\\"):]: value for key, value in entry.items() if key.startswith("Corner\\")}, maximum=128)
            seen = set()
            for definition in definitions:
                name = definition.get("name")
                if name not in by_name or name in seen or "value" not in definition:
                    raise _unsupported("Native HSPICE corner association is invalid", section=section_name)
                seen.add(name)
                value = definition["value"]
                item = {"path": entry[field], "section": value, "enabled": enabled} if collection == "models" else {"name": entry[field], "value": value, "enabled": enabled}
                by_name[name][collection].append(item)
    for entry in _entries(sections.get("CornerSetup", {}), maximum=127):
        name = entry.get("CornerName", "")
        if not name or name in by_name:
            raise _unsupported("Native additional corners are missing or duplicated")
        models = [{"path": model.get("ModelFile", ""), "section": model.get("Sections", ""), "enabled": _boolean(model.get("isModelEnabled", "1"))}
                  for model in _entries({key[len("MODEL\\"):]: value for key, value in entry.items() if key.startswith("MODEL\\")})]
        parameters = [{"name": parameter.get("ParamName", ""), "value": parameter.get("Value", ""), "enabled": _boolean(parameter.get("isParamEnabled", "1"))}
                      for parameter in _entries({key[len("PARAM\\"):]: value for key, value in entry.items() if key.startswith("PARAM\\")})]
        corner = {"name": name, "temperature": entry.get("Temperature", ""), "enabled": _boolean(entry.get("isEnabled", "1")), "models": models, "parameters": parameters}
        corners.append(corner)
        by_name[name] = corner
    outputs = []
    for entry in _entries(sections.get("Outputs", {})):
        expression = entry.get("outputNode", "")
        outputs.append({"name": expression, "expr": expression, "expr_type": entry.get("outputType", "Signal"), "eval_type": "Point", "spec": "",
                        "save": _boolean(entry.get("save", "false")), "plot": _boolean(entry.get("plot", "false"))})
    return {"corners": corners, "outputs": outputs, "model_files": model_files,
            "model_files_for_corners": sorted({model["path"] for corner in corners if corner["name"] != "Nominal" for model in corner["models"]}),
            "enabled_corners": [corner["name"] for corner in corners if corner["enabled"]], "native_output_aliases": False}


def native_number(value):
    text = str(value)
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(meg|[tgkmunpf])?", text, re.I)
    if not match:
        raise _unsupported("Native HSPICE numeric field is not a finite literal")
    suffix = (match.group(2) or "").lower()
    normalized = match.group(1) + {"t": "T", "g": "G"}.get(suffix, suffix)
    return format(engineering(normalized), ".17g")


def import_analysis(name, entry):
    if _boolean(entry.get("IsStatement", "false")) or _boolean(entry.get("MonteCarlo\\isValid", "0")) or _boolean(entry.get("SaveRecovery\\isValid", "0")):
        raise _unsupported("Enabled statement/Monte Carlo/recovery analysis needs an explicit native representation")
    sweeps = _entries({key[len("Sweep\\"):]: value for key, value in entry.items() if key.startswith("Sweep\\")}, maximum=3)
    allowed_sweeps = 1 if name in {"dc", "ac"} else 0
    if any(_boolean(sweep.get("IsValid", "0")) for sweep in sweeps[allowed_sweeps:]):
        raise _unsupported("Nested native sweeps cannot be flattened during migration")
    if name == "op":
        if entry.get("TimeList", "").strip():
            raise _unsupported("Time-qualified native OP needs explicit representation")
        options = {}
    elif name == "tran":
        options = {"stop": native_number(entry["Stop"]), "step": native_number(entry["Step"]), "start": native_number(entry.get("Start") or "0"),
                   "use_initial_conditions": _boolean(entry.get("UseIC", "0")), "accuracy": {"0": "conservative", "1": "moderate", "2": "liberal"}[entry.get("Errpreset", "1")]}
    elif name in {"dc", "ac"}:
        if not sweeps or not _boolean(sweeps[0].get("IsValid", "0")):
            raise _unsupported("Native analysis needs an enabled primary sweep")
        sweep = sweeps[0]
        options = {"start": native_number(sweep["Range\\start"]), "stop": native_number(sweep["Range\\stop"])}
        if sweep["Range\\rangeType"] == "1":
            options.update(sweep="decade", points=int(sweep["Range\\name2"]))
        elif sweep["Range\\rangeType"] == "0":
            options["sweep"] = "linear"
            if sweep["Range\\linearType"] == "0":
                options["step"] = native_number(sweep["Range\\name0"])
            elif sweep["Range\\linearType"] == "1":
                options["points"] = int(sweep["Range\\name1"])
            else:
                raise _unsupported("Unknown native linear resolution")
        else:
            raise _unsupported("Unknown native sweep scale")
        if name == "dc":
            if sweep["Var\\varType"] == "0" and sweep.get("Var\\name0") == "temp":
                options["variable"] = "temp"
            elif sweep["Var\\varType"] == "1":
                options["variable"] = _name(sweep["Var\\name1"])
            else:
                raise _unsupported("Unknown native DC sweep variable type")
    else:
        raise _unsupported("Native analysis is not represented by this HSPICE writer", analysis=name)
    return {"enabled": _boolean(entry.get("IsEnabled", "1")), "options": normalize_maestro_analysis_options(name, options)}
