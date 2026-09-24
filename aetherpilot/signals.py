"""Bounded CDBA/Verilog bit names and explicit inherited-net resolution."""

from __future__ import annotations

import copy
import re


ATOM = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.$!/-]*)(?:([<\[])(-?\d+)(?::(-?\d+)(?::(-?\d+))?)?([>\]]))?\Z")
INHERITED_EXPR = re.compile(r"\[(@)([^:\]]{1,4096}):(%):([^\]]{1,65536})\]\Z")


def native_signal_name(value):
    bits = expand_signal(value)
    match = ATOM.fullmatch(value)
    if match and match[5] is None:
        return value.replace("[", "<").replace("]", ">")
    return ",".join(bits)


def _split(expression):
    nesting = []
    start = 0
    result = []
    pairs = {"}": "{", ">": "<", "]": "["}
    for index, char in enumerate(expression):
        if char in "{<[":
            nesting.append(char)
        elif char in "}>]":
            if not nesting or nesting.pop() != pairs[char]:
                raise ValueError("unbalanced signal expression")
        elif char == "," and not nesting:
            result.append(expression[start:index].strip())
            start = index + 1
    if nesting:
        raise ValueError("unbalanced signal expression")
    result.append(expression[start:].strip())
    return result


def expand_signal(expression, *, max_bits=65536, max_depth=32):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 65536 or type(max_bits) is not int or not 1 <= max_bits <= 1_000_000:
        raise ValueError("invalid signal expression or expansion budget")

    def expand(text, depth, remaining):
        if depth > max_depth:
            raise ValueError("signal nesting exceeds its budget")
        text = text.strip()
        terms = _split(text)
        if len(terms) > 1:
            value = []
            for term in terms:
                value.extend(expand(term, depth + 1, remaining - len(value)))
            return value
        repeat = re.fullmatch(r"<\*(\d+)>(.+)", text)
        if repeat is None:
            repeat = re.fullmatch(r"\{(\d+)\{(.+)\}\}", text)
        if repeat is not None:
            count = int(repeat[1])
            if not 1 <= count <= remaining:
                raise ValueError("signal repetition exceeds its budget")
            values = expand(repeat[2], depth + 1, remaining // count)
            if len(values) * count > remaining:
                raise ValueError("signal repetition exceeds its budget")
            return values * count
        if text.startswith("{") and text.endswith("}"):
            return expand(text[1:-1], depth + 1, remaining)
        match = ATOM.fullmatch(text)
        if not match or remaining < 1:
            raise ValueError("unsupported signal expression or expansion budget exceeded")
        name, opener, first, last, stride, closer = match.groups()
        if opener is None:
            return [name]
        if (opener, closer) not in {("<", ">"), ("[", "]")}:
            raise ValueError("mixed signal index delimiters")
        first = int(first)
        last = int(last) if last is not None else first
        step = int(stride) if stride is not None else (1 if last >= first else -1)
        if not step or (last - first) * step < 0 or (last - first) % step:
            raise ValueError("invalid signal slice stride")
        count = abs((last - first) // step) + 1
        if count > remaining or any(abs(index) > 2 ** 31 - 1 for index in (first, last)):
            raise ValueError("signal range exceeds its budget")
        return [f"{name}<{index}>" for index in range(first, last + (1 if step > 0 else -1), step)]

    return expand(expression, 0, max_bits)


def signal_expression_info(expression, *, expected_width=None, require_unique=False, max_bits=65536):
    """Return a deterministic, auditable AST for a bus/net expression.

    ``expand_signal`` remains the single electrical truth.  This companion
    records whether a value was a scalar, slice, concatenation or repetition,
    which lets a caller compose connections without silently flattening away the
    user's intent.  Width and duplicate-bit checks are performed before a
    native operation is allowed to proceed.
    """
    bits = expand_signal(expression, max_bits=max_bits)
    canonical = str(expression).strip().replace("[", "<").replace("]", ">")

    def node(text, depth=0):
        if depth > 32:
            raise ValueError("signal expression nesting exceeds its budget")
        text = text.strip()
        parts = _split(text)
        if len(parts) > 1:
            children = [node(part, depth + 1) for part in parts]
            return {"kind": "concat", "expression": ",".join(child["expression"] for child in children),
                    "width": sum(child["width"] for child in children), "children": children}
        repeat = re.fullmatch(r"<\*(\d+)>(.+)", text) or re.fullmatch(r"\{(\d+)\{(.+)\}\}", text)
        if repeat:
            child = node(repeat[2], depth + 1)
            count = int(repeat[1])
            return {"kind": "repeat", "expression": f"<*{count}>{child['expression']}", "count": count,
                    "width": count * child["width"], "children": [child]}
        if text.startswith("{") and text.endswith("}"):
            child = node(text[1:-1], depth + 1)
            return {"kind": "group", "expression": child["expression"], "width": child["width"], "children": [child]}
        match = ATOM.fullmatch(text)
        if match is None:
            raise ValueError("unsupported signal expression")
        name, opener, first, last, stride, closer = match.groups()
        if opener is None:
            return {"kind": "scalar", "expression": name, "width": 1, "name": name}
        first_i = int(first)
        last_i = int(last) if last is not None else first_i
        step_i = int(stride) if stride is not None else (1 if last_i >= first_i else -1)
        width = abs((last_i - first_i) // step_i) + 1
        return {"kind": "slice", "expression": f"{name}<{first_i}:{last_i}" + (f":{step_i}" if stride is not None else "") + ">",
                "width": width, "name": name, "first": first_i, "last": last_i, "stride": step_i}

    tree = node(str(expression))
    if tree["width"] != len(bits):
        raise ValueError("signal expression width disagrees with expansion")
    if expected_width is not None and (type(expected_width) is not int or expected_width < 1 or tree["width"] != expected_width):
        raise ValueError("signal expression width does not match the expected width")
    if require_unique and len(bits) != len(set(bits)):
        raise ValueError("signal expression contains duplicate bits")
    return {"schema_version": 1, "expression": canonical, "canonical": tree["expression"],
            "kind": tree["kind"], "width": tree["width"], "bits": bits, "tree": tree,
            "unique": len(bits) == len(set(bits))}


def bit_connections(connections, *, instance_name=None, max_bits=65536):
    if not isinstance(connections, dict):
        raise ValueError("connections must be an object")
    instances = expand_signal(instance_name, max_bits=max_bits) if instance_name is not None else [None]
    result = []
    terminals_seen = set()
    for terminal, net in sorted(connections.items()):
        terminals = expand_signal(terminal, max_bits=max_bits)
        if net is None:
            nets = [None] * (len(terminals) * len(instances))
        else:
            nets = expand_signal(net, max_bits=max_bits)
        if len(nets) != len(terminals) * len(instances):
            raise ValueError("terminal and net bit widths differ")
        offset = 0
        for instance in instances:
            for bit in terminals:
                identity = (instance, bit)
                if identity in terminals_seen:
                    raise ValueError("terminal bit is connected more than once")
                terminals_seen.add(identity)
                result.append({"instance": instance, "terminal": bit, "net": nets[offset]})
                offset += 1
        if len(result) > max_bits:
            raise ValueError("bit connection count exceeds its budget")
    return result


def instance_occurrences(instance, *, max_bits=65536):
    names = expand_signal(instance["name"], max_bits=max_bits)
    if names == [instance["name"]]:
        return [instance]
    connections = {name: {} for name in names}
    for bit in bit_connections(instance["connections"], instance_name=instance["name"], max_bits=max_bits):
        connections[bit["instance"]][bit["terminal"]] = bit["net"]
    occurrences = []
    for name in names:
        actual = copy.deepcopy(instance)
        actual.update(name=name, num_bits=1, bits=[name])
        actual["connections"] = {}
        for terminal in instance["connections"]:
            nets = [connections[name][bit] for bit in expand_signal(terminal, max_bits=max_bits)]
            if any(net is None for net in nets):
                raise ValueError("hierarchy occurrence has an unconnected terminal")
            actual["connections"][terminal] = ",".join(nets)
        actual["bit_connections"] = bit_connections(actual["connections"], instance_name=name, max_bits=max_bits)
        occurrences.append(actual)
    return occurrences


def parse_inherited_expression(expression):
    """Parse the bounded native ``[@property:%:default]`` form."""
    if not isinstance(expression, str) or len(expression) > 65536:
        raise ValueError("inherited connection expression is invalid")
    match = INHERITED_EXPR.fullmatch(expression.strip())
    if match is None:
        raise ValueError("unsupported inherited connection expression")
    property_name, default = match.group(2), match.group(4)
    if not property_name.strip() or not default.strip():
        raise ValueError("inherited connection expression is incomplete")
    expand_signal(default.strip())
    return {"property": property_name, "default": default.strip(), "raw": expression.strip(), "source": "native_net_expression"}


def netset_overrides(properties):
    """Extract persisted instance netSet properties without guessing ordinary props."""
    if properties is None:
        return {}
    if isinstance(properties, dict):
        properties = [properties]
    if not isinstance(properties, list):
        return {}
    result = {}
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        value = prop.get("value")
        type_name = " ".join(str(prop.get(key, "")) for key in ("type", "propType", "kind", "objType")).casefold()
        if isinstance(name, str) and name and ("netset" in type_name or prop.get("isNetSet") is True):
            if isinstance(value, (str, dict)):
                result[name] = value
            else:
                raise ValueError("netSet override must be a signal or inherited definition")
    return result


def resolve_inherited(definition, contexts, *, max_depth=32):
    if not isinstance(definition, dict) or not {"property", "default"} <= set(definition) or not isinstance(definition["property"], str):
        raise ValueError("inherited connection requires property and default")
    if not isinstance(contexts, list) or len(contexts) > max_depth or any(not isinstance(context, dict) or not isinstance(context.get("overrides", {}), dict) for context in contexts):
        raise ValueError("invalid inherited connection context")
    seen = set()
    chain = []
    property_name = definition["property"]
    default = definition["default"]
    while True:
        if property_name in seen or len(seen) >= max_depth:
            raise ValueError("cyclic inherited connection")
        seen.add(property_name)
        selected = None
        for context in contexts:
            if property_name in context.get("overrides", {}):
                selected = context
                break
        value = selected["overrides"][property_name] if selected is not None else default
        chain.append({"property": property_name, "source": selected.get("path", []) if selected else "default", "raw": value})
        if isinstance(value, dict) and {"property", "default"} <= set(value):
            property_name, default = value["property"], value["default"]
            continue
        bits = expand_signal(value)
        return {"raw_definition": definition, "effective": value, "bits": bits, "override_chain": chain, "resolved": True}


def bind_hierarchy(parent_connections, child_ports):
    if not isinstance(child_ports, dict):
        raise ValueError("child ports must be an object")
    if set(parent_connections) != set(child_ports):
        raise ValueError("hierarchy port set mismatch")
    bindings = []
    for port, internal in sorted(child_ports.items()):
        external = parent_connections[port]
        inside = expand_signal(internal)
        outside = expand_signal(external)
        port_bits = expand_signal(port)
        if len(inside) != len(outside) or len(inside) != len(port_bits):
            raise ValueError("hierarchy port bit width mismatch")
        bindings.extend({"port": port_bit, "parent_net": parent_net, "child_net": child_net} for port_bit, parent_net, child_net in zip(port_bits, outside, inside))
    return bindings
