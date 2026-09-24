"""Single-host runtime: workspace lock, owned process group and bounded execution."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
from pathlib import Path

from .artifacts import timestamp
from .backend import require_backend
from .errors import PreflightError, SessionError
from .resources import available_memory_mb, wait_bounded


@contextlib.contextmanager
def workspace_lock(workspace):
    try:
        descriptor = os.open(Path(workspace) / ".aetherpilot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise SessionError("Cannot open workspace lock", stage="session", code="lock_failed") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SessionError("Workspace is busy; no changes were attempted", stage="session", code="workspace_busy") from exc
        yield
    finally:
        os.close(descriptor)


def stop_owned_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class AetherRuntime:
    def __init__(self, config):
        self.config = config

    def worker_command(self, request, artifacts):
        worker = Path(__file__).with_name("worker.py")
        return [
            "bash", "-c",
            'set -e; source "$1"; shift; if [ -z "${AETHER_ROOT:-}" ] || [ ! -f "$AETHER_ROOT/bin/pythone" ]; then exit 78; fi; exec bash "$AETHER_ROOT/bin/pythone" "$@"',
            "aetherpilot-runtime", str(self.config.env_file), str(worker), str(artifacts.path / "request.json"),
        ]

    def execute(self, request, artifacts):
        # Keep native discovery at the execution boundary.  Importing the
        # package and using offline operations must not require a vendor
        # runtime, while native requests should report one stable error when
        # Aether or PyAether is absent.
        require_backend(self.config, operation=request.get("operation"))
        self.config.check()
        available = available_memory_mb()
        if available is not None and available < self.config.min_free_memory_mb:
            raise SessionError("Insufficient memory reserve to launch Aether", stage="resources", code="host_memory_pressure")
        request = {**request, "options": {**self.config.worker_options(), **request.get("options", {})}, "artifact_dir": str(artifacts.path)}
        artifacts.write("request.json", request)
        command = self.worker_command(request, artifacts)
        with workspace_lock(self.config.workspace):
            artifacts.event("session", "starting")
            session = {"owner": "agent", "state": "starting", "operation_id": artifacts.operation_id, "started_at": timestamp(), "transport": "local-subprocess"}
            artifacts.write("session.json", session)
            process = None
            try:
                with (artifacts.path / "native.stdout.log").open("wb") as stdout, (artifacts.path / "native.stderr.log").open("wb") as stderr:
                    process = subprocess.Popen(command, cwd=self.config.workspace, stdout=stdout, stderr=stderr, start_new_session=True)
                    session.update(pid=process.pid, process_group=process.pid, state="running")
                    artifacts.write("session.json", session)
                    limit = self.config.timeout_seconds
                    if request["operation"] == "simulation.run":
                        limit += self.config.monte_carlo_timeout_seconds
                    if request.get("plan", {}).get("simulation"):
                        limit += self.config.simulator_timeout_seconds
                        if request["plan"]["simulation"].get("monte_carlo"):
                            limit += self.config.monte_carlo_timeout_seconds
                    try:
                        returncode, usage = wait_bounded(process, timeout=limit, artifact_dir=artifacts.path,
                                                        max_rss_mb=self.config.max_worker_rss_mb, max_bytes=self.config.max_operation_bytes,
                                                        min_free_mb=self.config.min_free_memory_mb)
                        session["resources"] = usage
                    except SessionError as exc:
                        session["state"] = "timed_out" if exc.code == "timeout" else "resource_limited"
                        session["limit_error"] = exc.to_dict()
                        raise
                    session.update(returncode=returncode, state="exited")
                response_path = artifacts.path / "response.json"
                if not response_path.is_file():
                    text = (artifacts.path / "native.stderr.log").read_text(errors="replace")
                    if "license" in text.lower() and any(word in text.lower() for word in ("failed", "denied", "cannot", "no such")):
                        raise PreflightError("Native runtime could not acquire a license", stage="preflight", code="license_unavailable")
                    if returncode > 0 and self._stage(artifacts) == "session":
                        raise PreflightError("Configured environment could not launch product Python", stage="preflight", code="runtime_missing" if returncode == 78 else "environment_failed")
                    raise SessionError("Native worker exited without a response", stage=self._stage(artifacts), code="worker_exited", details={"returncode": returncode})
                try:
                    result = json.loads(response_path.read_text())
                    if result["operation_id"] != artifacts.operation_id or result["operation"] != request["operation"] or type(result["ok"]) is not bool:
                        raise ValueError("Response identity mismatch")
                    if returncode != 0 and result["ok"]:
                        raise ValueError("Successful result from failed process")
                    return result
                except (ValueError, KeyError, TypeError) as exc:
                    raise SessionError("Invalid native response", stage="session", code="protocol_error") from exc
            except OSError as exc:
                raise SessionError("Cannot execute local runtime", stage="session", code="launch_failed") from exc
            finally:
                if process is not None:
                    stop_owned_group(process)
                session["ended_at"] = timestamp()
                if session["state"] == "running":
                    session["state"] = "terminated"
                artifacts.write("session.json", session)

    @staticmethod
    def _stage(artifacts):
        try:
            return json.loads((artifacts.path / "stage.json").read_text())["stage"]
        except (OSError, ValueError, KeyError):
            return "session"
