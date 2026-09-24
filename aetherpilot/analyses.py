"""ALPS OP/DC/AC/TRAN/NOISE contracts and readback-derived PDK netlists."""

import copy
import math
import re

from .contract import engineering, fields, finite, identifier, numeric_text
from .errors import ValidationError
from .pdk import DEFAULT_PDK_ROOT, render_device


def validate_analysis_request(source):
    value = copy.deepcopy(source)
    fields(value, ("schema_version", "analysis", "ground", "sources", "signals"), ("temperature", "conditions", "monte_carlo", "assertions"), where="analysis_request")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValidationError("Unsupported analysis schema", stage="plan", code="invalid_analysis")
    value.setdefault("temperature", 27)
    finite(value["temperature"], "temperature")
    if not -273.15 < value["temperature"] <= 1000:
        raise ValidationError("Temperature is outside the bounded analysis range", stage="plan", code="invalid_analysis")
    if not isinstance(value["ground"], str) or not value["ground"]:
        raise ValidationError("Ground must name a database net", stage="plan", code="invalid_analysis")
    if not isinstance(value["signals"], list) or not 1 <= len(value["signals"]) <= 100 or any(not isinstance(signal, str) or not signal for signal in value["signals"]) or len(set(value["signals"])) != len(value["signals"]):
        raise ValidationError("Signals must be a unique nonempty bounded list", stage="plan", code="invalid_analysis")
    if not isinstance(value["sources"], list) or len(value["sources"]) > 100:
        raise ValidationError("Invalid source list", stage="plan", code="invalid_analysis")
    names = set()
    for source in value["sources"]:
        fields(source, ("name", "positive", "negative", "dc"), ("ac", "phase", "pulse"), where="source")
        identifier(source["name"])
        if source["name"][0] not in "VI" or source["name"].casefold() in names or source["positive"] == source["negative"]:
            raise ValidationError("Sources require distinct nodes and unique V/I names", stage="plan", code="invalid_analysis")
        names.add(source["name"].casefold())
        for key in ("dc", "ac", "phase"):
            if key in source:
                source[key] = numeric_text(source[key], "source." + key)
                engineering(source[key])
        if "pulse" in source:
            pulse = source["pulse"]
            fields(pulse, ("low", "high", "delay", "rise", "fall", "width", "period"), where="pulse")
            for key in pulse:
                pulse[key] = numeric_text(pulse[key], "pulse." + key)
                if key not in {"low", "high"} and engineering(pulse[key]) < 0:
                    raise ValidationError("Pulse times cannot be negative", stage="plan", code="invalid_analysis")
            if engineering(pulse["period"]) <= 0 or sum(engineering(pulse[key]) for key in ("rise", "fall", "width")) > engineering(pulse["period"]):
                raise ValidationError("Pulse period is inconsistent", stage="plan", code="invalid_analysis")
    analysis = value["analysis"]
    if not isinstance(analysis, dict):
        raise ValidationError("Analysis must be an object", stage="plan", code="invalid_analysis")
    kind = analysis.get("type")
    if kind == "op":
        fields(analysis, ("type",), where="analysis")
    elif kind == "dc":
        fields(analysis, ("type", "source", "start", "stop", "step"), where="analysis")
        if analysis["source"].casefold() not in names:
            raise ValidationError("DC sweep source does not exist", stage="plan", code="invalid_analysis")
        for key in ("start", "stop", "step"):
            finite(analysis[key], "analysis." + key)
        if analysis["step"] <= 0 or analysis["stop"] < analysis["start"] or not math.isclose((analysis["stop"] - analysis["start"]) / analysis["step"], round((analysis["stop"] - analysis["start"]) / analysis["step"]), rel_tol=0, abs_tol=1e-7):
            raise ValidationError("Invalid DC sweep grid", stage="plan", code="invalid_analysis")
    elif kind == "tran":
        fields(analysis, ("type", "step", "stop"), ("start",), where="analysis")
        analysis.setdefault("start", 0)
        for key in ("step", "stop", "start"):
            finite(analysis[key], "analysis." + key)
        if analysis["step"] <= 0 or not 0 <= analysis["start"] < analysis["stop"]:
            raise ValidationError("Invalid transient time grid", stage="plan", code="invalid_analysis")
    elif kind in {"ac", "noise"}:
        fields(analysis, ("type", "scale", "points", "start", "stop") + (("source", "output") if kind == "noise" else ()), where="analysis")
        for key in ("start", "stop"):
            finite(analysis[key], "analysis." + key, positive=True)
        if analysis["stop"] <= analysis["start"] or analysis["scale"] not in {"lin", "dec", "oct"} or type(analysis["points"]) is not int or not 2 <= analysis["points"] <= 10000:
            raise ValidationError("Invalid frequency grid", stage="plan", code="invalid_analysis")
        if kind == "noise" and (analysis["source"].casefold() not in names or not isinstance(analysis["output"], str)):
            raise ValidationError("Noise requires an input source and output node", stage="plan", code="invalid_analysis")
    else:
        raise ValidationError("Unsupported analysis type", stage="plan", code="invalid_analysis")
    count = expected_point_count(analysis)
    if count > 20000:
        raise ValidationError("Analysis exceeds its point budget", stage="plan", code="analysis_too_large")
    value.setdefault("conditions", [{"name": "nominal"}])
    if not isinstance(value["conditions"], list) or not 1 <= len(value["conditions"]) <= 200:
        raise ValidationError("Conditions must contain 1–200 sweep/PVT cases", stage="plan", code="invalid_analysis")
    condition_names = set()
    for condition in value["conditions"]:
        fields(condition, ("name",), ("temperature", "sources", "parameters", "sections"), where="condition")
        identifier(condition["name"])
        if condition["name"] in condition_names:
            raise ValidationError("Duplicate condition name", stage="plan", code="invalid_analysis")
        condition_names.add(condition["name"])
        if "temperature" in condition:
            finite(condition["temperature"], "condition.temperature")
            if not -273.15 < condition["temperature"] <= 1000:
                raise ValidationError("Invalid condition temperature", stage="plan", code="invalid_analysis")
        if not isinstance(condition.get("sources", {}), dict) or set(condition.get("sources", {})) - {item["name"] for item in value["sources"]}:
            raise ValidationError("Condition source override is invalid", stage="plan", code="invalid_analysis")
        for name, setting in condition.get("sources", {}).items():
            engineering(setting)
        if not isinstance(condition.get("parameters", []), list):
            raise ValidationError("Parameter sweep settings must be an array", stage="plan", code="invalid_analysis")
        seen = set()
        for setting in condition.get("parameters", []):
            fields(setting, ("instance", "parameter", "value"), where="parameter_setting")
            identity = (setting["instance"], setting["parameter"])
            if identity in seen:
                raise ValidationError("Duplicate parameter setting", stage="plan", code="invalid_analysis")
            seen.add(identity)
            engineering(setting["value"])
        if "sections" in condition:
            if not isinstance(condition["sections"], list):
                raise ValidationError("PVT model sections must be an array", stage="plan", code="invalid_analysis")
            for section in condition["sections"]:
                if section is not None:
                    identifier(section)
    samples = 1
    if "monte_carlo" in value:
        monte = value["monte_carlo"]
        fields(monte, ("kind", "samples", "seed"), ("variables", "min_success_rate"), where="monte_carlo")
        if monte["kind"] not in {"parameter_resampling", "native_model"} or type(monte["samples"]) is not int or not 2 <= monte["samples"] <= 200 or type(monte["seed"]) is not int or not 1 <= monte["seed"] <= 2 ** 31 - 1:
            raise ValidationError("Invalid Monte Carlo controls", stage="plan", code="invalid_analysis")
        monte.setdefault("min_success_rate", 1.0)
        finite(monte["min_success_rate"], "min_success_rate")
        if not 0 <= monte["min_success_rate"] <= 1:
            raise ValidationError("Invalid Monte Carlo success rate", stage="plan", code="invalid_analysis")
        if monte["kind"] == "native_model" and (kind == "op" or monte.get("variables")):
            raise ValidationError("Native model MC requires DC/AC/TRAN/NOISE and statistical model files, not host variables", stage="plan", code="unsupported_monte_carlo_mode")
        if monte["kind"] == "parameter_resampling":
            if not isinstance(monte.get("variables"), list) or not 1 <= len(monte["variables"]) <= 100:
                raise ValidationError("Parameter Monte Carlo requires bounded variables", stage="plan", code="invalid_analysis")
            for variable in monte["variables"]:
                fields(variable, ("instance", "parameter", "distribution"), ("sigma", "low", "high"), where="mc_variable")
                if variable["distribution"] == "normal":
                    if set(variable) != {"instance", "parameter", "distribution", "sigma"} or engineering(variable["sigma"]) <= 0:
                        raise ValidationError("Invalid normal distribution", stage="plan", code="invalid_analysis")
                elif variable["distribution"] == "uniform":
                    if set(variable) != {"instance", "parameter", "distribution", "low", "high"} or engineering(variable["low"]) >= engineering(variable["high"]):
                        raise ValidationError("Invalid uniform distribution", stage="plan", code="invalid_analysis")
                else:
                    raise ValidationError("Unknown Monte Carlo distribution", stage="plan", code="invalid_analysis")
        samples = monte["samples"]
    if samples * len(value["conditions"]) > 200 or samples * len(value["conditions"]) * count * len(value["signals"]) > 2_000_000:
        raise ValidationError("Experiment exceeds its serial run/data budget", stage="plan", code="analysis_too_large")
    value.setdefault("assertions", [])
    if not isinstance(value["assertions"], list) or len(value["assertions"]) > 100:
        raise ValidationError("Invalid analysis assertions", stage="plan", code="invalid_analysis")
    for assertion in value["assertions"]:
        fields(assertion, ("column", "statistic", "min", "max"), where="assertion")
        if assertion["statistic"] not in {"min", "max", "first", "last"}:
            raise ValidationError("Invalid assertion statistic", stage="plan", code="invalid_analysis")
        finite(assertion["min"], "assertion.min")
        finite(assertion["max"], "assertion.max")
        if assertion["min"] > assertion["max"]:
            raise ValidationError("Invalid assertion range", stage="plan", code="invalid_analysis")
    return value


def expected_point_count(analysis):
    kind = analysis["type"]
    if kind == "op":
        return 1
    if kind in {"dc", "tran"}:
        return int(round((analysis["stop"] - analysis["start"]) / analysis["step"])) + 1
    if analysis["scale"] == "lin":
        return analysis["points"]
    base = 10 if analysis["scale"] == "dec" else 2
    return int(math.floor(math.log(analysis["stop"] / analysis["start"], base) * analysis["points"] + 1e-8)) + 1


def build_analysis_netlist(request, readback, includes, operation_id, *, pdk_root=DEFAULT_PDK_ROOT, native_mc_index=None):
    setup = validate_analysis_request(request)
    if "records" in readback:
        raise ValidationError("Simulation requires a verified flattened electrical view", stage="netlist", code="hierarchical_netlist_required")
    nets = sorted(readback["nets"])
    if setup["ground"] not in nets or any(signal not in nets for signal in setup["signals"]):
        raise ValidationError("Analysis refers to unknown database nets", stage="netlist", code="netlist_mismatch")
    node_map = {name: "0" if name == setup["ground"] else "N" + str(index) for index, name in enumerate(nets)}
    lines = ["AetherPilot " + operation_id, ".option post=2 numdgt=10", ".temp " + str(setup["temperature"])]
    for model in includes:
        if any(char in model["path"] for char in ("'", "\n", "\r")):
            raise ValidationError("Unsafe model include path", stage="netlist", code="netlist_input")
        lines.append((".lib" if model["section"] else ".include") + " '" + model["path"] + "'" + (" " + model["section"] if model["section"] else ""))
    devices = []
    for index, instance in enumerate(readback["instances"]):
        line, evidence = render_device(instance, index=index, node_map=node_map, root=pdk_root)
        lines.append(line)
        devices.append(evidence)
    for source in setup["sources"]:
        if any(source[key] not in node_map for key in ("positive", "negative")):
            raise ValidationError("Source refers to an unknown database net", stage="netlist", code="netlist_mismatch")
        line = " ".join([source["name"], node_map[source["positive"]], node_map[source["negative"]], "DC", source["dc"]])
        if "ac" in source:
            line += " AC " + source["ac"] + " " + source.get("phase", "0")
        if "pulse" in source:
            line += " PULSE(" + " ".join(source["pulse"][key] for key in ("low", "high", "delay", "rise", "fall", "width", "period")) + ")"
        lines.append(line)
    analysis = setup["analysis"]
    kind = analysis["type"]
    suffix = ""
    if native_mc_index is not None:
        if type(native_mc_index) is not int or native_mc_index < 1 or setup.get("monte_carlo", {}).get("kind") != "native_model":
            raise ValidationError("Invalid native Monte Carlo sample", stage="netlist", code="invalid_analysis")
        lines.append(".option seed=" + str(setup["monte_carlo"]["seed"]) + " modmonte=1 generate_mc_param_format_file=yes mc_file_only=no")
        suffix = " SWEEP MONTE=LIST " + str(native_mc_index)
    columns = []
    if kind == "op":
        lines.append(".op")
        columns = ["v(" + signal + ")" for signal in setup["signals"]]
    elif kind == "dc":
        lines.append(".dc " + " ".join(str(analysis[key]) for key in ("source", "start", "stop", "step")) + suffix)
    elif kind == "tran":
        lines.append(f".tran {analysis['step']} {analysis['stop']} start={analysis['start']}" + suffix)
    else:
        lines.append(".ac " + " ".join(str(analysis[key]) for key in ("scale", "points", "start", "stop")) + suffix)
        if kind == "noise":
            if analysis["output"] not in node_map:
                raise ValidationError("Noise output is not a database node", stage="netlist", code="netlist_mismatch")
            lines.append(".noise v(" + node_map[analysis["output"]] + ") " + analysis["source"] + " 1")
    if kind != "op":
        if kind == "noise":
            columns = ["onoise(mag)", "inoise(mag)"]
        elif kind == "ac":
            columns = [prefix + "(" + node_map[signal] + ")" for signal in setup["signals"] for prefix in ("vr", "vi")]
        else:
            columns = ["v(" + node_map[signal] + ")" for signal in setup["signals"]]
        lines.append(".print " + ("ac" if kind == "noise" else kind) + " " + " ".join(columns))
    lines.append(".end")
    return "\n".join(lines) + "\n", {"node_map": node_map, "devices": devices, "columns": [column.lower() for column in columns]}


def output_number(text):
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?a", text):
        return float(text[:-1]) * 1e-18
    return engineering(text)


def parse_operating_point(text, setup, node_map):
    if text.lower().count("operating point information begin") != 1 or text.lower().count("operating point information end") != 1:
        raise ValidationError("Operating point result is incomplete or ambiguous", stage="results", code="result_incomplete")
    values = {"0": 0.0}
    for match in re.finditer(r"(?:^|\s)0\s*:\s*([A-Za-z0-9_]+)\s*=\s*(\S+)", text, re.M):
        key = match[1].lower()
        if key in values:
            raise ValidationError("Operating point node occurs more than once", stage="results", code="result_parse")
        values[key] = output_number(match[2])
    if any(node_map[signal].lower() not in values for signal in setup["signals"]):
        raise ValidationError("Operating point omits requested nodes", stage="results", code="result_incomplete")
    return [[0.0, *[values[node_map[signal].lower()] for signal in setup["signals"]]]]


def parse_analysis_table(text, setup, columns):
    analysis = setup["analysis"]
    axes = {"dc": {"dc"}, "tran": {"time"}, "ac": {"freq", "frequency"}, "noise": {"freq", "frequency"}}[analysis["type"]]
    started = False
    finished = False
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].lower() in axes:
            if started or finished or [part.lower() for part in parts[1:]] != columns:
                raise ValidationError("Unexpected analysis table header", stage="results", code="result_provenance")
            started = True
            continue
        if not started:
            continue
        if parts == ["y"]:
            finished = True
            started = False
            continue
        if len(parts) != len(columns) + 1 or len(rows) >= 20000:
            raise ValidationError("Malformed or excessive analysis table", stage="results", code="result_parse")
        row = [output_number(part) for part in parts]
        if rows and row[0] <= rows[-1][0]:
            raise ValidationError("Analysis axis is not strictly increasing", stage="results", code="result_provenance")
        rows.append(row)
    if not finished or not rows:
        raise ValidationError("Analysis table is incomplete", stage="results", code="result_incomplete")
    if analysis["type"] != "tran":
        if len(rows) != expected_point_count(analysis):
            raise ValidationError("Analysis point count differs from the setup", stage="results", code="result_provenance")
        for index, row in enumerate(rows):
            if analysis["type"] == "dc":
                expected = analysis["start"] + index * analysis["step"]
            elif analysis["scale"] == "lin":
                expected = analysis["start"] + index * (analysis["stop"] - analysis["start"]) / (analysis["points"] - 1)
            else:
                expected = analysis["start"] * (10 if analysis["scale"] == "dec" else 2) ** (index / analysis["points"])
            if not math.isclose(row[0], expected, rel_tol=1e-8, abs_tol=1e-15):
                raise ValidationError("Analysis grid differs from the setup", stage="results", code="result_provenance")
    elif rows[0][0] < analysis["start"] - 1e-15 or not math.isclose(rows[-1][0], analysis["stop"], rel_tol=1e-8, abs_tol=1e-15):
        raise ValidationError("Transient interval differs from the setup", stage="results", code="result_provenance")
    return rows
