"""Strict declarative contract; no expressions, shell commands or native code.

The schema and validators live in :mod:`aetherpilot.core.contract`.  This
module is the private adapter entry point: it re-exports the shared helpers
and pins the certification gates (``018um_PDK`` technology, ``alps``
simulation backend) for the native runtime.
"""

from __future__ import annotations

from .core.contract import *  # noqa: F401,F403
from .core.contract import validate_plan as _core_validate_plan
from .core.contract import validate_simulation as _core_validate_simulation
from .errors import ValidationError  # noqa: F401


def validate_plan(source):
    """Validate a schematic plan against the certified 018um_PDK/ALPS gates."""
    return _core_validate_plan(source, technology="018um_PDK", simulation_backend="alps")


def validate_simulation(plan):
    """Validate the optional plan-embedded simulation against the ALPS gate."""
    return _core_validate_simulation(plan, backend="alps")
