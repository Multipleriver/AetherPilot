"""Durable local Maestro jobs with explicit ownership and bounded execution."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .artifacts import ArtifactOps, atomic_json, timestamp
from .errors import ArtifactError, OperationError, PreflightError, SessionError, ValidationError
from .resources import available_memory_mb


_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL = {"completed", "failed", "canceled", "timed_out", "resource_limited"}


def _job_path(root: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValidationError("Maestro job ID is invalid", stage="maestro", code="invalid_job_id")
    path = (Path(root).resolve() / job_id).resolve()
    if path.parent != Path(root).resolve():
        raise ValidationError("Maestro job path escapes the artifact root", stage="maestro", code="unsafe_job_path")
    return path


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise OperationError("Maestro job record is unreadable", stage="maestro", code="job_record_invalid") from exc
    if not isinstance(value, dict):
        raise OperationError("Maestro job record is malformed", stage="maestro", code="job_record_invalid")
    return value


def _config_payload(config):
    return {
        "env_file": str(config.env_file), "workspace": str(config.workspace), "artifact_root": str(config.artifact_root),
        "timeout_seconds": config.timeout_seconds, "simulator_timeout_seconds": config.simulator_timeout_seconds,
        "monte_carlo_timeout_seconds": config.monte_carlo_timeout_seconds, "model_libraries": config.model_libraries,
        "screenshot": config.screenshot, "snapshot_root": str(config.snapshot_root),
        "snapshot_interval_seconds": config.snapshot_interval_seconds, "snapshot_idle_seconds": config.snapshot_idle_seconds,
        "snapshot_retention_count": config.snapshot_retention_count, "snapshot_retention_hours": config.snapshot_retention_hours,
        "snapshot_quota_bytes": config.snapshot_quota_bytes, "max_worker_rss_mb": config.max_worker_rss_mb,
        "min_free_memory_mb": config.min_free_memory_mb, "max_operation_bytes": config.max_operation_bytes,
        "pdk_root": str(config.pdk_root),
    }


def _write_status(directory: Path, status: dict[str, Any]) -> None:
    atomic_json(directory / "job-status.json", status)


def _manifest_complete(directory: Path, job_id: str) -> bool:
    """Check a finished bundle without changing any evidence files."""
    try:
        ArtifactOps.inspect(directory.parent, job_id)
    except (ArtifactError, OSError, ValueError, TypeError, KeyError):
        return False
    return True


def _refresh_manifest(directory: Path, job_id: str) -> bool:
    """Compatibility wrapper for the former mutating helper.

    Existing callers may still import this private name, but status polling
    must never rewrite a sealed evidence bundle.  The return value reports
    whether the current immutable bundle is complete.
    """
    return _manifest_complete(directory, job_id)


def launch(config, request, artifacts):
    config.check()
    available = available_memory_mb()
    if available is not None and available < config.min_free_memory_mb:
        raise SessionError("Insufficient memory reserve to launch Aether", stage="resources", code="host_memory_pressure")
    request = {**request, "options": {**config.worker_options(), **request.get("options", {})}, "artifact_dir": str(artifacts.path)}
    artifacts.write("request.json", request)
    directory = artifacts.path
    job = {
        "schema_version": 1, "job_id": artifacts.operation_id, "operation_id": artifacts.operation_id,
        "operation": request["operation"], "state": "queued", "owner": "aetherpilot", "created_at": timestamp(),
        "config": _config_payload(config),
    }
    atomic_json(directory / "job.json", job)
    supervisor = Path(__file__).with_name("maestro_job_worker.py")
    command = [sys.executable, str(supervisor), str(directory / "request.json"), str(directory / "job.json")]
    try:
        with (directory / "job.stdout.log").open("wb") as stdout, (directory / "job.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=config.workspace, stdout=stdout, stderr=stderr, start_new_session=True)
    except OSError as exc:
        job.update(state="failed", error={"code": "launch_failed", "message": str(exc)}, ended_at=timestamp())
        atomic_json(directory / "job.json", job)
        _write_status(directory, job)
        raise SessionError("Cannot launch durable Maestro job", stage="session", code="launch_failed") from exc
    job.update(state="running", pid=process.pid, process_group=process.pid, started_at=timestamp())
    atomic_json(directory / "job.json", job)
    _write_status(directory, job)
    return {"action": "run_simulation", "job_id": artifacts.operation_id, "state": "running", "pid": process.pid,
            "operation_id": artifacts.operation_id, "artifact_dir": str(directory), "durable": True,
            "poll": {"status": "maestro.job_status", "wait": "maestro.job_wait", "cancel": "maestro.job_cancel"}}


def status(config, job_id: str) -> dict[str, Any]:
    directory = _job_path(config.artifact_root, job_id)
    job = _read(directory / "job.json")
    current = _read(directory / "job-status.json") if (directory / "job-status.json").is_file() else job
    if current.get("state") not in _TERMINAL and (directory / "response.json").is_file() and _manifest_complete(directory, job_id):
        try:
            response = _read(directory / "response.json")
            state = "completed" if response.get("ok") else "failed"
            current = {**current, "state": state, "ended_at": timestamp(), "ok": bool(response.get("ok"))}
            _write_status(directory, current)
        except OperationError:
            pass
    if current.get("state") not in _TERMINAL:
        pid = current.get("pid") or job.get("pid")
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        native_alive = False
        native_group = current.get("native_process_group")
        if isinstance(native_group, int) and native_group > 0:
            try:
                os.killpg(native_group, 0)
                native_alive = True
            except OSError:
                native_alive = False
        if not alive and not native_alive:
            canceled = current.get("state") == "cancel_requested"
            current = {**current, "state": "canceled" if canceled else "failed", "ended_at": timestamp(),
                       "error": None if canceled else {"code": "job_lost", "message": "Durable job supervisor exited without a response"}}
            _write_status(directory, current)
    return {"action": "job_status", "job_id": job_id, "state": current.get("state"), "ok": current.get("ok"),
            "pid": current.get("pid"), "started_at": current.get("started_at"), "ended_at": current.get("ended_at"),
            "error": current.get("error"), "artifact_dir": str(directory), "durable": True}


def _response(config, job_id):
    directory = _job_path(config.artifact_root, job_id)
    path = directory / "response.json"
    return _read(path) if path.is_file() else None


def wait(config, job_id: str, timeout: float = 600):
    if type(timeout) not in (int, float) or timeout <= 0:
        raise ValidationError("Maestro job wait timeout must be positive", stage="plan", code="invalid_job_timeout")
    deadline = time.monotonic() + timeout
    while True:
        current = status(config, job_id)
        if current["state"] in _TERMINAL:
            response = _response(config, job_id)
            return {"action": "job_wait", "job_id": job_id, "state": current["state"], "completed": True,
                    "status": current, "result": response}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"action": "job_wait", "job_id": job_id, "state": current["state"], "completed": False,
                    "timeout": True, "status": current, "result": None}
        time.sleep(min(0.5, remaining))


def cancel(config, job_id: str):
    directory = _job_path(config.artifact_root, job_id)
    current = _read(directory / "job-status.json") if (directory / "job-status.json").is_file() else _read(directory / "job.json")
    if current.get("state") in _TERMINAL:
        return {"action": "job_cancel", "job_id": job_id, "state": current.get("state"), "canceled": current.get("state") == "canceled"}
    supervisor_pid = current.get("pid")
    native_group = current.get("native_process_group")
    if not isinstance(supervisor_pid, int) or supervisor_pid <= 0:
        raise OperationError("Maestro job has no owned process group", stage="maestro", code="job_record_invalid")
    updated = {**current, "state": "cancel_requested", "cancel_requested_at": timestamp()}
    _write_status(directory, updated)
    groups = (native_group,) if isinstance(native_group, int) and native_group > 0 else (supervisor_pid,)
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"action": "job_cancel", "job_id": job_id, "state": "cancel_requested", "canceled": True,
            "supervisor_pid": supervisor_pid, "native_process_group": native_group}


__all__ = ["launch", "status", "wait", "cancel"]
