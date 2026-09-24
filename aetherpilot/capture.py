"""Bounded consistent capture and caller-driven automatic snapshot scheduling."""

from __future__ import annotations

import math
import time

from .errors import OperationError, ValidationError


class CaptureCoordinator:
    def __init__(self, *, attempts=3, timeout_seconds=120, clock=time.monotonic):
        if type(attempts) is not int or not 1 <= attempts <= 3 or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120:
            raise ValidationError("Capture permits at most three attempts in 120 seconds", stage="snapshot", code="invalid_snapshot_policy")
        self.attempts = attempts
        self.timeout = timeout_seconds
        self.clock = clock

    def capture(self, observe, copy, verify, discard):
        deadline = self.clock() + self.timeout
        history = []
        for attempt in range(1, self.attempts + 1):
            if self.clock() >= deadline:
                break
            candidate = None
            retained = False
            try:
                before = observe()
                if before is None:
                    raise OperationError("Capture needs a reliable source revision", stage="snapshot", code="source_revision_unavailable")
                candidate = copy(before, deadline)
                after = observe()
                if before != after:
                    history.append({"attempt": attempt, "status": "source_changed"})
                    continue
                verification = verify(candidate, deadline)
                if verification.get("ok") is not True:
                    raise OperationError("Isolated snapshot verification failed", stage="snapshot", code="snapshot_reopen_failed", details=verification)
                if before != observe():
                    history.append({"attempt": attempt, "status": "source_changed_after_reopen"})
                    continue
                if self.clock() >= deadline:
                    break
                retained = True
                history.append({"attempt": attempt, "status": "verified"})
                return {"candidate": candidate, "revision": before, "verification": verification, "attempts": history}
            except OperationError as exc:
                if exc.code != "snapshot_source_changed":
                    raise
                history.append({"attempt": attempt, "status": "source_changed_during_copy"})
            finally:
                if candidate is not None and not retained:
                    discard(candidate)
        raise OperationError("Source did not remain stable within the capture budget", stage="snapshot", code="snapshot_source_changed", details={"attempts": history})


class SnapshotScheduler:
    """Call tick from the owning editor event loop; never starts a background thread."""

    def __init__(self, capture, *, interval_seconds=30, idle_seconds=3, clock=time.monotonic):
        self.capture = capture
        self.clock = clock
        self.configure(interval_seconds=interval_seconds, idle_seconds=idle_seconds)
        self.last_revision = None
        self.captured_revision = None
        self.last_change = self.clock()
        self.last_attempt = None
        self.busy = False

    def configure(self, *, interval_seconds=None, idle_seconds=None):
        for key, value in (("interval_seconds", interval_seconds), ("idle_seconds", idle_seconds)):
            if value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 86400:
                raise ValidationError("Snapshot intervals must be finite positive seconds", stage="snapshot", code="invalid_snapshot_policy")
            setattr(self, key, value)

    def tick(self, revision, *, enabled=True):
        now = self.clock()
        if revision != self.last_revision:
            self.last_revision = revision
            self.last_change = now
        if not enabled or self.busy or revision is None:
            return {"status": "disabled" if not enabled else "busy" if self.busy else "revision_unavailable"}
        if revision == self.captured_revision:
            return {"status": "unchanged"}
        if now - self.last_change < self.idle_seconds:
            return {"status": "waiting_for_idle"}
        if self.last_attempt is not None and now - self.last_attempt < self.interval_seconds:
            return {"status": "waiting_for_interval"}
        self.last_attempt = now
        self.busy = True
        try:
            result = self.capture()
            if result.get("ok") is True:
                self.captured_revision = revision
            return {"status": "captured" if result.get("ok") is True else "failed", "result": result}
        finally:
            self.busy = False
