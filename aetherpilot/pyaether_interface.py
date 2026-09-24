#!/usr/bin/env python3
"""Compatibility entry point using the verified, bounded runtime."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aetherpilot.config import RuntimeConfig
from aetherpilot.artifacts import empty_result, redact
from aetherpilot.errors import HarnessError
from aetherpilot.harness import Harness


DEFAULT_ENV = RuntimeConfig().env_file


def run(operation, *, lib=None, cell=None, env_file=DEFAULT_ENV):
    config = RuntimeConfig.load()
    config.env_file = Path(env_file).resolve()
    harness = Harness(config)
    if operation == "list-libs":
        return harness.libraries()
    if operation == "create-schematic":
        if not lib or not cell:
            raise ValueError("create-schematic requires --lib and --cell")
        return harness.smoke(lib, cell, "018um_PDK")
    raise ValueError("Unsupported compatibility operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("list-libs", "create-schematic"))
    parser.add_argument("--lib")
    parser.add_argument("--cell")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = parser.parse_args()
    if args.operation == "create-schematic" and (not args.lib or not args.cell):
        parser.error("create-schematic requires --lib and --cell")
    try:
        result = run(args.operation, lib=args.lib, cell=args.cell, env_file=args.env_file)
    except HarnessError as exc:
        result = empty_result(args.operation)
        result["stage"] = exc.stage
        result["errors"].append(exc.to_dict())
    print(json.dumps(redact(result), sort_keys=True, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
