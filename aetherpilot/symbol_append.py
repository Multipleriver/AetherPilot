"""Exact baseline-preserving append semantics for native symbols."""

from collections import Counter
import copy
import re

from .contract import canonical, digest
from .errors import ValidationError
from .signals import expand_signal
from .symbol import compare_symbol_plan, symbol_content, validate_symbol_plan, validate_symbol_readback


def validate_symbol_append(source):
    value = copy.deepcopy(source)
    baseline = value.pop("base_sha256", None)
    if value.get("operation") != "symbol.append" or not isinstance(baseline, str) or re.fullmatch(r"[0-9a-f]{64}", baseline) is None:
        raise ValidationError("Symbol append requires an exact baseline hash", stage="plan", code="invalid_plan")
    value["operation"] = "symbol.apply"
    value["mode"] = "modify"
    value = validate_symbol_plan(value)
    value.update(operation="symbol.append", base_sha256=baseline)
    return value


def prepare_symbol_append(plan, baseline):
    if validate_symbol_readback(baseline):
        raise ValidationError("Symbol append needs complete native facts", stage="plan", code="incomplete_readback")
    if plan["base_sha256"] != baseline["content_sha256"]:
        raise ValidationError("Symbol baseline changed", stage="plan", code="baseline_changed")
    old_bits = {bit for term in baseline["terms"] for bit in expand_signal(term["name"], max_bits=4096)}
    new_bits = {bit for name in plan["term_order"] for bit in expand_signal(name, max_bits=4096)}
    if old_bits & new_bits:
        raise ValidationError("Appended symbol terminals overlap existing bits", stage="plan", code="terminal_exists")
    if baseline["selection_boxes"] and any(item["kind"] == "selection_box" for item in plan["shapes"]):
        raise ValidationError("Use replacement to change a symbol selection box", stage="plan", code="selection_box_exists")
    if len(plan["shapes"]) + sum(len(baseline[field]) for field in ("terms", "shapes", "labels", "selection_boxes")) > 10000:
        raise ValidationError("Symbol append exceeds the object budget", stage="plan", code="resource_limit")


def compare_symbol_append(plan, baseline, actual):
    if validate_symbol_readback(actual):
        raise ValidationError("Appended symbol readback is incomplete", stage="readback", code="incomplete_readback")
    delta = copy.deepcopy(actual)
    if symbol_content(baseline)["properties"] != symbol_content(actual)["properties"]:
        raise ValidationError("Append changed existing symbol properties", stage="readback", code="symbol_baseline_mismatch")
    for field in ("terms", "labels", "shapes", "selection_boxes"):
        remaining = Counter(canonical(item) for item in symbol_content(baseline)[field])
        delta[field] = []
        for item in actual[field]:
            normalized = symbol_content({field: [item]})[field]
            if not normalized:
                continue
            key = canonical(normalized[0])
            if remaining[key]:
                remaining[key] -= 1
            else:
                delta[field].append(item)
        if any(remaining.values()):
            raise ValidationError("Append changed existing symbol content", stage="readback", code="symbol_baseline_mismatch", details={"collection": field})
    if actual["term_order"] != baseline["term_order"] + plan["term_order"]:
        raise ValidationError("Append changed native terminal ordering", stage="readback", code="symbol_baseline_mismatch")
    for field in ("term_order", "pin_order", "port_order"):
        delta[field] = [term["name"] for term in delta["terms"]]
    delta["content_sha256"] = digest(symbol_content(delta))
    compare_symbol_plan(plan, delta)
    return {"ok": True, "baseline_preserved": True, "checks": ["added_content", "unchanged_baseline", "terminal_order"]}
