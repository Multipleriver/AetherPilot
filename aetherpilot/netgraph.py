"""Verified hierarchy-to-flat electrical projection with per-bit provenance."""

import copy

from .core.contract import digest
from .errors import ValidationError
from .core.readback import readback_is_complete, stable_readback
from .signals import bit_connections, expand_signal, instance_occurrences


def flatten_hierarchy(hierarchy, *, max_bits=100000, max_instances=10000):
    if not readback_is_complete(hierarchy) or "records" not in hierarchy:
        raise ValidationError("Flattening requires a complete verified hierarchy", stage="netlist", code="incomplete_readback")
    records = {tuple(record["path"]): record for record in hierarchy["records"]}
    parents = {}

    def find(identity):
        parents.setdefault(identity, identity)
        root = identity
        while parents[root] != root:
            root = parents[root]
        while parents[identity] != identity:
            previous = parents[identity]
            parents[identity] = root
            identity = previous
        return root

    def join(first, second):
        left, right = find(first), find(second)
        if left != right:
            smaller, larger = sorted((left, right))
            parents[larger] = smaller

    globals_by_name = {}
    for path, record in sorted(records.items()):
        if "readback" not in record:
            continue
        actual = record["readback"]
        if actual.get("source", {}).get("modified") is not False or not actual.get("database_sha256"):
            raise ValidationError("Simulation flattening requires saved dependency revisions", stage="netlist", code="source_state_unconfirmed")
        inherited_resolution = actual.get("inherited_resolution")
        if actual.get("inherited_connections"):
            if not isinstance(inherited_resolution, dict) or inherited_resolution.get("status") != "complete" or len(inherited_resolution.get("bindings", [])) != len(actual["inherited_connections"]):
                raise ValidationError("Inherited connection context needs independent native validation before flattening", stage="netlist", code="inherited_context_unverified")
        for name in actual["nets"]:
            for bit in expand_signal(name):
                identity = path, bit
                find(identity)
                if bit.endswith("!"):
                    join(identity, globals_by_name.setdefault(bit, identity))
                if len(parents) > max_bits:
                    raise ValidationError("Flattened net graph exceeds its bit budget", stage="netlist", code="resource_limit")
        for binding in (inherited_resolution or {}).get("bindings", []):
            if binding.get("resolved") is not True:
                raise ValidationError("Inherited connection resolution is incomplete", stage="netlist", code="inherited_context_unverified")
            definition = binding.get("raw_definition", {})
            defaults = expand_signal(definition["default"])
            effective = expand_signal(binding["effective"])
            if len(defaults) != len(effective):
                raise ValidationError("Inherited connection changes signal width", stage="netlist", code="netlist_mismatch")
            source = binding.get("override_chain", [{}])[0].get("source", "default")
            if source == "default":
                continue
            if not isinstance(source, list) or not source or source != list(path):
                raise ValidationError("Inherited connection provenance does not identify its instance", stage="netlist", code="inherited_context_unverified")
            parent_path = path[:-1]
            for default_bit, effective_bit in zip(defaults, effective):
                child = (path, default_bit)
                parent = (parent_path, effective_bit)
                if child not in parents or parent not in parents:
                    raise ValidationError("Inherited connection refers to an unknown net", stage="netlist", code="netlist_mismatch")
                join(child, parent)
        inherited_child_bits = {
            bit
            for definition in actual.get("inherited_connections", [])
            for bit in expand_signal(definition.get("default", ""))
        }
        if path:
            if not isinstance(record.get("port_bindings"), list):
                raise ValidationError("Hierarchy lacks verified port bindings", stage="netlist", code="hierarchy_binding_missing")
            for binding in record["port_bindings"]:
                if binding["child_net"] in inherited_child_bits:
                    continue
                child, parent = (path, binding["child_net"]), (path[:-1], binding["parent_net"])
                if child not in parents or parent not in parents:
                    raise ValidationError("Port binding refers to an unknown bit", stage="netlist", code="netlist_mismatch")
                join(child, parent)
    groups = {}
    for identity in parents:
        groups.setdefault(find(identity), []).append(identity)
    names = {}
    aliases = {}
    provenance = []
    for root, members in sorted(groups.items()):
        root_names = sorted(bit for path, bit in members if not path)
        global_names = sorted(bit for path, bit in members if bit.endswith("!"))
        name = root_names[0] if root_names else global_names[0] if global_names else "N_h_" + digest(members)[:24]
        if name in names.values():
            raise ValidationError("Flattened net names collide", stage="netlist", code="ambiguous_identity")
        names[root] = name
        aliases.update({bit: name for path, bit in members if not path})
        provenance.append({"net": name, "members": [{"path": list(path), "bit": bit} for path, bit in members]})
    instances = []
    instance_provenance = []
    for path, record in sorted(records.items()):
        if "readback" not in record:
            continue
        occurrences = (occurrence for instance in record["readback"]["instances"] for occurrence in instance_occurrences(instance))
        for instance in occurrences:
            child_path = path + (instance["name"],)
            child = records.get(child_path)
            if child is None:
                raise ValidationError("Hierarchy omitted an instance", stage="netlist", code="dependency_missing")
            if child["status"] != "primitive":
                continue
            mappings = {}
            for connection in bit_connections(instance["connections"], instance_name=instance["name"]):
                identity = path, connection["net"]
                if identity not in parents:
                    raise ValidationError("Primitive terminal is unconnected or references an unknown bit", stage="netlist", code="netlist_mismatch")
                mappings.setdefault(connection["instance"], {})[connection["terminal"]] = names[find(identity)]
            for occurrence, connections in sorted(mappings.items()):
                name = occurrence if not path else "I_h_" + digest([path, occurrence])[:24]
                flat = copy.deepcopy(instance)
                flat.update(name=name, connections=connections, num_bits=1, bits=[name], bit_connections=bit_connections(connections, instance_name=name))
                instances.append(flat)
                instance_provenance.append({"instance": name, "path": list(path), "occurrence": occurrence, "master": instance["master"]})
                if len(instances) > max_instances:
                    raise ValidationError("Flattening exceeds the instance budget", stage="netlist", code="resource_limit")
    if len({item["name"] for item in instances}) != len(instances):
        raise ValidationError("Flattened instance names collide", stage="netlist", code="ambiguous_identity")
    dependencies = [{"target": record["target"], "sha256": record["readback"]["database_sha256"]} for record in hierarchy["records"] if "readback" in record]
    root = records[()]["readback"]
    return stable_readback({"schema_version": 2, "target": hierarchy["root"], "technology": root.get("technology"),
                            "instances": instances, "nets": sorted(names.values()), "pins": [], "wires": [],
                            "source": {"saved": True, "modified": False, "kind": "verified_electrical_projection", "dependencies": dependencies},
                            "database_sha256": digest(dependencies), "completeness": {"status": "complete", "scope": "electrical", "issues": []},
                            "scope": "electrical_only; original hierarchy remains the geometry authority", "net_aliases": aliases,
                            "net_provenance": provenance, "instance_provenance": instance_provenance,
                            "hierarchy_sha256": digest(hierarchy)})
