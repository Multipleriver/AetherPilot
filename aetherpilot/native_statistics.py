"""Strict identities and numeric evidence for ALPS native statistical samples."""

import math
import re

from .errors import ValidationError


def parse_native_mc_parameters(text, *, expected_seed, expected_sample):
    if type(expected_seed) is not int or type(expected_sample) is not int or expected_seed < 1 or expected_sample < 1:
        raise ValidationError("Native MC expectations must be positive integers", stage="results", code="native_mc_identity_mismatch")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 16 * 1024 ** 2:
        raise ValidationError("Native MC evidence exceeds its bound", stage="results", code="native_mc_evidence_invalid")
    patterns = {
        "global_variables": r"\$\s*Global Variable number\s*:\s*(\d+)",
        "local_variables": r"\$\s*Local Variable number\s*:\s*:?\s*(\d+)",
        "seed": r"\$\s*option seed\s*=\s*(\d+)",
        "sampling_method": r"\$\s*option sampling_method\s*=\s*(\w+)",
    }
    metadata = {}
    for field, pattern in patterns.items():
        matches = re.findall("^" + pattern + r"\s*$", text, re.M | re.I)
        if len(matches) != 1:
            raise ValidationError("Native MC metadata is missing or repeated", stage="results", code="native_mc_evidence_invalid", details={"field": field})
        metadata[field] = matches[0].lower() if field == "sampling_method" else int(matches[0])
    rows = [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("$")]
    count = metadata["global_variables"] + metadata["local_variables"]
    if not 1 <= count <= 100000 or len(rows) != 2 or rows[0][0].lower() != "index":
        raise ValidationError("Native MC requires exactly one bounded sample", stage="results", code="native_mc_evidence_invalid")
    variables = rows[0][1:]
    if len(variables) != count or len(set(variables)) != count or len(rows[1]) != count + 1:
        raise ValidationError("Native MC columns differ from their declared dimensions", stage="results", code="native_mc_evidence_invalid")
    identities = {}
    for name in variables:
        match = re.fullmatch(r"\S+(?::@:\S+)+:@:(I[GL][NU]C)", name)
        if match is None:
            raise ValidationError("Native MC variable identity is malformed", stage="results", code="native_mc_evidence_invalid")
        identities[name] = {"scope": "global" if match[1][1] == "G" else "local",
                            "distribution": "normal" if match[1][2] == "N" else "uniform"}
    if sum(item["scope"] == "global" for item in identities.values()) != metadata["global_variables"]:
        raise ValidationError("Native MC variable scope counts differ", stage="results", code="native_mc_evidence_invalid")
    if not re.fullmatch(r"[1-9]\d*", rows[1][0]) or int(rows[1][0]) != expected_sample or metadata["seed"] != expected_seed:
        raise ValidationError("Native MC seed or sample identity differs", stage="results", code="native_mc_identity_mismatch")
    if any(not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value) for value in rows[1][1:]):
        raise ValidationError("Native MC sample has invalid numeric syntax", stage="results", code="native_mc_evidence_invalid")
    try:
        values = {name: float(value) for name, value in zip(variables, rows[1][1:])}
    except (ValueError, OverflowError) as exc:
        raise ValidationError("Native MC sample is not numeric", stage="results", code="native_mc_evidence_invalid") from exc
    if any(not math.isfinite(value) for value in values.values()):
        raise ValidationError("Native MC sample is not finite", stage="results", code="native_mc_evidence_invalid")
    return {**metadata, "sample_index": expected_sample, "variables": identities, "values": values,
            "value_semantics": "simulator_random_variates_not_physical_parameter_values"}
