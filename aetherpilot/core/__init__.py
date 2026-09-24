"""Vendor-neutral public data contracts.

The modules in :mod:`aetherpilot.core` only depend on the Python standard
library and the shared error types.  Native database adapters and PDK policy
are intentionally kept outside this package.  The historical top-level
``aetherpilot.contract`` and ``aetherpilot.readback`` modules remain available
to the private adapter layer for compatibility.
"""

from .contract import (
    ORIENTATIONS,
    canonical,
    design_digest,
    digest,
    engineering,
    fields,
    finite,
    identifier,
    no_duplicate_keys,
    numeric_text,
    parameter_literal,
    point,
    property_records,
    reject,
    signal_identifier,
    unique,
    validate_plan,
    validate_simulation,
)
from .readback import (
    canonical as readback_canonical,
    diff_readbacks,
    digest as readback_digest,
    make_snapshot,
    primitive_proof_complete,
    readback_is_complete,
    stable_readback,
    timestamp,
    validate_snapshot,
)

__all__ = [
    "ORIENTATIONS", "canonical", "design_digest", "digest", "engineering",
    "fields", "finite", "identifier", "no_duplicate_keys", "numeric_text",
    "parameter_literal", "point", "property_records", "reject",
    "signal_identifier", "unique", "validate_plan", "validate_simulation",
    "readback_canonical", "diff_readbacks", "readback_digest", "make_snapshot",
    "primitive_proof_complete", "readback_is_complete", "stable_readback",
    "timestamp", "validate_snapshot",
]
