"""Bounded hierarchy traversal with explicit view decisions and bit bindings."""

from __future__ import annotations

import copy
import time

from .core.contract import digest, fields, identifier
from .errors import OperationError, ValidationError
from .core.readback import primitive_proof_complete
from .signals import bind_hierarchy, instance_occurrences, netset_overrides, resolve_inherited


def view_configuration(value=None):
    value = copy.deepcopy(value) if value is not None else {}
    fields(value, (), ("switch_views", "bindings"), where="view_configuration")
    value.setdefault("switch_views", ["schematic"])
    value.setdefault("bindings", [])
    if not isinstance(value["switch_views"], list) or not 1 <= len(value["switch_views"]) <= 32:
        raise ValidationError("View search order requires 1–32 entries", stage="read", code="invalid_view_configuration")
    for view in value["switch_views"]:
        identifier(view, "view")
    if len(set(value["switch_views"])) != len(value["switch_views"]):
        raise ValidationError("Duplicate switch view", stage="read", code="invalid_view_configuration")
    if not isinstance(value["bindings"], list) or len(value["bindings"]) > 10000:
        raise ValidationError("Too many hierarchy bindings", stage="read", code="invalid_view_configuration")
    seen = set()
    for binding in value["bindings"]:
        fields(binding, ("path", "view"), where="view_binding")
        path = binding["path"]
        if not isinstance(path, list) or not path or len(path) > 100 or any(not isinstance(part, str) or not part or len(part) > 4096 for part in path):
            raise ValidationError("View binding requires an explicit instance path", stage="read", code="invalid_view_configuration")
        identifier(binding["view"], "view")
        if tuple(path) in seen:
            raise ValidationError("Duplicate hierarchy binding", stage="read", code="invalid_view_configuration")
        seen.add(tuple(path))
    return value


def read_hierarchy(read, exists, target, *, configuration=None, primitive=None, max_depth=20,
                   max_records=10000, timeout_seconds=60, clock=time.monotonic):
    if type(max_depth) is not int or not 0 <= max_depth <= 100:
        raise ValidationError("Hierarchy depth must be from 0 to 100", stage="read", code="invalid_depth")
    if type(max_records) is not int or not 1 <= max_records <= 100000 or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120:
        raise ValidationError("Invalid hierarchy resource budget", stage="read", code="invalid_budget")
    config = view_configuration(configuration)
    bindings = {tuple(item["path"]): item["view"] for item in config["bindings"]}
    used_bindings = set()
    active = set()
    cache = {}
    dependencies = {}
    records = []
    deadline = clock() + timeout_seconds

    def check_budget():
        if len(records) >= max_records or clock() >= deadline:
            raise OperationError("Hierarchy traversal budget exhausted", stage="read", code="resource_limit",
                                 details={"records": len(records), "max_records": max_records, "timeout_seconds": timeout_seconds})

    def visit(current, path, depth, parent_instance=None, resolution=None, inherited_context=None):
        check_budget()
        identity = tuple(current[key] for key in ("library", "cell", "view"))
        record = {"path": path, "target": current, "view_resolution": resolution or {"policy": "explicit_root"}}
        if parent_instance is not None:
            record["instance"] = copy.deepcopy(parent_instance)
        records.append(record)
        if identity in active:
            record["status"] = "cycle"
            return
        if depth > max_depth:
            record["status"] = "depth_limit"
            return
        try:
            available = exists(current)
        except Exception as exc:
            record.update(status="incomplete", reason="native view lookup failed", error={"exception": type(exc).__name__})
            return
        if not available:
            record.update(status="black_box", reason="selected view unavailable")
            return
        active.add(identity)
        try:
            if identity not in cache:
                try:
                    cache[identity] = read(current)
                except Exception as exc:
                    record.update(status="incomplete", reason="native read failed", error={"exception": type(exc).__name__})
                    return
            actual = copy.deepcopy(cache[identity])
            record["readback"] = actual
            record["status"] = actual.get("completeness", {}).get("status", "unknown")
            actual["hierarchy"] = {"path": path, "children": []}
            contexts = copy.deepcopy(inherited_context or [])
            definitions = actual.get("inherited_connections", [])
            resolutions = []
            for definition in definitions:
                try:
                    resolutions.append(resolve_inherited(definition, contexts))
                except (TypeError, ValueError) as exc:
                    resolutions.append({"raw_definition": definition, "resolved": False, "error": str(exc)})
            actual["inherited_resolution"] = {
                "status": "complete" if all(item.get("resolved") is True for item in resolutions) else "incomplete",
                "contexts": contexts,
                "bindings": resolutions,
            }
            record["inherited_context"] = contexts
            record["inherited_resolution"] = actual["inherited_resolution"]
            if actual["inherited_resolution"]["status"] != "complete":
                record["status"] = "incomplete"
            dependencies[identity] = {"target": current, "database_sha256": actual.get("database_sha256"),
                                      "source": actual.get("source", {}), "readback_sha256": digest(cache[identity])}
            if parent_instance is not None:
                try:
                    ports = {pin["name"]: pin["net"] for pin in actual["pins"]}
                    if len(ports) != len(actual["pins"]):
                        raise ValueError("duplicate child port")
                    record["port_bindings"] = bind_hierarchy(parent_instance["connections"], ports)
                except (ValueError, KeyError) as exc:
                    record.update(status="incomplete", binding_error=str(exc))
            occurrences = (occurrence for instance in actual.get("instances", []) for occurrence in instance_occurrences(instance))
            for instance in occurrences:
                check_budget()
                master = instance["master"]
                child_path = path + [instance["name"]]
                selected = bindings.get(tuple(child_path))
                if selected is not None:
                    used_bindings.add(tuple(child_path))
                views = [selected] if selected is not None else config["switch_views"]
                candidates = [{"library": master["library"], "cell": master["cell"], "view": view} for view in views]
                decision = {"policy": "path_binding" if selected is not None else "switch_views", "searched": views}
                actual["hierarchy"]["children"].append(child_path)

                # Certified primitives are leaves even if the product also
                # exposes an implementation schematic.  Probe primitive
                # metadata before selecting a child view so known
                # analog/basic symbols are not traversed as user hierarchy.
                primitive_probed = False
                probed_proof = None
                if primitive is not None and selected is None:
                    try:
                        probed_proof = primitive(instance)
                        primitive_probed = True
                    except Exception as exc:
                        records.append({"path": child_path, "target": copy.deepcopy(master), "instance": copy.deepcopy(instance),
                                        "status": "incomplete", "view_resolution": decision, "primitive": None,
                                        "reason": "primitive metadata lookup failed", "error": {"exception": type(exc).__name__}})
                        continue
                    proof_identity_matches = isinstance(probed_proof, dict) and (
                        probed_proof.get("library") == master.get("library") and
                        probed_proof.get("cell") == master.get("cell")
                    )
                    if probed_proof is not None and proof_identity_matches:
                        valid = primitive_proof_complete(probed_proof)
                        proof_reason = probed_proof.get("reason", "primitive proof incomplete") if isinstance(probed_proof, dict) else "primitive proof must be an object"
                        leaf = {"path": child_path, "target": copy.deepcopy(master), "instance": copy.deepcopy(instance),
                                "status": "primitive" if valid else "incomplete", "view_resolution": decision,
                                "primitive": copy.deepcopy(probed_proof),
                                "reason": "verified device boundary" if valid else proof_reason}
                        records.append(leaf)
                        if valid:
                            dependencies[tuple(master[key] for key in ("library", "cell", "view"))] = {"target": master, "primitive": probed_proof}
                        continue

                try:
                    child = next((candidate for candidate in candidates if exists(candidate)), None)
                except Exception as exc:
                    records.append({"path": child_path, "target": copy.deepcopy(master), "instance": copy.deepcopy(instance),
                                    "status": "incomplete", "view_resolution": decision,
                                    "primitive": None, "reason": "native child view lookup failed",
                                    "error": {"exception": type(exc).__name__}})
                    continue
                if child is not None:
                    child_context = copy.deepcopy(contexts)
                    try:
                        overrides = copy.deepcopy(instance.get("netset_overrides", {}))
                        if not overrides:
                            overrides = netset_overrides(instance.get("properties"))
                        if overrides:
                            child_context.insert(0, {"path": child_path, "overrides": overrides})
                    except (TypeError, ValueError) as exc:
                        child_context.insert(0, {"path": child_path, "overrides": {}, "error": str(exc)})
                    visit(child, child_path, depth + 1, instance, decision, child_context)
                else:
                    proof = probed_proof
                    if primitive is not None and selected is None and not primitive_probed:
                        # Preserve the historical callback contract for
                        # callers that only provide a generic proof object:
                        # invoke it at the leaf boundary after confirming no
                        # child view exists.
                        try:
                            proof = primitive(instance)
                        except Exception as exc:
                            records.append({"path": child_path, "target": copy.deepcopy(master), "instance": copy.deepcopy(instance),
                                            "status": "incomplete", "view_resolution": decision, "primitive": None,
                                            "reason": "primitive metadata lookup failed", "error": {"exception": type(exc).__name__}})
                            continue
                    if proof is not None:
                        valid = primitive_proof_complete(proof)
                        proof_reason = proof.get("reason", "primitive proof incomplete") if isinstance(proof, dict) else "primitive proof must be an object"
                        records.append({"path": child_path, "target": copy.deepcopy(master), "instance": copy.deepcopy(instance),
                                        "status": "primitive" if valid else "incomplete", "view_resolution": decision,
                                        "primitive": copy.deepcopy(proof),
                                        "reason": "verified device boundary" if valid else proof_reason})
                        if valid:
                            dependencies[tuple(master[key] for key in ("library", "cell", "view"))] = {"target": master, "primitive": proof}
                    else:
                        records.append({"path": child_path, "target": copy.deepcopy(master), "instance": copy.deepcopy(instance),
                                    "status": "black_box", "view_resolution": decision, "primitive": None,
                                    "reason": "no selected design view or certified primitive"})
        finally:
            active.remove(identity)

    visit(copy.deepcopy(target), [], 0)
    issues = []
    for record in records:
        if record["status"] in {"complete", "primitive"}:
            continue
        issue = {"path": record["path"], "status": record["status"]}
        for key in ("reason", "binding_error", "error"):
            if key in record:
                issue[key] = record[key]
        issues.append(issue)
    issues.extend({"path": list(path), "status": "unused_view_binding"} for path in sorted(bindings.keys() - used_bindings))
    complete = not issues
    return {"schema_version": 2, "root": target, "max_depth": max_depth, "records": records,
            "complete": complete, "completeness": {"status": "complete" if complete else "incomplete", "issues": issues},
            "dependencies": sorted(dependencies.values(), key=lambda item: digest(item["target"])),
            "view_resolution": {"policy": "explicit_config", "configuration": config},
            "budget": {"max_records": max_records, "unique_views_read": len(cache), "timeout_seconds": timeout_seconds}}
