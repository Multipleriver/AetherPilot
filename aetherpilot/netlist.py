"""Strict schematic netlist interchange for the certified AetherPilot surface."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shlex
from pathlib import Path

from .contract import digest, engineering, identifier, parameter_literal, signal_identifier, validate_plan
from .errors import ValidationError
from .pdk import DEFAULT_PDK_ROOT, parse_device_template
from .readback import readback_is_complete, stable_readback
from .schematic_content import schematic_content


_HEADER = "* AETHERPILOT-NETLIST 1"
_SAFE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_PREFIX_TO_TYPE = {"M": "mos", "Q": "bjt", "D": "diode", "R": "resistor", "C": "capacitor", "X": "subcircuit"}


def _record(prefix, value):
    encoded = base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).decode()
    return f"* AP-{prefix} {encoded}"


def _decode_record(prefix, line):
    marker = f"* AP-{prefix} "
    if not line.startswith(marker):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(line[len(marker):].encode()).decode())
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("Malformed AetherPilot netlist metadata", stage="netlist", code="netlist_metadata") from exc


def _safe_model(value):
    if not isinstance(value, str) or not _SAFE_SYMBOL.fullmatch(value):
        raise ValidationError("Netlist model names must be simple identifiers", stage="netlist", code="netlist_input")
    return value


def _device_line(instance, model, terminal_order):
    name = "X" + instance["name"]
    if set(instance["connections"]) != set(terminal_order):
        raise ValidationError("Schematic connections do not match the PDK terminal set", stage="netlist", code="netlist_mismatch",
                              details={"instance": instance["name"], "expected": terminal_order,
                                       "actual": sorted(instance["connections"])})
    terminals = [instance["connections"][terminal] for terminal in terminal_order]
    parameters = [f"{key}={parameter_literal(value, key)}" for key, value in sorted(instance.get("parameters", {}).items())]
    return " ".join([name, *terminals, _safe_model(model), *parameters])


def _explicit_parameters(instance):
    properties = instance.get("properties")
    if not isinstance(properties, list):
        return dict(instance.get("parameters", {}))
    explicit = {}
    for property_value in properties:
        if not isinstance(property_value, dict) or not isinstance(property_value.get("name"), str):
            raise ValidationError("Netlist instance property metadata is malformed", stage="netlist", code="netlist_metadata")
        name = property_value["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValidationError("Netlist instance property name is unsafe", stage="netlist", code="netlist_metadata")
        if "value" not in property_value:
            raise ValidationError("Netlist instance property value is missing", stage="netlist", code="netlist_metadata")
        explicit[name] = property_value["value"]
    return explicit


def export_schematic_netlist(readback, *, device_models=None, operation_id=None, output=None, pdk_root=DEFAULT_PDK_ROOT):
    """Export a complete readback with a standard-like body and exact metadata.

    The metadata is intentionally explicit: a flat SPICE line cannot preserve
    schematic geometry, annotation kind, or master view by itself. Consumers
    that do not understand the metadata can still inspect the device lines.
    """
    pdk_root = pdk_root or DEFAULT_PDK_ROOT
    value = stable_readback(copy.deepcopy(readback))
    if not readback_is_complete(value):
        raise ValidationError("Netlist export requires complete schematic readback", stage="netlist", code="incomplete_readback")
    if "records" in value:
        raise ValidationError("Netlist export requires a flat schematic readback", stage="netlist", code="hierarchical_netlist_required")
    models = device_models or {}
    lines = [_HEADER, _record("META", {"schema_version": 1, "target": value.get("target"),
                                      "content_version": 2 if "port_order" in value else 1, "port_order": value.get("port_order"),
                                      "technology": value.get("technology"), "content_sha256": digest(schematic_content(value)),
                                      "operation_id": operation_id, "instance_count": len(value.get("instances", [])),
                                      "pin_count": len(value.get("pins", [])), "wire_count": len(value.get("wires", [])),
                                      "net_count": len(value.get("nets", []))})]
    for instance in sorted(value.get("instances", []), key=lambda item: item["name"]):
        model = models.get(instance["name"], instance.get("master", {}).get("cell"))
        if isinstance(model, dict):
            model = model.get("model", instance.get("master", {}).get("cell"))
        master = instance.get("master", {})
        if master.get("library") != "018um_PDK" or not isinstance(master.get("cell"), str):
            raise ValidationError("Netlist export only supports the certified 018um_PDK masters", stage="netlist", code="unsupported_device")
        definition = Path(pdk_root) / master["cell"] / "device.def"
        if not definition.is_file() or definition.is_symlink():
            raise ValidationError("Netlist export cannot resolve the certified PDK device", stage="netlist", code="unsupported_device",
                                  details={"instance": instance["name"], "cell": master["cell"]})
        terminal_order = parse_device_template(definition.read_text(encoding="utf-8", errors="replace"))["terminals"]
        lines.append(_record("MASTER", {"instance": instance["name"], "master": instance["master"], "body_model": model}))
        projected = {key: instance.get(key) for key in ("name", "origin", "orientation", "connections")}
        projected["parameters"] = _explicit_parameters(instance)
        lines.append(_record("INSTANCE", projected))
        lines.append(_device_line({**instance, "parameters": projected["parameters"]}, model, terminal_order))
    for pin in sorted(value.get("pins", []), key=lambda item: item["name"]):
        lines.append(_record("PIN", {key: pin.get(key) for key in ("name", "net", "direction", "position", "orientation")}))
    for wire in value.get("wires", []):
        lines.append(_record("WIRE", {"net": wire["net"], "points": wire["points"]}))
    for key in ("labels", "texts"):
        for item in value.get(key, []):
            if key == "texts" and str(item.get("kind", "")).casefold() in {"attrdisplay", "propdisplay", "instattrdisplay", "evaltext"}:
                continue
            lines.append(_record(key.upper(), item))
    for item in value.get("inherited_connections", []):
        evidence = item.get("evidence", []) if isinstance(item, dict) else []
        pin = item.get("pin") if isinstance(item, dict) else None
        if pin is None:
            pin = next((entry.get("terminal") for entry in evidence if isinstance(entry, dict) and entry.get("terminal")), None)
        if not isinstance(pin, str) or not pin:
            raise ValidationError("Inherited connection lacks a stable attachment pin", stage="netlist", code="unsupported_netlist_feature",
                                  details={"property": item.get("property") if isinstance(item, dict) else None})
        lines.append(_record("INHERITED_CONNECTIONS", {"property": item["property"], "default": item["default"], "pin": pin, "raw": item.get("raw")}))
    lines.append(".end")
    text = "\n".join(lines) + "\n"
    result = {"schema_version": 1, "format": "aetherpilot-spice+metadata", "text": text,
              "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "source_sha256": digest(schematic_content(value)),
              "instance_count": len(value.get("instances", [])), "net_count": len(value.get("nets", [])),
              "metadata_complete": True}
    if output is not None:
        path = Path(output).expanduser().resolve()
        if path.exists():
            raise ValidationError("Netlist destination already exists", stage="netlist", code="target_exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result["output"] = str(path)
    return result


def _parse_device_line(line, pdk_root, *, master=None, body_model=None, native_parameters=False):
    if master is not None:
        if not isinstance(master, dict) or set(master) != {"library", "cell", "view"} or master["library"] != "018um_PDK" or master["view"] != "symbol":
            raise ValidationError("Netlist metadata references an unsupported master", stage="netlist", code="netlist_metadata")
        identifier(master["cell"], "netlist master cell")
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError as exc:
        raise ValidationError("Malformed netlist device line", stage="netlist", code="netlist_parse") from exc
    if len(tokens) < 3 or not tokens[0] or tokens[0][0].upper() not in _PREFIX_TO_TYPE:
        raise ValidationError("Unsupported netlist device line", stage="netlist", code="unsupported_netlist_device")
    name = tokens[0][1:]
    identifier(name, "netlist instance")
    model_candidates = []
    definitions = [Path(pdk_root) / master["cell"] / "device.def"] if master else sorted(Path(pdk_root).glob("*/device.def"))
    for model_path in definitions:
        model = model_path.parent.name
        expected_model = body_model or model
        if expected_model not in tokens or not model_path.is_file() or model_path.is_symlink():
            continue
        parsed = parse_device_template(model_path.read_text(encoding="utf-8", errors="replace"))
        model_index = len(parsed["terminals"]) + 1
        if len(tokens) > model_index and tokens[model_index] == expected_model:
            model_candidates.append((model, model_path, parsed, model_index))
    if len(model_candidates) != 1:
        raise ValidationError("Netlist device has no model", stage="netlist", code="netlist_parse")
    model, model_path, parsed, model_index = model_candidates[0]
    if tokens[0][0].upper() not in {"X", parsed["prefix"]}:
        raise ValidationError("Netlist primitive prefix differs from the PDK device", stage="netlist", code="netlist_mismatch")
    _safe_model(model)
    terminals = tokens[1:model_index]
    if len(terminals) != len(parsed["terminals"]):
        raise ValidationError("Netlist terminal count does not match the PDK definition", stage="netlist", code="netlist_mismatch",
                              details={"model": model, "expected": parsed["terminals"], "actual_count": len(terminals)})
    parameters = {}
    aliases = {formatting[:-2].casefold(): name for name, formatting in re.findall(r"\[@([A-Za-z_][A-Za-z0-9_]*):([^\]]+)\]", parsed["template"]) if formatting.endswith("=%")}
    for token in tokens[model_index + 1:]:
        if token.count("=") != 1:
            raise ValidationError("Netlist parameter assignment is malformed", stage="netlist", code="netlist_parse")
        key, raw = token.split("=", 1)
        identifier(key, "netlist parameter")
        if not native_parameters:
            key = aliases.get(key.casefold(), key)
        if key in parameters:
            raise ValidationError("Netlist contains repeated parameter assignments", stage="netlist", code="netlist_duplicate_parameter")
        parameters[key] = parameter_literal(raw, key)
    connections = {terminal: signal_identifier(net) for terminal, net in zip(parsed["terminals"], terminals)}
    return {"name": name, "master": {"library": "018um_PDK", "cell": model, "view": "symbol"},
            "orientation": "R0", "parameters": parameters, "connections": connections}


def import_schematic_netlist(text_or_path, *, target, pdk_root=DEFAULT_PDK_ROOT, require_metadata=False):
    """Import the safe flat subset and return a validated ``schematic.apply`` plan.

    Exact round trips use AP metadata. External flat netlists are accepted only
    when their PDK terminal order is unambiguous; geometry is then generated as
    a deterministic draft and is never presented as original layout evidence.
    """
    pdk_root = pdk_root or DEFAULT_PDK_ROOT
    input_path = text_or_path if isinstance(text_or_path, Path) else Path(text_or_path) if isinstance(text_or_path, str) and len(text_or_path) < 4096 and "\n" not in text_or_path else None
    if input_path is not None and input_path.is_file():
        if input_path.is_symlink() or input_path.stat().st_size > 64 * 1024 ** 2:
            raise ValidationError("Netlist path is unsafe or exceeds the import budget", stage="netlist", code="resource_limit")
        text = input_path.read_text(encoding="utf-8")
    else:
        text = str(text_or_path)
    if len(text.encode()) > 64 * 1024 * 1024:
        raise ValidationError("Netlist exceeds the import budget", stage="netlist", code="resource_limit")
    instances = {}
    masters = {}
    body_models = {}
    body_lines = []
    subcircuit = None
    subcircuit_closed = False
    external_ports = None
    ended = False
    pins = []
    wires = []
    metadata = None
    annotations = []
    inherited = []
    seen_records = set()
    logical_lines = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("+"):
            if not logical_lines or not re.match(r"[MQDRCX]", logical_lines[-1], re.IGNORECASE):
                raise ValidationError("Netlist continuation has no device record", stage="netlist", code="netlist_parse")
            logical_lines[-1] += " " + raw.lstrip()[1:].strip()
        else:
            logical_lines.append(raw.strip())
    for raw in logical_lines:
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            for prefix, destination in (("META", "metadata"), ("INSTANCE", "instance"), ("MASTER", "master"), ("PIN", "pin"), ("WIRE", "wire"), ("LABELS", "labels"), ("TEXTS", "texts"), ("INHERITED_CONNECTIONS", "inherited")):
                item = _decode_record(prefix, line)
                if item is None:
                    continue
                if destination == "metadata":
                    if destination in seen_records:
                        raise ValidationError("Netlist contains duplicate metadata records", stage="netlist", code="netlist_metadata")
                    metadata = item
                elif destination == "instance":
                    if not isinstance(item, dict) or not isinstance(item.get("name"), str) or item["name"] in instances:
                        raise ValidationError("Netlist contains duplicate or invalid instance metadata", stage="netlist", code="netlist_metadata")
                    instances[item["name"]] = item
                elif destination == "master":
                    if not isinstance(item, dict) or not isinstance(item.get("instance"), str) or item["instance"] in masters:
                        raise ValidationError("Netlist contains duplicate or invalid master metadata", stage="netlist", code="netlist_metadata")
                    masters[item["instance"]] = item["master"]
                    body_models[item["instance"]] = item.get("body_model")
                elif destination == "pin":
                    pins.append(item)
                elif destination == "wire":
                    wires.append(item)
                elif destination == "labels":
                    annotations.append(item)
                elif destination == "texts":
                    annotations.append(item)
                elif destination == "inherited":
                    inherited.append(item)
                seen_records.add(destination)
                break
            continue
        if ended:
            raise ValidationError("Netlist contains content after its end marker", stage="netlist", code="netlist_parse")
        if line.casefold() == ".end":
            ended = True
            continue
        if line.casefold().startswith(".title "):
            continue
        if line.casefold().startswith(".subckt "):
            tokens = line.split()
            if subcircuit is not None or body_lines or len(tokens) < 3:
                raise ValidationError("Only one flat subcircuit wrapper is supported", stage="netlist", code="unsupported_netlist_hierarchy")
            subcircuit = identifier(tokens[1], "subcircuit")
            external_ports = [signal_identifier(name) for name in tokens[2:]]
            if len(external_ports) != len(set(external_ports)):
                raise ValidationError("Repeated subcircuit ports", stage="netlist", code="netlist_mismatch")
            continue
        if line.split()[0].casefold() == ".ends":
            tokens = line.split()
            if subcircuit is None or subcircuit_closed or len(tokens) > 2 or len(tokens) == 2 and tokens[1] != subcircuit:
                raise ValidationError("Unmatched subcircuit end", stage="netlist", code="netlist_parse")
            subcircuit_closed = True
            continue
        if line.startswith(".") or subcircuit_closed:
            raise ValidationError("Unsupported netlist directive or hierarchy", stage="netlist", code="unsupported_netlist_directive")
        body_lines.append(line)
    if subcircuit is not None and not subcircuit_closed:
        raise ValidationError("Unterminated subcircuit", stage="netlist", code="netlist_parse")
    body_names = set()
    for line in body_lines:
        name = line.split()[0][1:]
        parsed = _parse_device_line(line, pdk_root, master=masters.get(name), body_model=body_models.get(name), native_parameters=metadata is not None)
        if parsed["name"] in body_names:
            raise ValidationError("Netlist contains duplicate devices", stage="netlist", code="netlist_duplicate_instance")
        body_names.add(parsed["name"])
        if parsed["name"] in instances:
            expected = instances[parsed["name"]]
            if any(expected.get(field) != parsed[field] for field in ("connections", "parameters")):
                raise ValidationError("Netlist body contradicts its metadata", stage="netlist", code="netlist_metadata_mismatch")
        else:
            instances[parsed["name"]] = parsed
    if require_metadata and metadata is None:
        raise ValidationError("Netlist metadata is required for exact import", stage="netlist", code="metadata_required")
    if metadata is not None:
        if type(metadata.get("content_version", 1)) is not int or metadata.get("content_version", 1) not in {1, 2}:
            raise ValidationError("Unsupported netlist content version", stage="netlist", code="netlist_metadata")
        if set(instances) != body_names:
            raise ValidationError("Netlist metadata has no matching device body", stage="netlist", code="netlist_metadata_mismatch")
        if metadata.get("schema_version") != 1 or not isinstance(metadata.get("content_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", metadata["content_sha256"]):
            raise ValidationError("Netlist metadata schema or content hash is invalid", stage="netlist", code="netlist_metadata")
        if metadata.get("instance_count") is not None and metadata["instance_count"] != len(instances):
            raise ValidationError("Netlist instance metadata count does not match device records", stage="netlist", code="netlist_metadata")
        for name, master in masters.items():
            if name not in instances or not isinstance(master, dict):
                raise ValidationError("Netlist master metadata does not match an instance", stage="netlist", code="netlist_metadata")
    labels = []
    for item in annotations:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("origin"), list):
            raise ValidationError("Netlist annotation metadata is malformed", stage="netlist", code="netlist_metadata")
        kind = str(item.get("kind", "")).casefold()
        if kind in {"attrdisplay", "propdisplay", "instattrdisplay", "evaltext"}:
            continue
        layer, purpose = item.get("layer"), item.get("purpose")
        if layer == "wire" and purpose == "label" and item.get("net"):
            annotation_kind = "wire_name"
        elif layer == "text" and purpose == "drawing":
            annotation_kind = "note"
        else:
            annotation_kind = "label"
        label = {key: item.get(key) for key in ("text", "origin", "orientation", "height", "layer", "purpose", "justification", "font", "properties")}
        label["kind"] = annotation_kind
        if annotation_kind == "wire_name":
            label["net"] = item["net"]
        labels.append(label)
    inherited_connections = []
    for item in inherited:
        if not isinstance(item, dict) or set(item) - {"property", "default", "pin", "raw"} or not {"property", "default", "pin"} <= set(item):
            raise ValidationError("Netlist inherited connection metadata is malformed", stage="netlist", code="netlist_metadata")
        inherited_connections.append({key: item[key] for key in ("property", "default", "pin")})
    if not instances:
        raise ValidationError("Netlist contains no supported devices", stage="netlist", code="netlist_empty")
    for name, instance in instances.items():
        if name in masters:
            instance["master"] = masters[name]
        if name not in instances:
            continue
        if "origin" not in instance:
            index = sorted(instances).index(name)
            instance["origin"] = [float(index * 4), 0.0]
        instance.setdefault("orientation", "R0")
        instance.setdefault("parameters", {})
    if not pins:
        nets = sorted({net for instance in instances.values() for net in instance["connections"].values()})
        if external_ports is not None and any(net not in nets for net in external_ports):
            raise ValidationError("Subcircuit has an unconnected external port", stage="netlist", code="netlist_mismatch")
        pins = [{"name": net, "net": net, "direction": "inputOutput", "position": [-4.0, float(index * 2)], "orientation": "R0"}
                for index, net in enumerate(external_ports if external_ports is not None else nets)]
    nets = sorted({net for instance in instances.values() for net in instance["connections"].values()} | {pin["net"] for pin in pins})
    if not wires and metadata is not None:
        for index, net in enumerate(nets):
            wires.append({"net": net, "points": [[0.0, float(index * 2)], [float(max(1, len(instances)) * 4), float(index * 2)]]})
    plan = {"schema_version": 1, "operation": "schematic.apply", "mode": "create", "grid": 0.0625,
            "target": {**target, "technology": target.get("technology", "018um_PDK"), "view": "schematic"},
            "instances": sorted(instances.values(), key=lambda item: item["name"]), "nets": nets,
            "pins": pins, "wires": wires, "labels": labels, "inherited_connections": inherited_connections}
    port_order = metadata.get("port_order") if metadata is not None else external_ports
    if port_order is not None:
        plan["port_order"] = port_order
    validated = validate_plan(plan)
    return {"schema_version": 1, "format": "aetherpilot-spice+metadata", "plan": validated,
            "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "metadata": metadata, "exact_geometry": metadata is not None and bool(wires),
            "completeness": {"status": "complete" if metadata is not None else "draft", "issues": [] if metadata is not None else ["flat_netlist_geometry_is_synthesized"]}}


__all__ = ["export_schematic_netlist", "import_schematic_netlist"]
