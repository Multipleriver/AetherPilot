"""Single-purpose isolated native snapshot verifier; no design writes permitted."""

import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aetherpilot.artifacts import atomic_json
from aetherpilot.contract import digest
from aetherpilot.native import SchematicOps
from aetherpilot.readback import make_snapshot, validate_snapshot
from aetherpilot.snapshot_native import design_facts, library_path


def main():
    os.umask(0o077)
    request = json.loads(Path(sys.argv[1]).read_text())
    bundle = Path(request["bundle"]).resolve()
    if Path.cwd().resolve() != bundle:
        raise RuntimeError("isolated reopen has the wrong working directory")
    import pyAether as api

    api.emyInitAether("-adv")
    actual = SchematicOps(api).read_hierarchy(request["target"], max_depth=request["max_depth"],
                                             configuration=request["configuration"], pdk_root=bundle / "libraries" / "018um_PDK")
    if validate_snapshot(make_snapshot(actual), require_complete=True):
        raise RuntimeError("isolated hierarchy is incomplete")
    names = {record["target"]["library"] for record in actual["records"]}
    names.add("basic")
    for record in actual["records"]:
        readback = record.get("readback")
        if readback is not None:
            names.update(instance["master"]["library"] for instance in readback["instances"])
            if readback.get("technology"):
                names.add(readback["technology"])
            path = Path(readback["source"]["file"]).resolve()
            if not path.is_relative_to(bundle / "libraries") or readback["source"].get("modified") is not False:
                raise RuntimeError("isolated source resolved outside the bundle or is modified")
    for name in names:
        if library_path(api, name) != bundle / "libraries" / name:
            raise RuntimeError("native dependency escaped the snapshot namespace")
    actual_hash = digest(design_facts(actual))
    if actual_hash != request["facts_sha256"]:
        raise RuntimeError("isolated native facts differ from the source")
    atomic_json(request["response"], {"ok": True, "facts_sha256": actual_hash, "libraries": sorted(names), "pid": os.getpid(), "isolated": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
