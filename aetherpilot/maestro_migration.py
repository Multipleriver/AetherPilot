"""Loss-detecting import of native local MDE settings into managed setups."""

import copy
import ctypes
import errno
import os

from .contract import engineering
from .errors import OperationError, ValidationError
from .maestro import _default_state, _normalize_state, apply_maestro_action, normalize_maestro_analysis_options
from .artifacts import atomic_text
from .maestro_settings import read_mde_setting, setting_text


def _boolean(value, field):
    if str(value).casefold() not in {"0", "1", "true", "false"}:
        raise ValidationError("Native setting has an invalid boolean", stage="maestro", code="native_import_unsupported", details={"field": field})
    return str(value).casefold() in {"1", "true"}


def import_analysis(name, entry):
    if name == "tran":
        options = {"stop": entry["Stop"], "accuracy": {"0": "conservative", "1": "moderate", "2": "liberal"}[entry["AccuracyLevel"]]}
    elif name in {"dc", "ac", "noise", "stb"}:
        options = {"start": entry["SweepRange\\Start"], "stop": entry["SweepRange\\Stop"]}
        sweep = entry["SweepType\\SweepType"]
        if sweep == "2":
            options.update(sweep="decade", points=int(entry["SweepType\\PointsPerDec"]))
        elif sweep == "1":
            options["sweep"] = "linear"
            if entry.get("SweepType\\StepSize"):
                options["step"] = entry["SweepType\\StepSize"]
            else:
                options["points"] = int(entry["SweepType\\NumberOfStepsForStepSize"]) + 1
        else:
            raise ValueError("unsupported native sweep type")
        if name == "dc":
            source = entry.get("SweepVariable\\ComponentName")
            options.update({"source": source, "parameter": entry.get("SweepVariable\\ComponentParameterName", "dc")} if source else {"variable": entry["SweepVariable\\VariableName"]})
        elif name == "noise":
            output = {"0": "probe", "1": "voltage"}[entry["OutputNoise\\OutputNoise"]]
            options["output_mode"] = output
            if output == "probe":
                options["probe"] = entry["OutputNoise\\OutputProbeInstance"]
            else:
                options.update(positive_output=entry["OutputNoise\\PositiveOutputNode"], negative_output=entry["OutputNoise\\NegativeOutputNode"])
            mode = {"0": "voltage", "1": "current", "2": "port", "3": "none"}[entry["InputNoise\\InputNoise"]]
            options["input_mode"] = mode
            if mode != "none":
                key = {"voltage": "InputVoltageSource", "current": "InputCurrentSource", "port": "InputPortSource"}[mode]
                options["input_" + mode + "_source"] = entry["InputNoise\\" + key]
            options["noise_separation"] = _boolean(entry.get("NoiseSeparation", "false"), "NoiseSeparation")
        elif name == "stb":
            options.update(probe=entry["ProbeInstance_Terminal"], mode_type={"0": "common", "1": "differential"}[entry.get("ModeType\\ModeType", "0")],
                           mode_type_enabled=_boolean(entry.get("ModeType\\ModeTypeEnable", "0"), "ModeTypeEnable"), local_ground=entry.get("LocalGroundName", ""))
    else:
        raise ValueError("unrepresented native analysis")
    return {"enabled": _boolean(entry.get("IsEnabled", "1"), "IsEnabled"), "options": normalize_maestro_analysis_options(name, options)}


def native_setup(native, *, test="imported", model_base=None):
    try:
        if native.get("netlist_format") not in {0, 2} or len(native["design"]) != 3:
            raise ValueError("Only native HSPICE and Spectre MDE states can be imported")
        if native["netlist_format"] == 0:
            return hspice_setup(native, test=test, model_base=model_base)
        sections = native["setting_sections"]
        state = _default_state(native["library"], native["cell"], native["view"])
        state, _ = apply_maestro_action(state, {"action": "create_test", "test": test, "design_library": native["design"][0], "design_cell": native["design"][1], "design_view": native["design"][2]})
        selected = state["tests"][test]
        selected["analyses"] = {name: import_analysis(name, entry) for name, entry in native["analyses"].items()}
        outputs = sections.get("Outputs", {})
        prefixes = {value: key[:-len("outputExprAlias")] for key, value in outputs.items() if key.endswith("\\outputExprAlias")}
        for output in native["outputs"]:
            prefix = prefixes[output["name"]]
            selected["outputs"][output["name"]] = {"name": output["name"], "output_type": output["expr_type"], "expr": output["expr"], "signal_name": "",
                       "eval_type": output["eval_type"], "save": _boolean(outputs[prefix + "save"], "save"), "plot": _boolean(outputs[prefix + "plot"], "plot")}
            tokens = output["spec"].split()
            if tokens:
                if len(tokens) == 3 and tokens[0] == "range":
                    spec = {"gt": tokens[1], "lt": tokens[2]}
                elif len(tokens) == 2 and tokens[0] in {"<", ">"}:
                    spec = {"lt" if tokens[0] == "<" else "gt": tokens[1]}
                else:
                    raise ValueError("unrepresented native specification")
                selected["specs"][output["name"]] = spec
        state["corners"] = {}
        for corner in native["corners"]:
            if any(not parameter["enabled"] for parameter in corner["parameters"]):
                raise ValueError("disabled corner parameters need explicit native representation")
            state["corners"][corner["name"]] = {"enabled": corner["enabled"], "temperature": engineering(corner["temperature"]),
                     "models": copy.deepcopy(corner["models"]), "variables": {item["name"]: item["value"] for item in corner["parameters"]}}
        simulator = sections.get("SimulatorOptions", {})
        selected["simulator_options"] = {key: value for key, value in simulator.items() if key in {"absTol", "absv", "gmin", "relTol", "tnom", "scale", "scalem", "maxnotes", "maxwarns", "pivrel"}}
        selected["environment_options"] = {key: value for key, value in {
            "netlist_views": simulator.get("spectreViewList"), "stop_views": simulator.get("spectreStopList"), "project_directory": sections.get("Other", {}).get("projectDirectory")}.items() if value}
        for field in ("analyses", "outputs", "specs"):
            state[field] = copy.deepcopy(selected[field])
        state["metadata"]["native_notes"] = {section: copy.deepcopy(sections[section]) for section in ("description", "NoteText") if section in sections}
        return _normalize_state(state)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValidationError("Native setup cannot be imported without semantic loss", stage="maestro", code="native_import_unsupported", details={"reason": str(error)}) from error


def hspice_setup(native, *, test, model_base=None):
    from .maestro_hspice import SIMULATOR_OPTIONS, import_analysis as import_hspice_analysis, native_fields, native_number, opaque_sections

    state = _default_state(native["library"], native["cell"], native["view"])
    state, unused = apply_maestro_action(state, {"action": "create_test", "test": test, "simulator": "alps", "design_library": native["design"][0], "design_cell": native["design"][1], "design_view": native["design"][2]})
    selected = state["tests"][test]
    selected["analyses"] = {name: import_hspice_analysis(name, entry) for name, entry in native["analyses"].items()}
    sections = native["setting_sections"]
    facts = native_fields(sections)
    for index, output in enumerate(facts["outputs"], 1):
        name = "signal_" + str(index)
        selected["outputs"][name] = {"name": name, "output_type": output["expr_type"], "signal_name": output["expr"], "expr": "", "eval_type": "Point", "save": output["save"], "plot": output["plot"]}
    state["corners"] = {}
    for corner in facts["corners"]:
        if any(not item["enabled"] for item in [*corner["models"], *corner["parameters"]]):
            raise ValueError("disabled model/parameter entries need explicit representation")
        model_values = []
        for model in corner["models"]:
            if not os.path.isabs(model["path"]):
                raise ValueError("relative native model files need an explicit execution base")
            model_values.append({"path": model["path"], "section": model["section"]})
        state["corners"][corner["name"]] = {"enabled": corner["enabled"], "temperature": float(native_number(corner["temperature"])),
                                           "models": model_values,
                                           "variables": {parameter["name"]: parameter["value"] for parameter in corner["parameters"]}}
    simulator = sections.get("SimulatorOptions", {})
    selected["simulator_options"] = {key: value for key, value in simulator.items() if key in SIMULATOR_OPTIONS}
    selected["environment_options"] = {key: value for key, value in {"netlist_views": simulator.get("hspiceViewList"), "stop_views": simulator.get("hspiceStopList"), "project_directory": sections.get("Other", {}).get("projectDirectory")}.items() if value}
    state["metadata"]["native_notes"] = {section: copy.deepcopy(sections[section]) for section in ("description", "NoteText") if section in sections}
    preserved = opaque_sections(sections)
    if preserved:
        state["metadata"]["native_hspice_opaque_sections"] = preserved
    if model_base:
        state["metadata"]["native_hspice_model_base"] = model_base
    state["simulator"] = "alps"
    for field in ("analyses", "outputs", "specs", "simulator_options", "environment_options"):
        state[field] = copy.deepcopy(selected[field])
    return _normalize_state(state)


def infer_native_model_base(native, source_path, requested=None):
    from .maestro_hspice import native_fields

    root = os.path.abspath(os.fspath(requested)) if requested else None
    relative = sorted({model["path"] for corner in native_fields(native["setting_sections"])["corners"] for model in corner["models"] if not os.path.isabs(model["path"])})
    if not relative:
        return root
    source_parent = os.path.abspath(os.path.dirname(os.fspath(source_path)))
    candidates = []
    if root:
        candidates.append(root)
    else:
        current = source_parent
        for _ in range(10):
            candidates.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    matches = []
    for candidate in candidates:
        resolved = tuple(os.path.realpath(os.path.join(candidate, path)) for path in relative)
        if all(os.path.isfile(path) for path in resolved):
            matches.append((candidate, resolved))
    unique = {(candidate, resolved) for candidate, resolved in matches}
    if len(unique) != 1:
        raise ValidationError("Relative native HSPICE models require one unambiguous base directory", stage="maestro", code="native_import_unsupported",
                              details={"source": source_path, "model_base_dir": requested, "candidate_count": len(unique)})
    return next(iter(unique))[0]


def resolve_hspice_model_paths(native, source_path, requested=None):
    from .maestro_hspice import native_fields

    base = infer_native_model_base(native, source_path, requested)
    if not base:
        return native, None
    native = copy.deepcopy(native)
    facts = native_fields(native["setting_sections"])
    model_paths = {model["path"]: os.path.realpath(os.path.join(base, model["path"])) for corner in facts["corners"] for model in corner["models"] if not os.path.isabs(model["path"])}
    for corner in facts["corners"]:
        for model in corner["models"]:
            if model["path"] in model_paths:
                model["path"] = model_paths[model["path"]]
    for section in ("ModelSetup", "CornerSetup"):
        for key, value in list(native["setting_sections"].get(section, {}).items()):
            if key.endswith("\\ModelFile") and value in model_paths:
                native["setting_sections"][section][key] = model_paths[value]
    return native, base


def compare_native_settings(source, candidate):
    differences = []
    for section in sorted(set(source) | set(candidate)):
        if section in {"description", "NoteText"}:
            continue
        for field in sorted(set(source.get(section, {})) | set(candidate.get(section, {}))):
            before = source.get(section, {}).get(field)
            after = candidate.get(section, {}).get(field)
            if before != after:
                differences.append({"section": section, "field": field})
    if differences:
        raise ValidationError("Native setting migration would lose or change fields", stage="maestro", code="native_import_loss", details={"fields": differences[:100], "difference_count": len(differences)})


def preserve_native_notes(path, state):
    notes = state.get("metadata", {}).get("native_notes", {})
    if not isinstance(notes, dict) or set(notes) - {"description", "NoteText"} or any(not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()) for values in notes.values()):
        raise ValidationError("Imported native notes are malformed", stage="maestro", code="invalid_maestro_state")
    if notes:
        sections = read_mde_setting(path)
        sections.update(copy.deepcopy(notes))
        atomic_text(path, setting_text(sections))


def publish_directory(source, destination):
    try:
        method = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise OperationError("Atomic no-replace publication is unavailable", stage="maestro", code="native_api_missing") from error
    method.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    method.restype = ctypes.c_int
    if method(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OperationError("Native setup publication was refused", stage="maestro", code="target_exists" if error in {errno.EEXIST, errno.ENOTEMPTY} else "native_publication_failed", details={"errno": error})
