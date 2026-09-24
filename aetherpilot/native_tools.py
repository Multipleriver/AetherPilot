"""Operation-local launch adapters for vendor scripts with Bash syntax."""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .errors import PreflightError


@contextmanager
def native_tool_environment(directory, tools):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    evidence = []
    previous = os.environ.get("PATH")
    previous_home = os.environ.get("AETHER_HOME")
    shadow_root = None
    shadow_home = None
    try:
        for name in tools:
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("Tool must be an executable basename")
            executable = shutil.which(name)
            if executable is None:
                raise PreflightError("Native executable is unavailable", stage="preflight", code="native_tool_missing", details={"tool": name})
            with Path(executable).open("rb") as handle:
                header = handle.read(65536)
            wrapped = header.startswith(b"#!/bin/sh") and b"function " in header
            if wrapped:
                launcher = directory / name
                launcher.write_text("#!/bin/bash\nexec /bin/bash %s \"$@\"\n" % shlex.quote(executable), encoding="utf-8")
                launcher.chmod(0o700)
                if shadow_home is None and previous_home and Path(previous_home).is_dir():
                    shadow_root = Path(tempfile.mkdtemp(prefix="aetherpilot-toolroot-"))
                    shadow_home = shadow_root / "tools" / "aether"
                    shadow_home.parent.mkdir(mode=0o700, parents=True)
                    shadow_home.symlink_to(Path(previous_home).resolve(), target_is_directory=True)
                    shadow_bin = shadow_root / "bin"
                    shadow_bin.mkdir(mode=0o700)
                    shadow_launcher = shadow_bin / name
                    shadow_launcher.write_text("#!/bin/bash\nexec /bin/bash %s \"$@\"\n" % shlex.quote(executable), encoding="utf-8")
                    shadow_launcher.chmod(0o700)
                    os.environ["AETHER_HOME"] = str(shadow_home)
            evidence.append({"tool": name, "executable": executable, "bash_wrapper": wrapped,
                             "aether_home_shadow": str(shadow_home) if shadow_home else None})
        path_entries = [str(directory)]
        if shadow_root is not None:
            path_entries.append(str(shadow_root / "bin"))
        path_entries.append(previous if previous is not None else os.defpath)
        os.environ["PATH"] = os.pathsep.join(path_entries)
        yield evidence
    finally:
        if previous is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = previous
        if previous_home is None:
            os.environ.pop("AETHER_HOME", None)
        else:
            os.environ["AETHER_HOME"] = previous_home
        if shadow_root is not None:
            shutil.rmtree(shadow_root, ignore_errors=True)
