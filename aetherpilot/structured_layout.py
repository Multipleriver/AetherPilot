"""Structured layout compatibility entry.

The implementation moved to :mod:`aetherpilot.layout_geometry` (shared
geometry/verification helpers) and :mod:`aetherpilot.layout_recipes` (the
recipe registry and engine).  This module keeps the original public surface —
``OTA5_STRUCTURE``, ``compose_ota5`` and ``layout_metrics`` — so existing
callers, tests and the worker keep working unchanged.
"""

from __future__ import annotations

from .layout_geometry import GRID, layout_metrics
from .layout_recipes import OTA5_STRUCTURE, compose_structured


def compose_ota5(source, masters, *, target, structure=None, grid=GRID):
    """Compose a compact OTA5 plan (compat wrapper around the recipe engine)."""
    return compose_structured(source, masters, target=target, recipe="ota5", structure=structure, grid=grid)


__all__ = ["GRID", "OTA5_STRUCTURE", "compose_ota5", "layout_metrics"]
