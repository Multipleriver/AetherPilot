"""Strict, JSON-safe evaluation of the local Maestro output-spec contract."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence

from .contract import engineering
from .errors import ValidationError


def spec_text(spec: Mapping[str, Any] | None) -> str:
    spec = spec or {}
    lower = str(spec.get("gt", "") or "")
    upper = str(spec.get("lt", "") or "")
    if lower and upper:
        return f"range {lower} {upper}"
    if upper:
        return f"< {upper}"
    if lower:
        return f"> {lower}"
    return ""


def evaluate_scalar(value: float, spec: Mapping[str, Any] | None) -> dict[str, Any]:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValidationError("Output specification value is not finite", stage="results", code="spec_invalid")
    spec = spec or {}
    lower = engineering(spec["gt"]) if spec.get("gt") else None
    upper = engineering(spec["lt"]) if spec.get("lt") else None
    if lower is None and upper is None:
        return {"status": None, "value": value, "spec": "", "pass": None}
    if lower is not None and upper is not None and lower >= upper:
        raise ValidationError("Output specification limits are reversed", stage="results", code="spec_invalid")
    if lower is not None and upper is not None:
        passed = lower < value < upper
        near = (lower - abs(lower) * 0.1 <= value <= lower) or (upper <= value <= upper + abs(upper) * 0.1)
        status = "pass" if passed else "near" if near else "fail"
    elif upper is not None:
        passed = value < upper
        near = upper <= value <= upper + abs(upper) * 0.1
        status = "pass" if passed else "near" if near else "fail"
    else:
        passed = value > lower
        near = lower - abs(lower) * 0.1 <= value <= lower
        status = "pass" if passed else "near" if near else "fail"
    return {"status": status, "value": value, "spec": spec_text(spec), "pass": passed}


def signal_name(expression: str) -> str | None:
    match = re.fullmatch(r"[vViI]\(\s*([^()]+)\s*\)", expression or "")
    return match.group(1).strip().strip('"') if match else None


def resolve_signal_name(bundle, expression):
    waveform = bundle.get("waveform", {})
    available = {item["name"] for item in waveform.get("signals", [])} | set(bundle.get("complex_signals", {}))
    if waveform.get("source", {}).get("netlist_format") == "hspice":
        candidates = [name for name in available if name.casefold() == expression.casefold()]
        if len(candidates) != 1:
            raise ValidationError("Native HSPICE selector is missing or ambiguous", stage="results", code="signal_missing", details={"expression": expression})
        return candidates[0]
    return signal_name(expression) or expression


def waveform_scalar(bundle: Mapping[str, Any], expression: str, *, policy: str, component=None) -> dict[str, Any]:
    if signal_name(expression) is None:
        raise ValidationError("Output expression cannot be evaluated from a stored waveform", stage="results", code="spec_expression_unsupported", details={"expression": expression})
    name = resolve_signal_name(bundle, expression)
    waveform = bundle.get("waveform", {})
    signals = {item.get("name"): item for item in waveform.get("signals", []) if isinstance(item, dict)}
    axis = waveform.get("axis", {}).get("values", [])
    complex_signal = bundle.get("complex_signals", {}).get(name)
    if complex_signal:
        real = signals.get(complex_signal.get("real"), {}).get("values", [])
        imag = signals.get(complex_signal.get("imag"), {}).get("values", [])
        if len(real) != len(axis) or len(imag) != len(axis):
            raise ValidationError("Complex output is absent from the stored waveform", stage="results", code="signal_missing", details={"signal": name})
        if component not in {"real", "imag", "magnitude"}:
            raise ValidationError("Complex specifications require an explicit component", stage="results", code="spec_requires_scalar")
        values = real if component == "real" else imag if component == "imag" else [math.hypot(left, right) for left, right in zip(real, imag)]
    else:
        selected = signals.get(name)
        values = selected.get("values", []) if selected else []
    if not values or len(values) != len(axis):
        raise ValidationError("Output signal is absent from the stored waveform", stage="results", code="signal_missing", details={"signal": name})
    if policy == "op_first":
        index = 0
    elif policy == "point_last":
        index = len(values) - 1
    elif policy in {"min", "max", "mean", "rms"}:
        scalar = {"min": min, "max": max, "mean": lambda items: math.fsum(items) / len(items),
                  "rms": lambda items: math.sqrt(math.fsum(item * item for item in items) / len(items))}[policy](values)
        return {"value": scalar, "signal": name, "policy": policy, "component": component,
                "sample_count": len(values), "source": waveform.get("source", {})}
    else:
        raise ValidationError("Unknown scalar evaluation policy", stage="results", code="spec_invalid")
    return {"value": values[index], "axis": axis[index], "signal": name, "policy": policy, "component": component,
            "source": waveform.get("source", {})}


def evaluate_outputs(outputs: Mapping[str, Mapping[str, Any]], bundles: Sequence[Mapping[str, Any]], *, analysis: str, corner: str) -> list[dict[str, Any]]:
    results = []
    for name, output in outputs.items():
        spec = output.get("spec") or {}
        if not spec.get("lt") and not spec.get("gt"):
            continue
        expression = output.get("signal_name") or output.get("expr") or ""
        bundle = next((item for item in bundles if item.get("analysis") == analysis and item.get("corner") == corner), None)
        if bundle is None:
            raise ValidationError("Output specification has no matching waveform", stage="results", code="result_incomplete", details={"output": name, "analysis": analysis, "corner": corner})
        policy = spec.get("reduction")
        if not policy or not spec.get("analysis"):
            raise ValidationError("Waveform specifications require explicit analysis and reduction", stage="results", code="spec_requires_scalar",
                                  details={"output": name})
        if spec["analysis"] != analysis:
            continue
        scalar = waveform_scalar(bundle, expression, policy=policy, component=spec.get("component"))
        checked = evaluate_scalar(scalar["value"], spec)
        results.append({"output": name, "analysis": analysis, "corner": corner, "expression": expression, "evaluation": scalar, **checked})
    return results


def evaluate_native_outputs(points, outputs, specs, bundles):
    results = []
    for point in points:
        corner = point["corner"]
        for name, spec in specs.items():
            if not spec_text(spec):
                continue
            native = point["outputs"].get(name)
            if native is None or native["spec"] != spec_text(spec):
                raise ValidationError("Native output specification differs from the setup", stage="results", code="result_provenance",
                                      details={"output": name, "corner": corner})
            if spec.get("reduction"):
                reduced = evaluate_outputs({name: {**outputs[name], "spec": spec}}, bundles, analysis=spec["analysis"], corner=corner)
                checked = reduced[0]
                checked["evaluation"]["kind"] = "explicit_waveform_reduction"
                native["native_value"] = native["value"]
                native["numeric_value"] = checked["value"]
                native["value"] = format(checked["value"], ".17g")
                native["evaluation"] = checked["evaluation"]
            else:
                scalar = native["numeric_value"]
                if scalar is None:
                    raise ValidationError("Native specification output is not scalar; use a scalar expression or explicit reduction",
                                          stage="results", code="spec_requires_scalar", details={"output": name, "corner": corner})
                checked = {"output": name, "corner": corner, "expression": outputs[name].get("expr", ""),
                           "evaluation": {"policy": "native_scalar", "table_sha256": native["table_sha256"],
                                          "native_aggregate_status": native["native_aggregate_status"]}, **evaluate_scalar(scalar, spec)}
            point["outputs"][name]["pass_fail"] = checked["status"]
            results.append(checked)
    for name, spec in specs.items():
        if not spec_text(spec) or spec.get("reduction"):
            continue
        selected = [result["status"] for result in results if result["output"] == name]
        aggregate = "fail" if "fail" in selected else "near" if "near" in selected else "pass"
        native_statuses = {point["outputs"][name]["native_aggregate_status"] for point in points}
        if native_statuses != {aggregate}:
            raise ValidationError("Native specification status and exported scalar values disagree", stage="results", code="spec_result_mismatch",
                                  details={"output": name, "native_statuses": sorted(native_statuses), "evaluated_status": aggregate})
    return results


__all__ = ["evaluate_outputs", "evaluate_scalar", "signal_name", "spec_text", "waveform_scalar"]
