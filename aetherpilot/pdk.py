"""Offline catalog for the single certified tutorial PDK."""

from __future__ import annotations

import re
import hashlib
import os
from pathlib import Path

from .errors import ValidationError
from .contract import digest, numeric_text
from .artifacts import file_digest
from .snapshots import inventory



def _discover_default_pdk_root():
    configured = os.environ.get("AETHERPILOT_PDK_ROOT")
    if configured:
        return Path(configured)
    project_root = Path(__file__).resolve().parents[1]
    candidates = [project_root / "pdk", Path.cwd() / "pdk"]
    for base in (project_root / "resources", project_root / "input_files"):
        if not base.is_dir():
            continue
        try:
            candidates.extend(path.parent.parent for path in base.rglob("n18/device.def"))
        except OSError:
            continue
    for candidate in candidates:
        if (candidate / "n18" / "device.def").is_file() and (candidate / "p18" / "device.def").is_file():
            return candidate
    return Path("pdk")


DEFAULT_PDK_ROOT = _discover_default_pdk_root()


def _device_type(definition):
    for line in definition.splitlines():
        text = line.strip()
        if text and not text.startswith("["):
            return {"M": "mos", "Q": "bjt", "R": "resistor", "C": "capacitor", "D": "diode", "X": "subcircuit"}.get(text[0], "unknown")
    return "unknown"


def _terminals(definition):
    terminals = set()
    for line in definition.splitlines():
        if line.startswith(">"):
            for token in re.findall(r"<([A-Za-z][A-Za-z0-9_]*)>", line):
                terminals.add(token)
    return sorted(terminals)


def catalog(root=DEFAULT_PDK_ROOT):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError("Certified 018um_PDK directory is unavailable", stage="pdk", code="pdk_catalog_missing", details={"root": str(root)})
    file_entries = [{**entry, "sha256": file_digest(root / entry["path"])} for entry in inventory(root, limit_bytes=64 * 1024 ** 2)]
    devices = []
    for definition_path in sorted(root.glob("*/device.def")):
        name = definition_path.parent.name
        definition = definition_path.read_text(encoding="utf-8", errors="replace")
        template = parse_device_template(definition)
        devices.append({"cell": name, "device_type": _device_type(definition), "terminals": _terminals(definition),
                        "terminal_order": template["terminals"], "netlist_parameters": template["parameters"], "netlist_dialect": "hspice",
                        "views": {view: (definition_path.parent / view).is_dir() for view in ("symbol", "schematic", "layout")},
                        "device_def_sha256": hashlib.sha256(definition_path.read_bytes()).hexdigest(),
                        "device_directory_sha256": digest([entry for entry in file_entries if entry["path"].startswith(name + "/")]),
                        "certification": {"netlist_template": "validated_offline", **{operation: "native_not_certified" for operation in
                                          ("read", "create", "modify", "check", "save", "reopen", "netlist", "simulation", "scan", "monte_carlo")}}})
    if not devices:
        raise ValidationError("Certified PDK contains no device definitions", stage="pdk", code="pdk_catalog_empty")
    return {"schema_version": 1, "pdk": "018um_PDK", "root": str(root), "device_count": len(devices),
            "devices": devices, "pdk_sha256": digest(file_entries), "file_count": len(file_entries),
            "scope": "catalog_only; native per-device certification remains required"}


def parse_device_template(text):
    section = None
    templates = []
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"\[[A-Za-z0-9_]+\]", line):
            section = line.casefold()
        elif section == "[hspice]" and line.startswith(">"):
            templates.append(line)
    if len(templates) != 1 or not re.match(r">[MQDRCLX]\?\s", templates[0]):
        raise ValidationError("PDK device has no unambiguous supported netlist template", stage="pdk", code="unsupported_device_template")
    template = templates[0]
    fields = re.findall(r"\[@([A-Za-z_][A-Za-z0-9_]*)(?::([^\]]+))?\]", template)
    if any(formatting and not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*=)?%", formatting) for name, formatting in fields):
        raise ValidationError("PDK template uses an unsupported expression", stage="pdk", code="unsupported_device_template")
    terminals = re.findall(r"<([A-Za-z_][A-Za-z0-9_]*)>", template)
    if not terminals or len(terminals) != len(set(terminals)):
        raise ValidationError("PDK template terminal order is ambiguous", stage="pdk", code="unsupported_device_template")
    residue = re.sub(r"\[@[^\]]+\]|<[A-Za-z_][A-Za-z0-9_]*>", "", template[3:]).strip()
    if residue and any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", token) for token in residue.split()):
        raise ValidationError("PDK template contains unsupported literal syntax", stage="pdk", code="unsupported_device_template")
    return {"prefix": template[1], "terminals": terminals, "parameters": [name for name, formatting in fields], "template": template}


def render_device(instance, *, index, node_map, root=DEFAULT_PDK_ROOT):
    master = instance.get("master", {})
    cell = master.get("cell", "")
    if master.get("library") != "018um_PDK" or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", cell):
        raise ValidationError("Automatic device netlisting only supports the certified PDK", stage="netlist", code="unsupported_device")
    path = Path(root) / cell / "device.def"
    if not path.is_file():
        raise ValidationError("PDK device definition is missing", stage="netlist", code="device_definition_missing")
    source = path.read_bytes()
    parsed = parse_device_template(source.decode("utf-8"))
    connections = instance.get("connections", {})
    if set(connections) != set(parsed["terminals"]) or any(net not in node_map for net in connections.values()):
        raise ValidationError("Native device terminals differ from the PDK netlist template", stage="netlist", code="netlist_mismatch")
    parameters = instance.get("parameters", {})
    omitted = []

    def substitute(match):
        name, formatting = match.groups()
        value = parameters.get(name)
        if value is None or value == "":
            if name == "model" and cell == "primRes2" and parsed["prefix"] == "R" and "[@rprim]" in parsed["template"]:
                omitted.append(name)
                return ""
            if not formatting:
                raise ValidationError("A required PDK netlist value is unresolved", stage="netlist", code="parameter_unresolved", details={"instance": instance["name"], "parameter": name})
            omitted.append(name)
            return ""
        if formatting or name != "model":
            value = numeric_text(value, name)
        elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value):
            raise ValidationError("Invalid PDK model identifier", stage="netlist", code="parameter_unresolved")
        return formatting.replace("%", str(value)) if formatting else str(value)

    text = re.sub(r"\[@([A-Za-z_][A-Za-z0-9_]*)(?::([^\]]+))?\]", substitute, parsed["template"])
    text = re.sub(r"<([A-Za-z_][A-Za-z0-9_]*)>", lambda match: node_map[connections[match[1]]], text)
    name = parsed["prefix"] + "_AP" + str(index)
    text = name + text[3:]
    return " ".join(text.split()), {"instance": instance["name"], "spice_name": name, "cell": cell,
                                     "definition_sha256": hashlib.sha256(source).hexdigest(), "omitted_optional_parameters": omitted}
