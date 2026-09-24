"""Fresh ALPS nominal runs, readback-derived SPICE and strict result parsing."""

from __future__ import annotations

import csv
import copy
import hashlib
import math
import os
import random
import re
import shlex
import statistics
import subprocess
import time
from pathlib import Path

from .artifacts import file_digest, timestamp
from .contract import digest, engineering, identifier, numeric_text
from .errors import PreflightError, SimulationError, ValidationError
from .native import content


_MODEL_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_MODEL_ASSIGNMENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=")
_MODEL_INCLUDE = re.compile(r"^\s*\.(?:inc|include)\s+", re.I)
_MODEL_SECTION = re.compile(r"^\s*\.lib\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\$.*)?$", re.I)
_MODEL_LIBRARY_REFERENCE = re.compile(r"^\s*\.lib\s+", re.I)
_MODEL_END_SECTION = re.compile(r"^\s*\.endl(?:\s+([^\s]+))?", re.I)
_MODEL_PARAM = re.compile(r"^\s*\.param\b", re.I)
_MODEL_IGNORED_IDENTIFIERS = {
    "E", "PI", "TEMP", "TNOM", "TIME", "VT", "V", "I", "F", "X", "Y", "Z",
    "MAX", "MIN", "ABS", "SQRT", "EXP", "LOG", "LN", "POW", "PWR", "SIN", "COS",
    "GAUSS", "AGAUSS", "UNIF", "AUNIF", "LIMIT",
}


def _model_include_path(line, parent):
    try:
        parts = shlex.split(line, comments=False)
    except ValueError as exc:
        raise SimulationError("Cannot parse model include", stage="simulation_preflight", code="model_include_syntax") from exc
    if len(parts) < 2 or any(character in parts[1] for character in ("$", "\n", "\r")):
        simulation_error("Dynamic model include is unsupported", "model_include_syntax", "simulation_preflight")
    return (parent / parts[1]).resolve()


def _selected_model_lines(path, section, *, stack=()):
    path = Path(path).resolve()
    if path in stack or len(stack) >= 32:
        simulation_error("Recursive model includes are unsupported", "model_cycle", "simulation_preflight")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SimulationError("Model dependency is not readable text", stage="simulation_preflight", code="model_encoding") from exc
    sections = [match.group(1).casefold() for line in lines if (match := _MODEL_SECTION.match(line))]
    if section is not None:
        wanted = section.casefold()
        selected = []
        active = False
        if sections.count(wanted) != 1:
            simulation_error("Requested model section is missing or repeated", "model_section_missing", "simulation_preflight", section=section, path=str(path))
        for index, line in enumerate(lines, start=1):
            start = _MODEL_SECTION.match(line)
            end = _MODEL_END_SECTION.match(line)
            if start:
                active = start.group(1).casefold() == wanted
            elif end:
                active = False
            elif active:
                selected.append((index, line))
    else:
        selected = list(enumerate(lines, start=1))
    result = [(path, index, line) for index, line in selected]
    expanded = []
    for source, index, line in result:
        expanded.append((source, index, line))
        if _MODEL_INCLUDE.match(line):
            dependency = _model_include_path(line, source.parent)
            expanded.extend(_selected_model_lines(dependency, None, stack=(*stack, path)))
        elif _MODEL_LIBRARY_REFERENCE.match(line) and not _MODEL_SECTION.match(line):
            dependency = _model_include_path(line, source.parent)
            parts = shlex.split(line, comments=False)
            if len(parts) != 3:
                simulation_error("Model library reference requires a file and section", "model_include_syntax", "simulation_preflight")
            identifier(parts[2], "model section")
            expanded.extend(_selected_model_lines(dependency, parts[2], stack=(*stack, path)))
    return expanded


def model_sections_for_readback(readback, pdk_root):
    sections = set()
    section_by_prefix = {"M": "TT", "D": "TT", "Q": "BJT_TT", "R": "RES_TT", "C": "MIM_TT"}
    for instance in readback.get("instances", []):
        cell = instance.get("master", {}).get("cell")
        definition = Path(pdk_root) / str(cell) / "device.def"
        if not definition.is_file():
            return None
        first = next((line.strip() for line in definition.read_text(encoding="utf-8", errors="replace").splitlines()
                      if line.strip() and not line.lstrip().startswith("#")), "")
        section = section_by_prefix.get(first[:1].upper())
        if section is None:
            return None
        sections.add(section)
    return sections


def model_names_for_readback(readback, pdk_root):
    from .pdk import parse_device_template, render_device

    names = set()
    for index, instance in enumerate(readback.get("instances", [])):
        definition = Path(pdk_root) / str(instance.get("master", {}).get("cell")) / "device.def"
        if not definition.is_file():
            return None
        template = parse_device_template(definition.read_text())
        if template["prefix"] == "X":
            return None
        node_map = {net: "N" + str(position) for position, net in enumerate(sorted(set(instance["connections"].values())))}
        rendered, unused = render_device(instance, index=index, node_map=node_map, root=pdk_root)
        tokens = rendered.split()
        position = len(template["terminals"]) + 1
        if position >= len(tokens):
            simulation_error("Device model is missing from its netlist", "model_missing", "simulation_preflight")
        model = tokens[position]
        try:
            engineering(model)
        except ValidationError:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", model):
                return None
            names.add(model.casefold())
    return names


def validate_model_dependencies(includes, directory, *, required_sections=None, required_models=None):
    """Validate selected model sections without modifying vendor model files."""
    evidence = []
    selected_includes = [include for include in includes if required_sections is None or include.get("section") is None or include.get("section") in required_sections]
    if not selected_includes:
        simulation_error("No configured model section covers the readback devices", "model_section_missing", "simulation_preflight",
                         required_sections=sorted(required_sections or []))
    found_models = set()
    unscoped = False
    for include in selected_includes:
        section = include.get("section")
        root = (Path(directory) / include["path"]).resolve()
        lines = _selected_model_lines(root, section)
        defined = set()
        expressions = []
        in_param_block = False
        filter_models = required_models is not None and not any(re.match(r"\s*\.subckt\b", line, re.I) or re.search(r"\bako:", line, re.I) for unused, unused_index, line in lines)
        unscoped |= not filter_models
        selected_model = True
        checked_models = set()
        ignored_models = set()
        for source, line_number, line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("*"):
                continue
            model_start = re.match(r"\s*\.model\s+([^\s(]+)", line, re.I)
            if model_start:
                model_name = model_start[1].casefold()
                found_models.add(model_name)
                selected_model = not filter_models or any(model_name == name or re.fullmatch(re.escape(name) + r"\.\d+", model_name) for name in required_models)
                (checked_models if selected_model else ignored_models).add(model_name)
            elif not stripped.startswith("+"):
                selected_model = True
            if _MODEL_PARAM.match(line):
                in_param_block = True
                defined.update(_MODEL_ASSIGNMENT.findall(line))
            elif in_param_block and stripped.startswith("+"):
                defined.update(_MODEL_ASSIGNMENT.findall(line))
            else:
                in_param_block = False
            if selected_model:
                for match in re.finditer(r"=\s*'([^']*)'", line):
                    expressions.append((source, line_number, match.group(1)))
        defined_casefold = {name.casefold() for name in defined}
        missing = {}
        for source, line_number, expression in expressions:
            names = {
                name for name in _MODEL_IDENTIFIER.findall(expression)
                if name.upper() not in _MODEL_IGNORED_IDENTIFIERS and name.casefold() not in defined_casefold
            }
            if names:
                missing.setdefault(str(source), {}).setdefault(str(line_number), set()).update(names)
        normalized_missing = [
            {"path": path, "line": int(line), "names": sorted(names)}
            for path, by_line in sorted(missing.items())
            for line, names in sorted(by_line.items(), key=lambda item: int(item[0]))
        ]
        item = {"path": include["path"], "section": section, "defined_parameters": sorted(defined), "missing": normalized_missing,
                "dependency_scope": "instantiated_primitive_models" if filter_models else "all_model_definitions",
                "checked_models": sorted(checked_models), "unused_models": sorted(ignored_models)}
        evidence.append(item)
        if normalized_missing:
            simulation_error("Selected model section references undefined parameters", "model_definition_incomplete",
                             "simulation_preflight", section=section, model=include["path"], missing=normalized_missing)
    if required_models is not None and not unscoped:
        absent = [name for name in required_models if not any(model == name or re.fullmatch(re.escape(name) + r"\.\d+", model) for model in found_models)]
        if absent:
            simulation_error("Instantiated model has no definition", "model_missing", "simulation_preflight", models=sorted(absent))
    return evidence


def simulation_error(message, code, stage="simulation", **details):
    raise SimulationError(message, stage=stage, code=code, details=details)


def snapshot_models(models, directory):
    if not models:
        raise PreflightError("No model libraries configured for simulation", stage="simulation_preflight", code="models_missing")
    includes = []
    records = []
    for index, model in enumerate(models):
        source = Path(model["path"]).resolve()
        root = source.parent
        destination_root = directory / "models" / str(index)
        visiting = set()
        completed = set()
        section = model.get("section")
        if section is not None:
            identifier(section, "model section")

        def copy_model(path):
            path = path.resolve()
            if path in visiting:
                simulation_error("Recursive model includes are unsupported", "model_cycle", "simulation_preflight")
            if path in completed:
                return
            if not path.is_relative_to(root):
                simulation_error("Model include escapes its configured model directory", "model_path_escape", "simulation_preflight")
            if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                simulation_error("Model dependency is missing or too large", "model_unavailable", "simulation_preflight")
            if len(completed) + len(visiting) >= 1000:
                simulation_error("Too many model dependencies", "model_limit", "simulation_preflight")
            visiting.add(path)
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise SimulationError("Model must be readable textual SPICE", stage="simulation_preflight", code="model_encoding") from exc
            lines = []
            for line in text.splitlines():
                stripped = line.lstrip()
                if re.match(r"^\.(?:inc|include|lib)\s", stripped, re.I):
                    try:
                        parts = shlex.split(stripped, comments=False)
                    except ValueError as exc:
                        raise SimulationError("Cannot parse model include", stage="simulation_preflight", code="model_include_syntax") from exc
                    is_include = parts[0].lower() in (".inc", ".include") or len(parts) >= 3
                    if is_include:
                        if len(parts) < 2 or any(character in parts[1] for character in ("$", "\n", "\r")):
                            simulation_error("Dynamic model include is unsupported", "model_include_syntax", "simulation_preflight")
                        dependency = (path.parent / parts[1]).resolve()
                        copy_model(dependency)
                        relative = os.path.relpath(dependency, path.parent)
                        if "'" in relative:
                            simulation_error("Unsafe model filename", "model_include_syntax", "simulation_preflight")
                        line = parts[0] + " '" + relative + "'" + (" " + " ".join(parts[2:]) if len(parts) > 2 else "")
                lines.append(line)
            destination = destination_root / path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
            records.append({"path": destination.relative_to(directory).as_posix(), "source_sha256": hashlib.sha256(raw).hexdigest(), "snapshot_sha256": file_digest(destination)})
            visiting.remove(path)
            completed.add(path)

        copy_model(source)
        includes.append({"path": (destination_root / source.name).relative_to(directory).as_posix(), "section": section})
    return includes, sorted(records, key=lambda item: item["path"])


def build_netlist(plan, readback, includes, operation_id, parameter_overrides=None):
    setup = plan["simulation"]
    analysis = setup["analysis"]
    if {item["name"] for item in readback["instances"]} != set(setup["device_models"]):
        simulation_error("Readback instances do not match simulation setup", "netlist_mismatch", "netlist")
    lines = ["AetherPilot nominal DC " + operation_id, "* readback-sha256 " + digest(content(readback))]
    for model in includes:
        if "'" in model["path"] or "\n" in model["path"]:
            simulation_error("Unsafe model include path", "netlist_input", "netlist")
        lines.append((".lib" if model["section"] else ".include") + " '" + model["path"] + "'" + (" " + model["section"] if model["section"] else ""))
    lines.extend([".option post=2 numdgt=10", ".temp " + str(setup["temperature"])])

    def node(name):
        return "0" if name == setup["ground"] else name

    for instance in readback["instances"]:
        mapping = setup["device_models"][instance["name"]]
        if instance["connections"].keys() != set(mapping["terminals"]):
            simulation_error("Readback MOS terminal mismatch", "netlist_mismatch", "netlist")
        terminals = [node(instance["connections"][name]) for name in mapping["terminals"]]
        parameter_overrides = parameter_overrides or {}
        parameters = [name + "=" + numeric_text(parameter_overrides.get((instance["name"], source), instance["parameters"][source]), source) for name, source in sorted(mapping["parameters"].items())]
        if "Fingers" in instance["parameters"] and "nf" not in mapping["parameters"] and engineering(instance["parameters"]["Fingers"]) != 1:
            simulation_error("Multiple fingers require an explicit simulator mapping", "unsupported_fingers", "netlist")
        lines.append(" ".join(["M_" + instance["name"], *terminals, mapping["model"], *parameters]))
    for source in setup["sources"]:
        lines.append(" ".join([source["name"], node(source["positive"]), node(source["negative"]), str(source["dc"])]))
    lines.append(".dc " + " ".join(str(analysis[key]) for key in ("source", "start", "stop", "step")))
    lines.append(".print dc " + " ".join("v(" + node(signal) + ")" for signal in setup["signals"]))
    lines.append(".end")
    return "\n".join(lines) + "\n"


def classify_log(text, returncode):
    lower = text.lower()
    if re.search(r"(?:license.{0,100}(?:fail|denied|unavailable|cannot|expired)|(?:fail|cannot).{0,100}license)", lower):
        return "license"
    if re.search(r"(?:not converg|convergence fail|failed to converg|no convergence)", lower):
        return "convergence"
    if re.search(r"(?:unknown model|undefined model|cannot open|can't open|no such file|syntax error|model.*not found)", lower):
        return "netlist"
    if re.search(r"^\s*(?:fatal|error).*\b(?:model|parameter|syntax|netlist|subckt|subcircuit|include)\b", lower, re.M):
        return "netlist"
    summaries = re.findall(r"simulation completes with\s+(\d+)\s+error", lower)
    if summaries and int(summaries[-1]) > 0:
        return "execution"
    if re.search(r"^\s*(?:fatal|error)(?:\s*[:\[(]|\b)", text, re.I | re.M):
        return "execution"
    if returncode:
        return "execution"
    if not summaries or "simulation engine completed" not in lower:
        return "incomplete"
    return None


def parse_dc_print(text, setup):
    expected_columns = ["dc"] + ["v(" + ("0" if signal == setup["ground"] else signal).lower() + ")" for signal in setup["signals"]]
    rows = []
    in_table = False
    finished = False
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0].lower() == "dc":
            if in_table or finished or [value.lower() for value in fields] != expected_columns:
                simulation_error("Unexpected DC table header or multiple tables", "result_parse", "results")
            in_table = True
            continue
        if not in_table:
            continue
        if fields == ["y"]:
            finished = True
            in_table = False
            continue
        if len(fields) != len(expected_columns):
            simulation_error("Malformed DC result row", "result_parse", "results")
        try:
            rows.append([engineering(value) for value in fields])
        except (ValidationError, ValueError, OverflowError) as exc:
            raise SimulationError("Non-numeric or non-finite DC result", stage="results", code="result_parse") from exc
    analysis = setup["analysis"]
    expected_count = round((analysis["stop"] - analysis["start"]) / analysis["step"]) + 1
    if not finished or len(rows) != expected_count:
        simulation_error("DC result is truncated or has the wrong point count", "result_incomplete", "results")
    for index, row in enumerate(rows):
        if not math.isclose(row[0], analysis["start"] + index * analysis["step"], rel_tol=1e-8, abs_tol=1e-12):
            simulation_error("DC result sweep differs from this run's setup", "result_provenance", "results")
    return rows


def metrics_from_rows(rows, setup):
    signals = {}
    for column, signal in enumerate(setup["signals"], start=1):
        values = [row[column] for row in rows]
        signals[signal] = {"min": min(values), "max": max(values), "first": values[0], "last": values[-1], "unit": "V"}
    assertions = []
    for assertion in setup["assertions"]:
        index = round((assertion["at"] - setup["analysis"]["start"]) / setup["analysis"]["step"])
        column = setup["signals"].index(assertion["signal"]) + 1
        value = rows[index][column]
        assertions.append({**assertion, "actual": value, "passed": assertion["min"] <= value <= assertion["max"]})
    return {"analysis": "dc", "points": len(rows), "temperature_c": setup["temperature"], "signals": signals, "assertions": assertions}


class SimulationOps:
    def __init__(self, artifacts, options):
        self.artifacts = artifacts
        self.options = options

    @staticmethod
    def preflight(options):
        root = os.environ.get("ALPS_ROOT")
        if not root or not (Path(root) / "bin" / "alps").is_file():
            raise PreflightError("Configured ALPS executable is unavailable", stage="simulation_preflight", code="simulator_missing")
        if not options.get("model_libraries"):
            raise PreflightError("No model libraries configured", stage="simulation_preflight", code="models_missing")
        for model in options["model_libraries"]:
            if not Path(model["path"]).is_file():
                raise PreflightError("Model library is unavailable", stage="simulation_preflight", code="model_missing")
            if model.get("section") is not None:
                identifier(model["section"], "model section")

    def _run_dc(self, plan, readback, directory, executable, *, parameter_overrides=None, timeout_seconds=None, label="nominal"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        output = directory / "output"
        output.mkdir(mode=0o700)
        includes, models = snapshot_models(self.options["model_libraries"], directory)
        self.artifacts.write("simulation/model-validation.json", validate_model_dependencies(
            includes, directory, required_sections=model_sections_for_readback(readback, self.options.get("pdk_root")),
            required_models={mapping["model"].casefold() for mapping in plan["simulation"]["device_models"].values()}))
        setup = plan["simulation"]
        netlist = directory / "input.sp"
        netlist.write_text(build_netlist(plan, readback, includes, self.artifacts.operation_id + "-" + label, parameter_overrides), encoding="utf-8")
        provenance = {"operation_id": self.artifacts.operation_id, "sample": label, "readback_sha256": digest(content(readback)),
                      "database_sha256": readback.get("database_sha256"), "setup_sha256": digest(setup),
                      "netlist_sha256": file_digest(netlist), "models": models, "started_at": timestamp(),
                      "engine": "alps", "engine_mode": "single_dc_run", "fresh_output_directory": True,
                      "parameter_overrides": parameter_overrides or {}}
        prefix = directory.relative_to(self.artifacts.path).as_posix()
        self.artifacts.write(prefix + "/models.json", models)
        self.artifacts.write(prefix + "/setup.json", setup)
        self.artifacts.write(prefix + "/provenance.json", provenance)
        process = None
        try:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(["bash", str(executable), "input.sp", "-o", "output/"], cwd=directory, stdout=stdout, stderr=stderr)
                try:
                    returncode = process.wait(timeout=timeout_seconds or self.options["simulator_timeout_seconds"])
                except subprocess.TimeoutExpired as exc:
                    raise SimulationError("ALPS exceeded its deadline", stage="simulation", code="timeout", details={"sample": label}) from exc
        except OSError as exc:
            raise SimulationError("Cannot launch ALPS", stage="simulation", code="environment", details={"sample": label}) from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        logs = sorted(output.glob("*.log"))
        if len(logs) != 1:
            simulation_error("Expected one fresh simulator log", "result_missing", "results", sample=label)
        log_text = logs[0].read_text(errors="replace")
        version = re.search(r"Version\s+(\S+),\s+Build on\s+(\S+)", log_text)
        if version:
            provenance["engine_version"] = version.group(1)
            provenance["engine_build"] = version.group(2)
        error = classify_log(log_text, returncode)
        if error:
            simulation_error("ALPS run failed; consult private simulator logs", error, sample=label)
        prints = sorted(output.glob("*.print"))
        if len(prints) != 1:
            simulation_error("Expected one fresh DC print table", "result_missing", "results", sample=label)
        rows = parse_dc_print(prints[0].read_text(), setup)
        metrics = metrics_from_rows(rows, setup)
        counts = re.findall(r"Simulation completes with\s+(\d+)\s+error\(s\),\s+(\d+)\s+warning", log_text)
        metrics["simulator_warnings"] = int(counts[-1][1]) if counts else None
        provenance.update(ended_at=timestamp(), returncode=returncode, result_sha256=file_digest(prints[0]), metrics_sha256=digest(metrics))
        self.artifacts.write(prefix + "/provenance.json", provenance)
        with (directory / "waveform.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sweep_V", *[signal + "_V" for signal in setup["signals"]]])
            writer.writerows(rows)
        self.artifacts.write(prefix + "/metrics.json", metrics)
        return {"rows": rows, "metrics": metrics, "provenance": provenance, "models": models}

    @staticmethod
    def _percentile(values, fraction):
        ordered = sorted(values)
        if not ordered:
            return None
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    def _run_monte_carlo(self, plan, readback, executable):
        setup = plan["simulation"]
        monte_carlo = setup["monte_carlo"]
        root = self.artifacts.path / "simulation"
        root.mkdir(mode=0o700)
        self.artifacts.write("simulation/setup.json", setup)
        rng = random.Random(monte_carlo["seed"])
        started = time.monotonic()
        total_timeout = self.options.get("monte_carlo_timeout_seconds", 600)
        samples = []
        for index in range(monte_carlo["samples"]):
            remaining = total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise SimulationError("Monte Carlo exceeded its total deadline", stage="simulation", code="timeout", details={"completed_samples": index})
            current = copy.deepcopy(readback)
            overrides = {}
            sampled = {}
            instances = {item["name"]: item for item in current["instances"]}
            for variable in monte_carlo["variables"]:
                base = engineering(instances[variable["instance"]]["parameters"][variable["parameter"]])
                if variable["distribution"] == "normal":
                    value = rng.gauss(base, engineering(variable["sigma"]))
                    for _ in range(10):
                        if value > 0:
                            break
                        value = rng.gauss(base, engineering(variable["sigma"]))
                else:
                    value = rng.uniform(engineering(variable["low"]), engineering(variable["high"]))
                if not math.isfinite(value) or value <= 0:
                    raise SimulationError("Monte Carlo sampled a non-positive parameter", stage="simulation", code="sample_invalid", details={"instance": variable["instance"], "parameter": variable["parameter"], "sample": index})
                value = "%.17g" % value
                overrides[(variable["instance"], variable["parameter"])] = value
                sampled[variable["instance"] + "." + variable["parameter"]] = value
            directory = root / "monte_carlo" / ("sample-%04d" % (index + 1))
            outcome = self._run_dc(plan, current, directory, executable, parameter_overrides=overrides,
                                   timeout_seconds=min(self.options["simulator_timeout_seconds"], remaining), label="mc-%04d" % (index + 1))
            samples.append({"index": index + 1, "parameters": sampled, "rows": outcome["rows"], "metrics": outcome["metrics"]})
        signal_points = {}
        for signal in setup["signals"]:
            points = []
            for row_index in range(len(samples[0]["rows"])):
                values = [sample["rows"][row_index][setup["signals"].index(signal) + 1] for sample in samples]
                if values:
                    points.append({"index": row_index, "mean": statistics.fmean(values), "stddev": statistics.pstdev(values) if len(values) > 1 else 0.0,
                                   "min": min(values), "max": max(values), "p05": self._percentile(values, 0.05),
                                   "p50": self._percentile(values, 0.50), "p95": self._percentile(values, 0.95)})
            signal_points[signal] = points
        min_pass_rate = monte_carlo.get("min_pass_rate", 1.0)
        assertions = []
        for assertion_index, assertion in enumerate(setup["assertions"]):
            outcomes = [sample["metrics"]["assertions"][assertion_index]["passed"] for sample in samples]
            pass_rate = sum(outcomes) / len(outcomes)
            assertions.append({**assertion, "pass_rate": pass_rate, "min_pass_rate": min_pass_rate, "passed": pass_rate >= min_pass_rate})
        for sample in samples:
            sample.pop("rows", None)
        metrics = {"analysis": "monte_carlo", "base_analysis": "dc", "samples": len(samples), "seed": monte_carlo["seed"],
                   "signals": signal_points, "assertions": assertions, "min_pass_rate": min_pass_rate,
                   "simulator_warnings": sum(sample["metrics"].get("simulator_warnings") or 0 for sample in samples),
                   "resource_policy": {"total_timeout_seconds": total_timeout, "concurrency": 1}}
        self.artifacts.write("simulation/monte_carlo/summary.json", {"samples": samples, "metrics": metrics})
        if any(not assertion["passed"] for assertion in assertions):
            simulation_error("Monte Carlo pass-rate requirement failed", "metric_assertion", "metrics")
        return metrics

    def run(self, plan, readback):
        self.artifacts.event("simulation_preflight", "started")
        self.preflight(self.options)
        root = os.environ.get("ALPS_ROOT")
        executable = Path(root) / "bin" / "alps" if root else None
        if executable is None or not executable.is_file():
            raise PreflightError("Configured ALPS executable is unavailable", stage="simulation_preflight", code="simulator_missing")
        setup = plan["simulation"]
        if "monte_carlo" in setup:
            return self._run_monte_carlo(plan, readback, executable)
        directory = self.artifacts.path / "simulation"
        directory.mkdir(mode=0o700)
        output = directory / "output"
        output.mkdir(mode=0o700)
        includes, models = snapshot_models(self.options["model_libraries"], directory)
        self.artifacts.write("simulation/model-validation.json", validate_model_dependencies(
            includes, directory, required_sections=model_sections_for_readback(readback, self.options.get("pdk_root"))))
        self.artifacts.write("simulation/models.json", models)
        self.artifacts.write("simulation/setup.json", setup)
        self.artifacts.event("netlist", "started")
        netlist = directory / "input.sp"
        netlist.write_text(build_netlist(plan, readback, includes, self.artifacts.operation_id), encoding="utf-8")
        provenance = {"operation_id": self.artifacts.operation_id, "readback_sha256": digest(content(readback)),
                      "database_sha256": readback["database_sha256"],
                      "setup_sha256": digest(setup), "netlist_sha256": file_digest(netlist), "models": models,
                      "started_at": timestamp(), "engine": "alps", "fresh_output_directory": True}
        self.artifacts.write("simulation/provenance.json", provenance)
        self.artifacts.event("simulation", "running")
        process = None
        try:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(["bash", str(executable), "input.sp", "-o", "output/"], cwd=directory, stdout=stdout, stderr=stderr)
                try:
                    returncode = process.wait(timeout=self.options["simulator_timeout_seconds"])
                except subprocess.TimeoutExpired as exc:
                    raise SimulationError("ALPS exceeded its deadline", stage="simulation", code="timeout") from exc
        except OSError as exc:
            raise SimulationError("Cannot launch ALPS", stage="simulation", code="environment") from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        logs = sorted(output.glob("*.log"))
        if len(logs) != 1:
            simulation_error("Expected one fresh simulator log", "result_missing", "results")
        log_text = logs[0].read_text(errors="replace")
        version = re.search(r"Version\s+(\S+),\s+Build on\s+(\S+)", log_text)
        if version:
            provenance["engine_version"] = version.group(1)
            provenance["engine_build"] = version.group(2)
        error = classify_log(log_text, returncode)
        if error:
            simulation_error("ALPS run failed; consult private simulator logs", error)
        prints = sorted(output.glob("*.print"))
        if len(prints) != 1:
            simulation_error("Expected one fresh DC print table", "result_missing", "results")
        self.artifacts.event("results", "parsing")
        rows = parse_dc_print(prints[0].read_text(), setup)
        with (directory / "waveform.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sweep_V", *[signal + "_V" for signal in setup["signals"]]])
            writer.writerows(rows)
        metrics = metrics_from_rows(rows, setup)
        counts = re.findall(r"Simulation completes with\s+(\d+)\s+error\(s\),\s+(\d+)\s+warning", log_text)
        metrics["simulator_warnings"] = int(counts[-1][1]) if counts else None
        self.artifacts.write("simulation/metrics.json", metrics)
        provenance.update(ended_at=timestamp(), returncode=returncode, result_sha256=file_digest(prints[0]), metrics_sha256=digest(metrics))
        self.artifacts.write("simulation/provenance.json", provenance)
        if any(not assertion["passed"] for assertion in metrics["assertions"]):
            simulation_error("Simulation values violate declared limits", "metric_assertion", "metrics")
        return metrics
