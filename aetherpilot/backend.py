"""Optional native backend discovery and the offline stub boundary.

The public package intentionally has no dependency on Aether or PyAether.
Importing :mod:`aetherpilot` and using its data/validation helpers must work
on a regular Python installation.  Native operations ask this module for a
backend only when they are about to launch the product runtime.

Discovery is static and side effect free.  In particular, it does not source
the user's environment script (which may start a license service), import a
vendor module, or mutate a workspace.  A configured product environment is
still allowed to use the existing worker unchanged once the gate succeeds.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .errors import BackendUnavailableError


_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))")
_UNSAFE_VALUE = re.compile(r"[`\n\r]|\$\(")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    # Environment files commonly carry an inline comment after an unquoted
    # value.  Keep a literal '#' in a path or name when it is quoted.
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _read_assignments(path: Path) -> dict[str, str]:
    """Read simple shell assignments without evaluating shell code."""

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        value = _unquote(match.group(2))
        if _UNSAFE_VALUE.search(value):
            continue
        values[match.group(1)] = value
    return values


def _expand(value: str | None, values: Mapping[str, str], *, rounds: int = 8) -> str | None:
    if value is None:
        return None
    current = value
    for _ in range(rounds):
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            name = match.group(1) or match.group(3)
            replacement = values.get(name, os.environ.get(name))
            if replacement is None:
                replacement = match.group(2)
            if replacement is None:
                return match.group(0)
            changed = True
            return replacement

        expanded = _VARIABLE.sub(replace, current)
        current = expanded
        if not changed:
            break
    return current


def _configured_paths(config: Any) -> tuple[Path | None, Path | None, str | None]:
    env_file = config.get("env_file") if isinstance(config, Mapping) else getattr(config, "env_file", None)
    if env_file is None:
        return None, None, "environment_file_missing"
    env_file = Path(env_file).expanduser()
    if not env_file.is_file():
        return None, None, "environment_file_missing"
    assignments = _read_assignments(env_file)
    # Sourcing an environment file gives its assignments precedence over the
    # caller's stale shell values, so mirror that order without executing the
    # file.
    values = {**{key: value for key, value in os.environ.items() if isinstance(value, str)}, **assignments}
    root_value = _expand(values.get("AETHER_ROOT"), values)
    pyaether_value = _expand(values.get("PYAETHER_HOME"), values)
    root = Path(root_value).expanduser() if root_value else None
    pyaether = Path(pyaether_value).expanduser() if pyaether_value else None
    if root is None:
        return None, pyaether, "aether_root_missing"
    if not (root / "bin" / "pythone").is_file():
        return root, pyaether, "product_python_missing"
    return root, pyaether, None


def _has_pyaether(root: Path | None, pyaether_home: Path | None) -> bool:
    """Check for a product-side PyAether tree without importing it."""

    try:
        if importlib.util.find_spec("pyAether") is not None:
            return True
    except (ImportError, ModuleNotFoundError, ValueError):
        # A partially initialized vendor package may have no usable
        # ``__spec__``; the product-tree checks below remain authoritative.
        pass
    candidates: list[Path] = []
    for base in (pyaether_home, root / "tools" / "pyaether" if root else None):
        if base is None:
            continue
        candidates.extend((base / "pyAether.py", base / "pyAether.so", base / "lib" / "pyAether", base / "lib64" / "pyAether"))
        # Product releases use a version/platform directory between the
        # pyaether root and its lib directory.
        try:
            candidates.extend(base.glob("*/**/lib/pyAether"))
            candidates.extend(base.glob("*/**/lib64/pyAether"))
            candidates.extend(base.glob("**/site-packages/pyAether"))
        except OSError:
            continue
    for candidate in candidates:
        try:
            if candidate.is_file() or (candidate.is_dir() and next(candidate.iterdir(), None) is not None):
                return True
        except OSError:
            continue
    return False


def backend_status(config: Any = None) -> dict[str, Any]:
    """Return a JSON-safe description of the selected optional backend.

    ``backend`` may be ``"auto"`` (the default), ``"native"`` or
    ``"stub"``.  The result intentionally reports reason codes rather than
    private paths or environment contents.
    """

    if config is None or isinstance(config, (str, Path)):
        # Import lazily to keep this module safe to import while config.py is
        # being initialized and to avoid making RuntimeConfig a hard import
        # dependency for callers that only need the static classes.
        from .config import RuntimeConfig

        config = RuntimeConfig.load(config)
    configured_backend = config.get("backend") if isinstance(config, Mapping) else getattr(config, "backend", None)
    selection = str(configured_backend if configured_backend is not None else os.environ.get("AETHERPILOT_BACKEND", "auto")).strip().lower()
    if selection not in {"auto", "native", "offline", "stub"}:
        selection = "auto"
    if selection in {"offline", "stub"}:
        return {"schema_version": 1, "backend": "stub", "name": "stub", "available": False,
                "status": "unavailable", "code": "backend_unavailable", "reason": "explicit_" + selection}

    root, pyaether_home, reason = _configured_paths(config)
    if reason is not None:
        return {"schema_version": 1, "backend": "stub", "name": "stub", "available": False,
                "status": "unavailable", "code": "backend_unavailable", "reason": reason,
                "missing": [reason]}
    if not _has_pyaether(root, pyaether_home):
        reason = "pyaether_missing"
        return {"schema_version": 1, "backend": "stub", "name": "stub", "available": False,
                "status": "unavailable", "code": "backend_unavailable", "reason": reason,
                "missing": [reason]}
    return {"schema_version": 1, "backend": "native", "name": "aether", "available": True,
            "status": "available", "product_python": True, "pyaether": True}


def require_backend(config: Any = None, *, operation: str | None = None) -> dict[str, Any]:
    """Require the configured native backend and return its status."""

    status = backend_status(config)
    if not status["available"]:
        details = {key: value for key, value in status.items() if key in {"backend", "reason", "missing"}}
        if operation is not None:
            details["operation"] = operation
        raise BackendUnavailableError(details=details)
    return status


class Backend:
    """Small protocol-like facade shared by native and stub selections."""

    name = "backend"

    def __init__(self, config: Any = None):
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.status().get("available"))

    def is_available(self) -> bool:
        """Compatibility spelling for callers that prefer a method."""
        return self.available

    def status(self) -> dict[str, Any]:
        return backend_status(self.config)

    def require(self, *, operation: str | None = None) -> dict[str, Any]:
        return require_backend(self.config, operation=operation)


class StubBackend(Backend):
    """Explicit offline backend; native execution always fails closed."""

    name = "stub"

    def status(self) -> dict[str, Any]:
        status = backend_status(self.config)
        if not status.get("available"):
            return status
        # This branch is only possible if a caller manually constructs a
        # StubBackend around a native configuration; keep the facade's
        # contract explicit and fail closed.
        return {"schema_version": 1, "backend": "stub", "name": "stub", "available": False,
                "status": "unavailable", "code": "backend_unavailable", "reason": "explicit_stub"}

    def require(self, *, operation: str | None = None) -> dict[str, Any]:
        status = self.status()
        details = {"backend": "stub", "reason": status.get("reason", "explicit_stub")}
        if operation is not None:
            details["operation"] = operation
        raise BackendUnavailableError(details=details)

    def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        self.require(operation=operation)
        raise AssertionError("unreachable")


class NativeBackend(Backend):
    """Marker facade for the existing AetherRuntime worker path."""

    name = "aether"


def get_backend(config: Any = None) -> Backend:
    """Return a native marker or explicit stub without importing PyAether."""

    status = backend_status(config)
    if status["available"]:
        return NativeBackend(config)
    return StubBackend(config)


OfflineBackend = StubBackend


__all__ = ["Backend", "NativeBackend", "StubBackend", "OfflineBackend", "BackendUnavailableError",
           "backend_status", "get_backend", "require_backend"]
