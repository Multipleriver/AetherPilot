"""Bounded hierarchical SPICE/CDL/Spectre interchange.

This module deliberately sits beside :mod:`aetherpilot.netlist`.  The latter
is the exact, metadata-rich flat interchange used by the V1 acceptance
fixtures; this parser handles the useful hierarchical front-end subset without
weakening that exact round-trip contract.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import Any

from .contract import identifier, parameter_literal, signal_identifier, validate_plan
from .errors import ValidationError
from .pdk import DEFAULT_PDK_ROOT, catalog, parse_device_template
from .signals import expand_signal


_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_PREFIX_TYPE = {"M": "mos", "Q": "bjt", "D": "diode", "R": "resistor", "C": "capacitor"}
_DIRECTIVES = {"title", "end", "ends", "subckt", "include", "inc", "lib", "param", "global", "options", "simulator"}
_ALIASES = {
    "nmos": "n18", "pmos": "p18", "n18": "n18", "p18": "p18",
    "resistor": "rm1", "res": "rm1", "capacitor": "mim", "cap": "mim",
    "diode": "ndio18", "dio": "ndio18", "npn": "npn18_W2L2", "pnp": "pnp18_W2L2",
}


def _input_text(value: str | Path) -> tuple[str, str]:
    path = value if isinstance(value, Path) else Path(value) if isinstance(value, str) and "\n" not in value and len(value) < 4096 else None
    if path is not None and path.is_file():
        if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
            raise ValidationError("Netlist path is unsafe or exceeds the import budget", stage="netlist", code="resource_limit")
        return path.read_text(encoding="utf-8", errors="strict"), str(path.resolve())
    text = str(value)
    if len(text.encode()) > 64 * 1024 * 1024:
        raise ValidationError("Netlist exceeds the import budget", stage="netlist", code="resource_limit")
    return text, "<inline>"


def _logical_lines(text: str) -> list[str]:
    result: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("+"):
            if not result:
                raise ValidationError("Netlist continuation has no preceding record", stage="netlist", code="netlist_parse")
            result[-1] += " " + line[1:].strip()
        else:
            result.append(line)
    return result


def _tokens(line: str, spectre: bool) -> list[str]:
    # Spectre uses ``M1 (d g s b) n18`` while SPICE uses whitespace tokens.
    if spectre:
        match = re.match(r"^(\S+)\s*\(([^)]*)\)\s*(.*)$", line)
        if match:
            body = match.group(3).strip()
            return [match.group(1), *body.split(), *shlex.split(body)[:0]] if not body else [match.group(1), *match.group(2).split(), *shlex.split(body)]
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError as exc:
        raise ValidationError("Malformed netlist record", stage="netlist", code="netlist_parse") from exc


def _load_devices(pdk_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        entries = catalog(pdk_root)["devices"]
    except Exception as exc:
        raise ValidationError("Certified PDK catalog is unavailable", stage="netlist", code="pdk_catalog_missing") from exc
    by_cell = {entry["cell"].casefold(): entry for entry in entries}
    by_prefix: dict[str, dict[str, Any]] = {}
    for entry in entries:
        path = Path(pdk_root) / entry["cell"] / "device.def"
        parsed = parse_device_template(path.read_text(encoding="utf-8", errors="replace"))
        by_prefix.setdefault(parsed["prefix"].upper(), entry)
    return by_cell, by_prefix


def _parse_params(tokens: list[str], start: int) -> dict[str, str]:
    params: dict[str, str] = {}
    for token in tokens[start:]:
        if token.count("=") != 1:
            raise ValidationError("Netlist parameter assignment is malformed", stage="netlist", code="netlist_parse")
        name, raw = token.split("=", 1)
        identifier(name, "netlist parameter")
        if name in params:
            raise ValidationError("Netlist contains repeated parameter assignments", stage="netlist", code="netlist_duplicate_parameter")
        params[name] = parameter_literal(raw, name)
    return params


def _primitive(tokens: list[str], pdk_root: Path, devices: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]) -> dict[str, Any]:
    by_cell, by_prefix = devices
    if len(tokens) < 2 or not _SAFE_NAME.fullmatch(tokens[0]) or tokens[0][0].upper() not in _PREFIX_TYPE:
        raise ValidationError("Unsupported primitive record", stage="netlist", code="unsupported_netlist_device")
    prefix = tokens[0][0].upper()
    candidates = [by_cell.get(token.casefold()) for token in tokens[1:] if token.casefold() in by_cell]
    model_entry = candidates[-1] if candidates else by_prefix.get(prefix)
    if model_entry is None:
        terminal_count = {"R": 2, "C": 2, "D": 2, "Q": 3, "M": 4}.get(prefix)
        if terminal_count is None or len(tokens) < terminal_count + 2:
            raise ValidationError("Primitive has no configured model", stage="netlist", code="unsupported_netlist_device")
        model = tokens[terminal_count + 1]
        params = _parse_params(tokens, terminal_count + 2)
        terminal_names = {2: ("P", "N"), 3: ("C", "B", "E"), 4: ("D", "G", "S", "B")} [terminal_count]
        return {"name": tokens[0], "kind": "primitive", "model": model,
                "master": {"library": "external", "cell": model, "view": "symbol"},
                "terminal_order": list(terminal_names),
                "connections": {name: signal_identifier(net) for name, net in zip(terminal_names, tokens[1:terminal_count + 1])},
                "parameters": params, "origin": [0.0, 0.0], "orientation": "R0",
                "external_model": True}
    model = model_entry["cell"]
    parsed = parse_device_template((pdk_root / model / "device.def").read_text(encoding="utf-8", errors="replace"))
    terminal_count = len(parsed["terminals"])
    if len(tokens) < terminal_count + 2:
        raise ValidationError("Primitive terminal count is incomplete", stage="netlist", code="netlist_mismatch")
    terminals = tokens[1:terminal_count + 1]
    body_model = tokens[terminal_count + 1]
    if body_model.casefold() not in by_cell and body_model.casefold() not in _ALIASES:
        raise ValidationError("Primitive model is not in the certified PDK", stage="netlist", code="unsupported_device")
    params = _parse_params(tokens, terminal_count + 2)
    return {"name": tokens[0], "kind": "primitive", "model": model,
            "master": {"library": "018um_PDK", "cell": model, "view": "symbol"},
            "terminal_order": parsed["terminals"],
            "connections": {name: signal_identifier(net) for name, net in zip(parsed["terminals"], terminals)},
            "parameters": params, "origin": [0.0, 0.0], "orientation": "R0"}


def _child(tokens: list[str], children: dict[str, list[str]]) -> dict[str, Any]:
    if len(tokens) < 3 or not _SAFE_NAME.fullmatch(tokens[0]):
        raise ValidationError("Malformed hierarchical instance", stage="netlist", code="netlist_parse")
    # Parameters follow the child name; the preceding tokens are positional
    # ports.  This is deterministic and avoids guessing from signal names.
    first_param = next((index for index, token in enumerate(tokens[1:], 1) if "=" in token), len(tokens))
    child_name = tokens[first_param - 1]
    if child_name not in children:
        raise ValidationError("Hierarchical instance references an undefined subcircuit", stage="netlist", code="undefined_subcircuit", details={"subcircuit": child_name})
    ports = children[child_name]
    nets = tokens[1:first_param - 1]
    if len(nets) != len(ports):
        raise ValidationError("Hierarchical instance port width differs from its subcircuit", stage="netlist", code="netlist_mismatch", details={"subcircuit": child_name, "expected": ports, "actual": nets})
    return {"name": tokens[0], "kind": "hierarchical", "subcircuit": child_name,
            "terminal_order": ports, "connections": {port: signal_identifier(net) for port, net in zip(ports, nets)},
            "parameters": _parse_params(tokens, first_param), "origin": [0.0, 0.0], "orientation": "R0"}


def _cell_name(root: str, subckt: str) -> str:
    value = f"{root}__{subckt}"
    if len(value) <= 80:
        return value
    return value[:63] + "__" + hashlib.sha256(value.encode()).hexdigest()[:14]


def parse_hierarchical_netlist(text_or_path: str | Path, *, target: dict[str, Any], pdk_root: str | Path | None = None,
                               netlist_format: str = "auto", max_subcircuits: int = 256, max_instances: int = 10000) -> dict[str, Any]:
    """Parse bounded hierarchical SPICE/CDL/Spectre into independent plans.

    The result is a dry-run/materialization IR: ``plans`` are ordered children
    first and the root last, so a caller can create child schematics and symbols
    before placing the root instance.  No file or native database is touched.
    """
    if not isinstance(target, dict) or not target.get("library") or not target.get("cell"):
        raise ValidationError("Hierarchical import requires a target library and cell", stage="netlist", code="netlist_input")
    if netlist_format not in {"auto", "spice", "cdl", "spectre"}:
        raise ValidationError("Unsupported hierarchical netlist format", stage="netlist", code="unsupported_netlist_format")
    text, source = _input_text(text_or_path)
    pdk = Path(pdk_root or DEFAULT_PDK_ROOT).expanduser().resolve()
    devices = _load_devices(pdk)
    lines = _logical_lines(text)
    spectre = netlist_format == "spectre" or any(line.casefold().startswith("simulator lang=spectre") for line in lines)
    blocks: dict[str, dict[str, Any]] = {}
    stack: list[str] = []
    top_lines: list[str] = []
    directives: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("*") or stripped.startswith("//"):
            continue
        lower = stripped.casefold()
        if lower.startswith("simulator lang="):
            spectre = "spectre" in lower
            continue
        if lower.startswith(".subckt") or lower.startswith("subckt "):
            fields = stripped.split()
            if len(fields) < 3 or len(stack) or len(blocks) >= max_subcircuits:
                raise ValidationError("Nested or excessive subcircuit declarations are not supported", stage="netlist", code="resource_limit")
            name = identifier(fields[1], "subcircuit")
            ports = [signal_identifier(item.strip("(),")) for item in fields[2:] if item.strip("(),")]
            if len(ports) != len(set(ports)) or name in blocks:
                raise ValidationError("Subcircuit names or ports are duplicated", stage="netlist", code="netlist_mismatch")
            blocks[name] = {"name": name, "ports": ports, "lines": []}
            stack.append(name)
            continue
        if lower.startswith(".ends") or lower == "ends" or lower.startswith("ends "):
            if not stack:
                raise ValidationError("Unmatched subcircuit end", stage="netlist", code="netlist_parse")
            fields = stripped.split()
            if len(fields) > 2 or (len(fields) == 2 and fields[1] != stack[-1]):
                raise ValidationError("Subcircuit end name does not match its declaration", stage="netlist", code="netlist_parse")
            stack.pop()
            continue
        if stack:
            blocks[stack[-1]]["lines"].append(stripped)
        else:
            top_lines.append(stripped)
    if stack:
        raise ValidationError("Unterminated subcircuit", stage="netlist", code="netlist_parse")
    if not blocks:
        raise ValidationError("Hierarchical import requires at least one subcircuit", stage="netlist", code="unsupported_netlist_hierarchy")
    if top_lines and any(not line.startswith(".") for line in top_lines):
        # A top-level body is permitted only as the root wrapper; requiring a
        # named .subckt keeps the target and port order explicit.
        raise ValidationError("Top-level devices require a named root subcircuit", stage="netlist", code="unsupported_netlist_hierarchy")
    for line in top_lines:
        if line.startswith("."):
            directive = line.split()[0][1:].casefold()
            if directive not in _DIRECTIVES:
                raise ValidationError("Unsupported netlist directive", stage="netlist", code="unsupported_netlist_directive")
            directives.append({"scope": "<top>", "text": line})
    children = {name: value["ports"] for name, value in blocks.items()}
    root = next((name for name in blocks if name.casefold() in {"top", str(target["cell"]).casefold()}), None)
    if root is None:
        referenced = set()
        for block in blocks.values():
            for line in block["lines"]:
                if not line or line[0].upper() != "X":
                    continue
                tokens = _tokens(line, spectre)
                first_param = next((index for index, token in enumerate(tokens[1:], 1) if "=" in token), len(tokens))
                if first_param >= 2:
                    referenced.add(tokens[first_param - 1])
        roots = sorted(set(blocks) - referenced)
        if len(roots) != 1:
            raise ValidationError("Root subcircuit is ambiguous; name one top-level subcircuit top", stage="netlist", code="ambiguous_root")
        root = roots[0]
    visiting: set[str] = set()
    done: set[str] = set()
    ordered: list[str] = []

    def visit(name: str):
        if name in visiting:
            raise ValidationError("Cyclic subcircuit dependency", stage="netlist", code="hierarchy_cycle", details={"subcircuit": name})
        if name in done:
            return
        visiting.add(name)
        for line in blocks[name]["lines"]:
            tokens = _tokens(line, spectre)
            if tokens and tokens[0][0].upper() == "X":
                first_param = next((index for index, token in enumerate(tokens[1:], 1) if "=" in token), len(tokens))
                if first_param < 2 or tokens[first_param - 1] not in blocks:
                    raise ValidationError("Hierarchical instance references an undefined subcircuit", stage="netlist", code="undefined_subcircuit")
                visit(tokens[first_param - 1])
        visiting.remove(name)
        done.add(name)
        ordered.append(name)

    visit(root)
    if len(done) != len(blocks):
        raise ValidationError("Netlist contains an unreachable subcircuit", stage="netlist", code="unused_subcircuit")
    total_instances = 0
    plans = []
    for name in ordered:
        block = blocks[name]
        instances = []
        hierarchy_instances = {}
        seen = set()
        for line in block["lines"]:
            if not line or line.startswith("."):
                directive = line.split()[0][1:].casefold() if line.startswith(".") else ""
                if directive not in _DIRECTIVES:
                    raise ValidationError("Unsupported netlist directive", stage="netlist", code="unsupported_netlist_directive")
                directives.append({"scope": name, "text": line})
                continue
            tokens = _tokens(line, spectre)
            if not tokens:
                continue
            if tokens[0][0].upper() == "X":
                instance = _child(tokens, children)
            else:
                instance = _primitive(tokens, pdk, devices)
            if instance["name"] in seen:
                raise ValidationError("Duplicate instance in subcircuit", stage="netlist", code="netlist_duplicate_instance")
            seen.add(instance["name"])
            if sum(len(expand_signal(net)) for net in instance["connections"].values()) > max_instances:
                raise ValidationError("Netlist signal expansion exceeds its budget", stage="netlist", code="resource_limit")
            if instance.get("kind") == "hierarchical":
                child_name = instance["subcircuit"]
                hierarchy_instances[instance["name"]] = child_name
                instance = {"name": instance["name"], "master": {"library": target["library"], "cell": _cell_name(str(target["cell"]), child_name), "view": "symbol"},
                            "origin": instance["origin"], "orientation": instance["orientation"], "parameters": instance["parameters"], "connections": instance["connections"]}
            else:
                instance = {"name": instance["name"], "master": instance["master"], "origin": instance["origin"],
                            "orientation": instance["orientation"], "parameters": instance["parameters"], "connections": instance["connections"]}
            instances.append(instance)
            total_instances += 1
            if total_instances > max_instances:
                raise ValidationError("Netlist instance budget exceeded", stage="netlist", code="resource_limit")
        nets = sorted({net for instance in instances for net in instance["connections"].values()} | set(block["ports"]))
        pins = [{"name": port, "net": port, "direction": "inputOutput", "position": [-4.0, float(index * 2)], "orientation": "R0"}
                for index, port in enumerate(block["ports"])]
        cell = str(target["cell"]) if name == root else _cell_name(str(target["cell"]), name)
        plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": 0.0625,
                "target": {"library": target["library"], "cell": cell, "view": "schematic", "technology": target.get("technology", "018um_PDK")},
                "instances": instances, "nets": nets, "pins": pins, "wires": [], "labels": [], "inherited_connections": [], "port_order": block["ports"],
                "hierarchy_instances": hierarchy_instances}
        hierarchy_meta = plan.pop("hierarchy_instances")
        validated_plan = validate_plan(plan)
        validated_plan["hierarchy_instances"] = hierarchy_meta
        plans.append(validated_plan)
    dependencies = [{"subcircuit": name, "cell": _cell_name(str(target["cell"]), name)} for name in ordered if name != root]
    directive_review = [item for item in directives if item["text"].split()[0][1:].casefold() in {"include", "inc", "lib", "param", "global", "options"}]
    issues = ["geometry_is_synthesized", "native_materialization_requires_child_symbols"]
    if directive_review:
        issues.append("external_directive_review_required")
    return {"schema_version": 1, "format": "hierarchical-spice-cdl-spectre", "source": source,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "format_detected": "spectre" if spectre else (netlist_format if netlist_format != "auto" else "spice"),
            "root": root, "root_target": plans[-1]["target"], "plans": plans, "dependencies": dependencies,
            "directives": directives, "completeness": {"status": "draft", "issues": issues},
            "materialization": {"supported": False, "reason": "dry_run_parser_only; child symbol generation must be explicitly verified"}}


def export_hierarchical_netlist(hierarchy: dict[str, Any], *, output: str | Path | None = None, netlist_format: str = "spice") -> dict[str, Any]:
    """Export parsed plans as deterministic SPICE subcircuits."""
    if not isinstance(hierarchy, dict) or not isinstance(hierarchy.get("plans"), list) or not hierarchy["plans"]:
        raise ValidationError("Hierarchical export requires parsed plans", stage="netlist", code="netlist_input")
    if netlist_format not in {"spice", "cdl", "spectre"}:
        raise ValidationError("Unsupported hierarchical export format", stage="netlist", code="unsupported_netlist_format")
    lines = ["* AETHERPILOT-HIERARCHICAL-NETLIST 1"]
    if netlist_format == "spectre":
        lines.append("simulator lang=spectre")
    root = hierarchy.get("root")
    emitted = set()
    cell_to_name = {item.get("cell"): item.get("subcircuit") for item in hierarchy.get("dependencies", [])}
    name_to_cell = {name: cell for cell, name in cell_to_name.items()}
    for plan in hierarchy["plans"]:
        cell = plan["target"]["cell"]
        name = root if cell == root else cell_to_name.get(cell, cell)
        if name in emitted:
            continue
        emitted.add(name)
        ports = plan.get("port_order", [pin["name"] for pin in plan.get("pins", [])])
        lines.append(("subckt " if netlist_format == "spectre" else ".subckt ") + (name + " (" + " ".join(ports) + ")" if netlist_format == "spectre" else " ".join([name, *ports])))
        hierarchy_instances = plan.get("hierarchy_instances", {})
        for instance in sorted(plan.get("instances", []), key=lambda item: item["name"]):
            if instance["name"] in hierarchy_instances:
                child_name = hierarchy_instances[instance["name"]]
                child_cell = name_to_cell.get(child_name, child_name)
                child_plan = next((candidate for candidate in hierarchy["plans"] if candidate["target"]["cell"] == child_cell), None)
                if child_plan is None:
                    raise ValidationError("Hierarchical export child has no plan", stage="netlist", code="undefined_subcircuit", details={"child": child_name})
                child_ports = child_plan.get("port_order", [pin["name"] for pin in child_plan.get("pins", [])])
                terminals = [instance["connections"][terminal] for terminal in child_ports]
                tail = " ".join([child_name, *[f"{k}={v}" for k, v in sorted(instance.get("parameters", {}).items())]])
                child_instance_name = instance["name"] if instance["name"].startswith("X") else "X" + instance["name"]
                lines.append(f"{child_instance_name} ({' '.join(terminals)}) {tail}" if netlist_format == "spectre" else " ".join([child_instance_name, *terminals, tail]))
            else:
                master = instance["master"]["cell"]
                if set(instance["connections"]) == {"D", "G", "S", "B"}:
                    terminal_order = ("D", "G", "S", "B")
                else:
                    terminal_order = tuple(instance["connections"])
                terminals = [instance["connections"][terminal] for terminal in terminal_order]
                tail = " ".join([master, *[f"{k}={v}" for k, v in sorted(instance.get("parameters", {}).items())]])
                lines.append(f"{instance['name']} ({' '.join(terminals)}) {tail}" if netlist_format == "spectre" else " ".join([instance["name"], *terminals, tail]))
        lines.append(("ends " if netlist_format == "spectre" else ".ends ") + name)
    lines.append(".end")
    text = "\n".join(lines) + "\n"
    result = {"schema_version": 1, "format": "hierarchical-" + netlist_format, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
              "netlist_format": netlist_format, "subcircuit_count": len(emitted), "root": root}
    if output is not None:
        path = Path(output).expanduser().resolve()
        if path.exists():
            raise ValidationError("Netlist destination already exists", stage="netlist", code="target_exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result["output"] = str(path)
    return result


def plans_from_hierarchy_readback(hierarchy: dict[str, Any]) -> dict[str, Any]:
    """Project a complete native ``read_hierarchy`` result to export plans.

    Only complete records are accepted.  Every non-primitive child must have a
    unique scalar path; arrays are intentionally rejected instead of being
    flattened into a potentially different electrical topology.
    """
    if not isinstance(hierarchy, dict) or hierarchy.get("complete") is not True or not isinstance(hierarchy.get("records"), list):
        raise ValidationError("Hierarchical export requires complete native readback", stage="netlist", code="incomplete_readback")
    records = {tuple(record.get("path", [])): record for record in hierarchy["records"] if isinstance(record, dict) and "readback" in record}
    root_record = records.get(())
    if root_record is None:
        raise ValidationError("Hierarchical readback has no root record", stage="netlist", code="netlist_input")
    plans = []
    ordered = sorted(records, key=lambda path: (len(path), path))
    for path in ordered:
        rb = records[path]["readback"]
        target = rb.get("target")
        if not isinstance(target, dict):
            raise ValidationError("Hierarchical readback target is missing", stage="netlist", code="netlist_metadata")
        children = {}
        instances = []
        for instance in rb.get("instances", []):
            item = {key: instance.get(key) for key in ("name", "master", "origin", "orientation", "parameters", "connections")}
            child_path = path + (instance["name"],)
            child = records.get(child_path)
            if child is not None and child.get("status") == "complete":
                child_target = child["readback"].get("target", {})
                children[instance["name"]] = child_target.get("cell")
                item["master"] = {"library": child_target.get("library"), "cell": child_target.get("cell"), "view": "symbol"}
            instances.append(item)
        target = {**target, "view": "schematic", "technology": target.get("technology", "018um_PDK")}
        pins = []
        for pin in rb.get("pins", []):
            pins.append({"name": pin.get("name"), "net": pin.get("net"), "direction": pin.get("direction", "inputOutput"),
                         "position": pin.get("position", [0.0, 0.0]), "orientation": pin.get("orientation", "R0")})
        inherited = []
        for item in rb.get("inherited_connections", []):
            if not isinstance(item, dict) or not item.get("property") or not item.get("default"):
                continue
            pin_name = item.get("pin") or next((entry.get("terminal") for entry in item.get("evidence", []) if isinstance(entry, dict)), None)
            if pin_name:
                inherited.append({"property": item["property"], "default": item["default"], "pin": pin_name})
        wires = [{key: wire.get(key) for key in ("net", "points")} for wire in rb.get("wires", [])]
        plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": 0.0625,
                "target": target, "instances": instances, "nets": rb.get("nets", []), "pins": pins,
                "wires": wires, "labels": [], "inherited_connections": inherited,
                "port_order": rb.get("port_order", [pin["name"] for pin in rb.get("pins", [])])}
        validated = validate_plan(plan)
        validated["hierarchy_instances"] = children
        plans.append(validated)
    root = root_record["readback"].get("target", {}).get("cell")
    dependencies = [{"subcircuit": plan["target"]["cell"], "cell": plan["target"]["cell"]} for plan in plans if plan["target"]["cell"] != root]
    return {"schema_version": 1, "format": "hierarchical-native-readback", "root": root, "plans": plans,
            "dependencies": dependencies, "completeness": {"status": "complete", "issues": []},
            "materialization": {"supported": False, "reason": "export-only projection"}}


__all__ = ["parse_hierarchical_netlist", "export_hierarchical_netlist", "plans_from_hierarchy_readback"]
