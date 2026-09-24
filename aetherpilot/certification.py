"""Resource-bounded native acceptance fixtures isolated from user designs."""

import json
import time

from .contract import digest, validate_plan
from .errors import HarnessError, OperationError, ValidationError
from .pdk import catalog
from .placement import placed_geometry
from .routing import route_schematic
from .schematic_native import SchematicTransactionsBackend
from .transactions import ViewTransactions


def device_fixture(device, library, backend):
    grid = 0.0625
    parameters = {}
    if device["cell"] == "primRes2":
        parameters = {"rprim": "1", "scaleprim": "1", "lprim": "1u", "wprim": "1u", "isnoisy": "no"}
    plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": grid,
            "target": {"library": library, "cell": device["cell"], "view": "schematic", "technology": "018um_PDK"},
            "instances": [{"name": "DUT", "master": {"library": "018um_PDK", "cell": device["cell"], "view": "symbol"},
                           "origin": [0, 0], "orientation": "R0", "parameters": parameters,
                           "connections": {terminal: "T_" + terminal for terminal in device["terminal_order"]}}],
            "nets": ["T_" + terminal for terminal in device["terminal_order"]], "pins": [], "wires": []}
    plan = validate_plan(plan)
    masters = backend.master_geometry(plan)
    contacts, obstacles = placed_geometry(plan, masters)
    for net, position, label in contacts:
        plan["pins"].append({"name": net, "net": net, "direction": "inputOutput", "position": list(position), "orientation": "R0"})
    routed = route_schematic(plan, contacts, obstacles, margin=32, timeout_seconds=5)
    return validate_plan(routed["plan"]), masters


def verify_pdk(adapter, artifacts, result, options):
    devices = catalog(options["pdk_root"])
    library = "ap_v1_pdk_" + result["operation_id"][:12]
    if library in adapter.library.list():
        raise OperationError("Acceptance namespace must be fresh", stage="preflight", code="target_exists")
    backend = SchematicTransactionsBackend(adapter.api)
    deadline = time.monotonic() + min(options.get("pdk_verification_timeout_seconds", 100), 300)
    rows = []
    for device in devices["devices"]:
        row = {"cell": device["cell"], "device_def_sha256": device["device_def_sha256"], "checks": {}, "errors": []}
        rows.append(row)
        try:
            if time.monotonic() >= deadline:
                raise OperationError("Device acceptance deadline reached", stage="resources", code="timeout")
            plan, masters = device_fixture(device, library, backend)
            artifacts.write("pdk-verification/" + device["cell"] + "/plan.json", plan)
            artifacts.write("pdk-verification/" + device["cell"] + "/masters.json", masters)
            row["checks"]["master_terminal_geometry"] = "passed"
            transaction = ViewTransactions(backend, artifacts, result)
            candidate = transaction.prepare(plan)
            row["candidate_id"] = candidate["candidate_id"]
            row["checks"].update(create="passed", check="passed", save="passed", reopen="passed", read="passed")
            published = transaction.publish(candidate["candidate_id"], approved=True, candidate_sha256=candidate["candidate"]["content_sha256"])
            actual = backend.read(plan["target"])
            artifacts.write("pdk-verification/" + device["cell"] + "/readback.json", actual)
            row["target"] = plan["target"]
            row["readback_sha256"] = digest(actual)
            row["checks"]["publish"] = "passed"
            parameters = actual["instances"][0]["parameters"]
            row["parameters"] = parameters
            row["parameter_provenance"] = actual["instances"][0]["parameter_provenance"]
            mutable = next((name for name in ("m", "w", "r", "Width", "Length") if name in parameters), None)
            if mutable is not None:
                from .contract import engineering

                replacement = format(engineering(parameters[mutable]) * 2, ".17g")
                edit = {"schema_version": 1, "operation": "schematic.edit", "target": plan["target"], "base_sha256": digest(backend.content(actual)),
                        "changes": [{"type": "instance.set_parameter", "target": "DUT", "value": {"name": mutable, "value": replacement}}]}
                try:
                    edit_candidate = transaction.prepare(edit, base_sha256=edit["base_sha256"])
                except HarnessError:
                    try:
                        artifacts.write("pdk-verification/" + device["cell"] + "/edit-candidate-readback.json", json.loads(artifacts.path.joinpath("candidate-readback.json").read_text(encoding="utf-8")))
                    except (OSError, TypeError):
                        pass
                    raise
                row["checks"]["modify"] = "passed"
                row["modify_candidate_id"] = edit_candidate["candidate_id"]
            else:
                row["checks"]["modify"] = "no_mutable_parameter; geometry fixture still required"
            row["ok"] = True
        except HarnessError as exc:
            row["ok"] = False
            row["errors"].append(exc.to_dict())
            staging = next((item["target"] for item in reversed(result.get("changes", [])) if item.get("type") == "candidate_view_created"), None)
            if staging is not None:
                try:
                    artifacts.write("pdk-verification/" + device["cell"] + "/failure-readback.json", backend.read(staging))
                except HarnessError:
                    pass
        except Exception as exc:
            import traceback

            traceback.print_exc()
            row["ok"] = False
            row["errors"].append({"code": "native_exception", "exception": type(exc).__name__, "message": str(exc)})
        artifacts.write("pdk-verification/matrix.json", {"library": library, "pdk_sha256": devices["pdk_sha256"], "rows": rows})
    report = {"library": library, "pdk_sha256": devices["pdk_sha256"], "device_count": len(rows), "passed": sum(row["ok"] for row in rows), "rows": rows,
              "scope": "native master/create/check/save/reopen/read/publish/parameter-edit; simulation remains separate"}
    result["readback"] = report
    artifacts.write("pdk-verification/report.json", report)
    if report["passed"] != len(rows):
        raise ValidationError("PDK native matrix has failing rows", stage="readback", code="pdk_acceptance_failed", details={"passed": report["passed"], "total": len(rows)})
    return report
