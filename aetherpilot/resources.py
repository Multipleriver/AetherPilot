"""Cooperative parent-side budgets for owned process groups only."""

import os
import stat
import time
from pathlib import Path

from .errors import SessionError


def available_memory_mb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def process_group_rss_mb(group):
    pages = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if int(fields[2]) == group:
                pages += int(fields[21])
        except (OSError, ValueError, IndexError):
            continue
    return pages * os.sysconf("SC_PAGE_SIZE") / 1024 ** 2


def directory_bytes(root, *, max_files=100000):
    size = 0
    count = 0
    for path in Path(root).rglob("*"):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            size += metadata.st_size
        count += 1
        if count > max_files:
            raise SessionError("Operation produced too many files", stage="resources", code="file_budget_exceeded")
    return size


def wait_bounded(process, *, timeout, artifact_dir, max_rss_mb, max_bytes, min_free_mb=512, poll_seconds=0.5):
    deadline = time.monotonic() + timeout
    peak_rss = 0.0
    while True:
        code = process.poll()
        used = directory_bytes(artifact_dir)
        if used > max_bytes:
            raise SessionError("Operation exceeded its disk budget", stage="resources", code="disk_budget_exceeded", details={"bytes": used, "limit_bytes": max_bytes})
        if code is not None:
            return code, {"peak_rss_mb": peak_rss, "artifact_bytes": used}
        rss = process_group_rss_mb(process.pid)
        peak_rss = max(peak_rss, rss)
        if rss > max_rss_mb:
            raise SessionError("Owned process group exceeded its memory budget", stage="resources", code="memory_budget_exceeded", details={"rss_mb": rss, "limit_mb": max_rss_mb})
        free = available_memory_mb()
        if free is not None and free < min_free_mb:
            raise SessionError("Host memory is below its safety reserve", stage="resources", code="host_memory_pressure", details={"available_mb": free, "reserve_mb": min_free_mb})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SessionError("Owned process exceeded its deadline", stage="session", code="timeout")
        time.sleep(min(poll_seconds, remaining))
