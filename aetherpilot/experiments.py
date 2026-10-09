"""Serial ALPS experiments with immutable models, per-run evidence, and MC accounting."""

import copy
import csv
import math
import os
import random
import re
import statistics
import subprocess
import time
from pathlib import Path

from .analyses import build_analysis_netlist, parse_analysis_table, parse_operating_point, validate_analysis_request
from .artifacts import file_digest, timestamp
from .contract import digest, engineering
from .errors import HarnessError, SimulationError, ValidationError
from .native_statistics import parse_native_mc_parameters
from .results import normalize_waveform, validate_waveform_result
from .simulation import SimulationOps, classify_log, model_names_for_readback, model_sections_for_readback, snapshot_models, validate_model_dependencies


def experiment_cases(request, readback):
    setup = validate_analysis_request(request)
    monte = setup.get("monte_carlo")
    generator = random.Random(monte["seed"] if monte else 0)
    instances = {item["name"]: item for item in readback["instances"]}
    cases = []
    for condition in setup["conditions"]:
        for sample_index in range(monte["samples"] if monte else 1):
            actual_setup = copy.deepcopy(setup)
            actual_setup["conditions"] = [{"name": "single"}]
            actual_setup["temperature"] = condition.get("temperature", setup["temperature"])
            for source in actual_setup["sources"]:
                source["dc"] = condition.get("sources", {}).get(source["name"], source["dc"])
            values = {(item["instance"], item["parameter"]): item["value"] for item in condition.get("parameters", [])}
            if monte and monte["kind"] == "parameter_resampling":
                seen = set()
                for variable in monte["variables"]:
                    identity = (variable["instance"], variable["parameter"])
                    if identity in seen or identity[0] not in instances or identity[1] not in instances[identity[0]]["parameters"]:
                        raise ValidationError("Monte Carlo variable is missing or repeated", stage="plan", code="invalid_analysis")
                    seen.add(identity)
                    nominal = engineering(values.get(identity, instances[identity[0]]["parameters"][identity[1]]))
                    values[identity] = generator.gauss(nominal, engineering(variable["sigma"])) if variable["distribution"] == "normal" else generator.uniform(engineering(variable["low"]), engineering(variable["high"]))
            for instance_name, parameter in values:
                if instance_name not in instances or parameter not in instances[instance_name]["parameters"]:
                    raise ValidationError("Sweep parameter is not present in the native readback", stage="plan", code="parameter_unresolved")
                if not math.isfinite(engineering(values[(instance_name, parameter)])):
                    raise ValidationError("Sampled parameter is not finite", stage="plan", code="invalid_analysis")
            cases.append({"index": len(cases) + 1, "condition": condition["name"], "sample": sample_index + 1,
                          "setup": actual_setup, "sections": condition.get("sections"),
                          "parameters": [{"instance": key[0], "parameter": key[1], "value": value} for key, value in sorted(values.items())],
                          "native_mc_index": sample_index + 1 if monte and monte["kind"] == "native_model" else None})
    return cases


def summarize_values(values):
    ordered = sorted(values)
    count = len(values)
    mean = statistics.mean(values)
    deviation = statistics.stdev(values) if count > 1 else None
    error = 1.96 * deviation / math.sqrt(count) if deviation is not None else None
    return {"count": count, "mean": mean, "sample_stddev": deviation, "min": ordered[0], "max": ordered[-1],
            "median": statistics.median(ordered), "p05": SimulationOps._percentile(ordered, 0.05), "p95": SimulationOps._percentile(ordered, 0.95),
            "mean_ci95_normal_approximation": [mean - error, mean + error] if error is not None else None,
            "ci_assumption": "independent finite-variance samples; normal approximation, not an exact guarantee"}


def column_metrics(rows, columns, assertions):
    metrics = {}
    for index, column in enumerate(columns, start=1):
        values = [row[index] for row in rows]
        metrics[column] = {"min": min(values), "max": max(values), "first": values[0], "last": values[-1]}
    checked = []
    for assertion in assertions:
        if assertion["column"] not in metrics:
            raise ValidationError("Assertion column is not recorded", stage="results", code="assertion_column_missing")
        value = metrics[assertion["column"]][assertion["statistic"]]
        checked.append({**assertion, "actual": value, "passed": assertion["min"] <= value <= assertion["max"]})
    return {"columns": metrics, "assertions": checked, "points": len(rows)}


def analysis_result_file(output, kind):
    pattern = r"\.(?:op|dcop)\d*" if kind == "op" else r"\.print\d*"
    candidates = sorted(path for path in output.iterdir() if re.search(pattern + r"$", path.name, re.I))
    if len(candidates) > 32:
        raise SimulationError("Analysis result count exceeds its budget", stage="results", code="result_unsafe")
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 ** 2:
            raise SimulationError("Analysis result is unsafe or exceeds its budget", stage="results", code="result_unsafe")
    if kind == "op":
        files = []
        for path in candidates:
            text = path.read_text(errors="replace").casefold()
            begin = "operating point information begin"
            end = "operating point information end"
            if text.count(begin) == text.count(end) == 1 and text.index(begin) < text.index(end):
                files.append(path)
    else:
        files = candidates
    if len(files) != 1:
        raise SimulationError("Expected one bounded fresh analysis result", stage="results", code="result_missing")
    return files[0]


class ExperimentRunner:
    def __init__(self, artifacts, options):
        self.artifacts = artifacts
        self.options = options

    def run(self, request, readback):
        setup = validate_analysis_request(request)
        coverage = readback.get("completeness", {})
        if coverage.get("status") != "complete" or coverage.get("issues") or readback.get("unsupported") or not readback.get("database_sha256") or readback.get("source", {}).get("modified") is not False:
            raise ValidationError("Formal simulation requires complete saved native evidence", stage="netlist", code="incomplete_readback")
        cases = experiment_cases(setup, readback)
        SimulationOps.preflight(self.options)
        root = self.artifacts.path / "experiment"
        root.mkdir(mode=0o700)
        includes, models = snapshot_models(self.options["model_libraries"], root)
        self.artifacts.write("experiment/model-validation.json", validate_model_dependencies(
            includes, root, required_sections=model_sections_for_readback(readback, self.options.get("pdk_root")),
            required_models=model_names_for_readback(readback, self.options.get("pdk_root"))))
        self.artifacts.write("experiment/models.json", models)
        self.artifacts.write("experiment/setup.json", setup)
        self.artifacts.write("experiment/cases.json", cases)
        executable = Path(os.environ["ALPS_ROOT"]) / "bin" / "alps"
        deadline = time.monotonic() + self.options.get("monte_carlo_timeout_seconds", 600)
        concurrency = self.options.get("simulation_concurrency", 1)
        if type(concurrency) is not int:
            concurrency = 1
        concurrency = max(1, min(concurrency, 8))
        fatal_codes = {"license", "timeout", "models_missing", "simulator_missing", "native_model_variation_unavailable",
                       "native_mc_identity_mismatch", "native_mc_evidence_invalid", "model_snapshot_changed"}

        def run_guarded(case):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SimulationError("Experiment exceeded its total deadline", stage="simulation", code="timeout")
            return self._run_case(case, readback, root, includes, models, executable, min(remaining, self.options["simulator_timeout_seconds"]))

        outcomes = []
        fatal = None
        if concurrency > 1 and len(cases) > 1:
            from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="experiment-case") as pool:
                pending = iter(cases)
                in_flight = {}
                for _ in range(min(concurrency, len(cases))):
                    case = next(pending)
                    in_flight[pool.submit(run_guarded, case)] = case
                while in_flight:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        case = in_flight.pop(future)
                        try:
                            outcome = future.result()
                        except HarnessError as exc:
                            outcome = {"index": case["index"], "condition": case["condition"], "sample": case["sample"], "ok": False, "error": exc.to_dict()}
                            if exc.code in fatal_codes:
                                fatal = exc
                        outcomes.append(outcome)
                        outcomes.sort(key=lambda item: item["index"])
                        self.artifacts.write("experiment/outcomes.json", outcomes)
                    while fatal is None and len(in_flight) < concurrency:
                        case = next(pending, None)
                        if case is None:
                            break
                        in_flight[pool.submit(run_guarded, case)] = case
        else:
            for case in cases:
                try:
                    outcomes.append(run_guarded(case))
                except HarnessError as exc:
                    outcomes.append({"index": case["index"], "condition": case["condition"], "sample": case["sample"], "ok": False, "error": exc.to_dict()})
                    self.artifacts.write("experiment/outcomes.json", outcomes)
                    if exc.code in fatal_codes:
                        fatal = exc
                        break
                self.artifacts.write("experiment/outcomes.json", outcomes)
        good = [outcome for outcome in outcomes if outcome["ok"]]
        statistics_by_condition = {}
        for condition in setup["conditions"]:
            selected = [outcome for outcome in good if outcome["condition"] == condition["name"]]
            if selected:
                statistics_by_condition[condition["name"]] = {column: {statistic: summarize_values([outcome["metrics"]["columns"][column][statistic] for outcome in selected]) for statistic in ("min", "max", "first", "last")} for column in selected[0]["metrics"]["columns"]}
        success_rate = len(good) / len(cases)
        result = {"analysis": setup["analysis"]["type"], "runs_requested": len(cases), "runs_completed": len(outcomes), "runs_succeeded": len(good),
                  "runs_failed": len(outcomes) - len(good), "success_rate": success_rate, "concurrency": concurrency,
                  "runs_not_started": len(cases) - len(outcomes), "aborted": fatal is not None,
                  "abort_reason": fatal.to_dict() if fatal else None,
                  "monte_carlo_kind": setup.get("monte_carlo", {}).get("kind"), "seed": setup.get("monte_carlo", {}).get("seed"),
                  "statistics": statistics_by_condition, "statistics_exclude_failed_samples": True,
                  "all_sample_evidence": "experiment/outcomes.json", "readback_sha256": digest(readback),
                  "native_model_variation": bool(setup.get("monte_carlo", {}).get("kind") == "native_model" and good and all(outcome.get("native_variation") for outcome in good))}
        self.artifacts.write("experiment/metrics.json", result)
        if fatal:
            raise fatal
        if success_rate < setup.get("monte_carlo", {}).get("min_success_rate", 1.0):
            raise SimulationError("Experiment failed its requested success-rate gate", stage="metrics", code="experiment_failed", details={"success_rate": success_rate})
        return result

    def _run_case(self, case, baseline, root, includes, models, executable, timeout):
        directory = root / ("run-%04d" % case["index"])
        directory.mkdir(mode=0o700)
        output = directory / "output"
        output.mkdir(mode=0o700)
        for model in models:
            if file_digest(root / model["path"]) != model["snapshot_sha256"]:
                raise SimulationError("Immutable model snapshot changed", stage="netlist", code="model_snapshot_changed")
        readback = copy.deepcopy(baseline)
        instances = {item["name"]: item for item in readback["instances"]}
        for setting in case["parameters"]:
            instances[setting["instance"]]["parameters"][setting["parameter"]] = setting["value"]
        selected = [{**model, "path": "../" + model["path"]} for model in includes]
        if case["sections"] is not None:
            if len(case["sections"]) != len(selected):
                raise ValidationError("PVT sections must match configured model-library count", stage="netlist", code="invalid_analysis")
            for model, section in zip(selected, case["sections"]):
                model["section"] = section
        text, mapping = build_analysis_netlist(case["setup"], readback, selected, self.artifacts.operation_id,
                                               pdk_root=self.options["pdk_root"], native_mc_index=case["native_mc_index"])
        netlist = directory / "input.sp"
        netlist.write_text(text)
        prefix = directory.relative_to(self.artifacts.path).as_posix()
        provenance = {"operation_id": self.artifacts.operation_id, "started_at": timestamp(), "baseline_sha256": digest(baseline), "effective_readback_sha256": digest(readback),
                      "setup_sha256": digest(case["setup"]), "netlist_sha256": file_digest(netlist), "case": case, "models_sha256": digest(models), "mapping": mapping}
        self.artifacts.write(prefix + "/provenance.json", provenance)
        process = None
        try:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                process = subprocess.Popen(["bash", str(executable), "input.sp", "-o", "output/"], cwd=directory, stdout=stdout, stderr=stderr)
                try:
                    returncode = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    raise SimulationError("ALPS sample exceeded its deadline", stage="simulation", code="timeout") from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        logs = list(output.glob("*.log"))
        if len(logs) != 1:
            raise SimulationError("Expected one fresh ALPS log", stage="results", code="result_missing")
        log_text = logs[0].read_text(errors="replace")
        if case["native_mc_index"] is not None and re.search(r"\bno\s+random\s+variables?\s+for\s+monte\s+carlo\s+analysis\b", log_text, re.I):
            raise SimulationError("Configured PDK models contain no native Monte Carlo random variables", stage="simulation", code="native_model_variation_unavailable",
                                  details={"log": str(logs[0].relative_to(root)), "log_sha256": file_digest(logs[0]), "parameter_resampling_fallback": False})
        failure = classify_log(log_text, returncode)
        if failure:
            raise SimulationError("ALPS did not produce a valid completed run", stage="simulation", code=failure)
        kind = case["setup"]["analysis"]["type"]
        result_file = analysis_result_file(output, kind)
        text = result_file.read_text(errors="strict")
        rows = parse_operating_point(text, case["setup"], mapping["node_map"]) if kind == "op" else parse_analysis_table(text, case["setup"], mapping["columns"])
        metrics = column_metrics(rows, mapping["columns"], case["setup"]["assertions"])
        native_variation = None
        if case["native_mc_index"] is not None:
            variable_files = sorted(path for path in output.iterdir() if re.search(r"\.mc\d+$", path.name))
            if not variable_files:
                raise SimulationError("Model files did not produce native statistical variables; no host resampling fallback is allowed", stage="results", code="native_model_variation_unavailable")
            if len(variable_files) != 1 or variable_files[0].is_symlink() or variable_files[0].stat().st_size > 16 * 1024 ** 2:
                raise SimulationError("Native Monte Carlo parameter evidence is ambiguous or unsafe", stage="results", code="native_mc_evidence_invalid")
            path = variable_files[0]
            sample = parse_native_mc_parameters(path.read_text(encoding="utf-8"), expected_seed=case["setup"]["monte_carlo"]["seed"],
                                                expected_sample=case["native_mc_index"])
            native_variation = {"source": "ALPS_native_statistical_variables", "variables": sorted(sample["variables"]),
                                "files": [{"path": path.name, "sha256": file_digest(path)}], "sample": sample,
                                "sample_index": case["native_mc_index"], "seed": case["setup"]["monte_carlo"]["seed"]}
        result_sha256 = file_digest(result_file)
        log_sha256 = file_digest(logs[0])
        waveform_source = {"operation_id": self.artifacts.operation_id, "case_index": case["index"],
                           "condition": case["condition"], "sample": case["sample"],
                           "readback_sha256": digest(readback), "setup_sha256": digest(case["setup"]),
                           "result_path": result_file.relative_to(self.artifacts.path).as_posix(),
                           "result_sha256": result_sha256, "log_sha256": log_sha256}
        waveform = normalize_waveform(rows, mapping["columns"], analysis=kind, source=waveform_source, fresh=True)
        waveform_errors = validate_waveform_result(waveform, expected_analysis=kind, expected_source=waveform_source)
        if waveform_errors:
            raise SimulationError("Normalized waveform failed its integrity checks", stage="results", code="result_provenance",
                                  details={"errors": waveform_errors})
        self.artifacts.write(prefix + "/waveform.json", waveform)
        with (directory / "waveform.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["axis", *mapping["columns"]])
            writer.writerows(rows)
        provenance.update(ended_at=timestamp(), returncode=returncode, result_sha256=result_sha256, log_sha256=log_sha256, metrics_sha256=digest(metrics),
                         waveform_sha256=waveform["content_sha256"])
        self.artifacts.write(prefix + "/provenance.json", provenance)
        self.artifacts.write(prefix + "/metrics.json", metrics)
        return {"index": case["index"], "condition": case["condition"], "sample": case["sample"], "ok": all(item["passed"] for item in metrics["assertions"]),
                "metrics": metrics, "native_variation": native_variation, "provenance": prefix + "/provenance.json"}
