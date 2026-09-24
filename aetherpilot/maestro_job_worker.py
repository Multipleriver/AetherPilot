"""Supervisor for one detached local Maestro worker."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aetherpilot.artifacts import ArtifactOps, atomic_json, empty_result, timestamp
from aetherpilot.config import RuntimeConfig
from aetherpilot.errors import HarnessError, SessionError
from aetherpilot.resources import wait_bounded
from aetherpilot.runtime import AetherRuntime, stop_owned_group, workspace_lock


def _status(path, value):
    atomic_json(path.parent / "job-status.json", value)


def _finish_response(artifact_dir, job_id, response):
    response_path = artifact_dir / "response.json"
    atomic_json(response_path, response)
    ArtifactOps.existing(artifact_dir, job_id).finish(response)


def main():
    request_path = Path(sys.argv[1]).resolve()
    job_path = Path(sys.argv[2]).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    config = RuntimeConfig(**job["config"])
    artifact_dir = Path(request["artifact_dir"]).resolve()
    job_id = job["job_id"]
    process = None
    state = {**job, "state": "starting", "started_at": timestamp()}
    _status(job_path, state)
    try:
        artifacts = type("Artifacts", (), {"path": artifact_dir, "operation_id": job_id})()
        runtime = AetherRuntime(config)
        with workspace_lock(config.workspace):
            command = runtime.worker_command(request, artifacts)
            with (artifact_dir / "native.stdout.log").open("wb") as stdout, (artifact_dir / "native.stderr.log").open("wb") as stderr:
                process = subprocess.Popen(command, cwd=config.workspace, stdout=stdout, stderr=stderr, start_new_session=True)
                state.update(state="running", native_pid=process.pid, native_process_group=process.pid)
                _status(job_path, state)
                limit = config.timeout_seconds
                if request["operation"] == "simulation.run":
                    limit += config.monte_carlo_timeout_seconds
                if request.get("plan", {}).get("simulation"):
                    limit += config.simulator_timeout_seconds
                    if request["plan"]["simulation"].get("monte_carlo"):
                        limit += config.monte_carlo_timeout_seconds
                try:
                    code, usage = wait_bounded(process, timeout=limit, artifact_dir=artifact_dir,
                                               max_rss_mb=config.max_worker_rss_mb, max_bytes=config.max_operation_bytes,
                                               min_free_mb=config.min_free_memory_mb)
                    state["resources"] = usage
                except SessionError as exc:
                    state.update(state="timed_out" if exc.code == "timeout" else "resource_limited", error=exc.to_dict())
                    _status(job_path, state)
                    raise
                state.update(returncode=code)
                if code != 0:
                    requested = json.loads((artifact_dir / "job-status.json").read_text()).get("state") == "cancel_requested" if (artifact_dir / "job-status.json").is_file() else False
                    state.update(state="canceled" if requested else "failed", error={"code": "native_worker_failed", "returncode": code})
                    response = empty_result(request["operation"], job_id)
                    response["stage"] = "session"
                    response["errors"].append(SessionError("Detached native worker was canceled" if requested else "Detached native worker failed",
                                                             stage="session", code="canceled" if requested else "worker_failed",
                                                             details={"returncode": code}).to_dict())
                    response["ok"] = False
                    _finish_response(artifact_dir, job_id, response)
                else:
                    response = json.loads((artifact_dir / "response.json").read_text(encoding="utf-8")) if (artifact_dir / "response.json").is_file() else {}
                    state.update(state="completed" if response.get("ok") else "failed", ok=bool(response.get("ok")), error=None if response.get("ok") else response.get("errors"))
                    _finish_response(artifact_dir, job_id, response or empty_result(request["operation"], job_id))
                _status(job_path, state)
    except HarnessError as exc:
        state.update(state="failed", error=exc.to_dict(), ended_at=timestamp())
        _status(job_path, state)
        return 1
    except Exception as exc:
        state.update(state="failed", error={"code": "supervisor_failed", "exception": type(exc).__name__}, ended_at=timestamp())
        _status(job_path, state)
        return 1
    finally:
        if process is not None and process.poll() is None:
            stop_owned_group(process)
        state.setdefault("ended_at", timestamp())
        if state.get("state") == "running":
            state["state"] = "failed"
        _status(job_path, state)
    return 0 if state.get("state") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
