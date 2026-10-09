"""Portable runtime configuration with explicit optional backend settings."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .contract import no_duplicate_keys
from .errors import PreflightError


ROOT = Path(__file__).resolve().parents[1]


def _path_env(name, default):
    return Path(os.environ.get(name, default))


@dataclass
class RuntimeConfig:
    backend: str = field(default_factory=lambda: os.environ.get("AETHERPILOT_BACKEND", "auto"))
    env_file: Path = field(default_factory=lambda: _path_env("AETHERPILOT_ENV_FILE", "backend.env"))
    workspace: Path = field(default_factory=lambda: _path_env("AETHERPILOT_WORKSPACE", "workspace"))
    artifact_root: Path = field(default_factory=lambda: _path_env("AETHERPILOT_ARTIFACT_ROOT", "artifacts"))
    timeout_seconds: float = 120
    simulator_timeout_seconds: float = 120
    monte_carlo_timeout_seconds: float = 600
    simulation_concurrency: int = 1
    model_libraries: list = field(default_factory=list)
    screenshot: bool = True
    snapshot_root: Optional[Path] = None
    snapshot_interval_seconds: float = 30
    snapshot_idle_seconds: float = 3
    snapshot_retention_count: int = 20
    snapshot_retention_hours: float = 24
    snapshot_quota_bytes: int = 10 * 1024 ** 3
    max_worker_rss_mb: int = 2048
    min_free_memory_mb: int = 512
    max_operation_bytes: int = 1024 ** 3
    pdk_root: Path = field(default_factory=lambda: _path_env("AETHERPILOT_PDK_ROOT", "pdk"))
    technology_library: str = field(default_factory=lambda: os.environ.get("AETHERPILOT_TECHNOLOGY", ""))

    def __post_init__(self):
        try:
            if self.backend not in {"auto", "native", "offline", "stub"}:
                raise ValueError("Invalid backend")
            if not isinstance(self.technology_library, str) or len(self.technology_library) > 256:
                raise ValueError("Invalid technology library")
            for key in ("env_file", "workspace", "artifact_root", "pdk_root"):
                setattr(self, key, Path(getattr(self, key)).expanduser().resolve())
            if self.snapshot_root is None:
                self.snapshot_root = self.artifact_root / "snapshots"
            else:
                self.snapshot_root = Path(self.snapshot_root).expanduser().resolve()
            for value in (self.timeout_seconds, self.simulator_timeout_seconds, self.monte_carlo_timeout_seconds, self.snapshot_interval_seconds, self.snapshot_idle_seconds, self.snapshot_retention_hours):
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
                    raise ValueError("Invalid timeout")
            if type(self.snapshot_retention_count) is not int or self.snapshot_retention_count <= 0:
                raise ValueError("Invalid snapshot retention count")
            if type(self.snapshot_quota_bytes) is not int or self.snapshot_quota_bytes <= 0:
                raise ValueError("Invalid snapshot quota")
            for value in (self.max_worker_rss_mb, self.min_free_memory_mb, self.max_operation_bytes):
                if type(value) is not int or value <= 0:
                    raise ValueError("Invalid resource budget")
            if type(self.screenshot) is not bool or not isinstance(self.model_libraries, list):
                raise ValueError("Invalid options")
            if type(self.simulation_concurrency) is not int or not 1 <= self.simulation_concurrency <= 8:
                raise ValueError("Invalid simulation concurrency")
        except (ValueError, TypeError) as exc:
            raise PreflightError("Invalid runtime configuration", stage="preflight", code="invalid_config") from exc

    @classmethod
    def load(cls, source=None):
        if isinstance(source, cls):
            return source
        if isinstance(source, Mapping):
            data = dict(source)
            if data.keys() - cls.__dataclass_fields__.keys():
                raise PreflightError("Unknown runtime configuration fields", stage="preflight", code="invalid_config")
            try:
                return cls(**data)
            except (ValueError, TypeError) as exc:
                raise PreflightError("Invalid runtime configuration", stage="preflight", code="invalid_config") from exc
        if source is None:
            source = os.environ.get("AETHERPILOT_CONFIG")
            if source is None:
                local_hint = os.environ.get("AETHERPILOT_LOCAL_CONFIG")
                candidates = [Path.cwd() / ".aetherpilot" / "config.json", Path.cwd() / "aetherpilot.local.json"]
                local = Path(local_hint).expanduser() if local_hint else next((item for item in candidates if item.exists()), None)
                if local is None or not local.exists():
                    return cls()
                source = local
        try:
            path = Path(source).resolve()
            data = json.loads(path.read_text(), object_pairs_hook=no_duplicate_keys)
            if not isinstance(data, dict) or data.keys() - cls.__dataclass_fields__.keys():
                raise ValueError("Unknown configuration fields")
            for key in ("env_file", "workspace", "artifact_root", "snapshot_root", "pdk_root"):
                if key in data and data[key] is not None:
                    data[key] = (path.parent / data[key]).resolve()
            for key in ("timeout_seconds", "simulator_timeout_seconds", "monte_carlo_timeout_seconds"):
                if key in data and (type(data[key]) not in (int, float) or not math.isfinite(data[key]) or not 0 < data[key] <= 86400):
                    raise ValueError("Invalid timeout")
            if "screenshot" in data and type(data["screenshot"]) is not bool:
                raise ValueError("Invalid screenshot setting")
            if not isinstance(data.get("model_libraries", []), list):
                raise ValueError("Invalid model libraries")
            for model in data.get("model_libraries", []):
                if not isinstance(model, dict) or set(model) - {"path", "section"} or "path" not in model:
                    raise ValueError("Invalid model library")
                model["path"] = str((path.parent / model["path"]).resolve())
            return cls(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise PreflightError("Cannot load runtime configuration", stage="preflight", code="invalid_config") from exc

    def check(self):
        if self.backend in {"offline", "stub"}:
            return
        for path, code in ((self.env_file, "environment_missing"), (self.workspace / "lib.defs", "workspace_missing")):
            if not path.is_file():
                raise PreflightError("Required local runtime input is missing", stage="preflight", code=code)
        if not self.workspace.is_dir():
            raise PreflightError("Workspace does not exist", stage="preflight", code="workspace_missing")

    def worker_options(self):
        return {"backend": self.backend, "technology_library": self.technology_library, "pdk_root": str(self.pdk_root), "max_operation_bytes": self.max_operation_bytes, "max_worker_rss_mb": self.max_worker_rss_mb,
                "min_free_memory_mb": self.min_free_memory_mb, "model_libraries": self.model_libraries, "simulator_timeout_seconds": self.simulator_timeout_seconds, "monte_carlo_timeout_seconds": self.monte_carlo_timeout_seconds, "screenshot": self.screenshot,
                "simulation_concurrency": self.simulation_concurrency,
                "snapshot_root": str(self.snapshot_root), "snapshot_interval_seconds": self.snapshot_interval_seconds,
                "snapshot_retention_count": self.snapshot_retention_count, "snapshot_retention_hours": self.snapshot_retention_hours,
                "snapshot_quota_bytes": self.snapshot_quota_bytes}
