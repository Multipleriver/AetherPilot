"""Persisted schematic facts, excluding volatile session and ownership metadata."""

from .core.readback import stable_readback


def schematic_content(readback):
    actual = stable_readback(readback)

    def normalized(value):
        if isinstance(value, list):
            return [normalized(item) for item in value if not isinstance(item, dict) or not str(item.get("name", "")).startswith("aetherpilot_")]
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items() if key not in {"wire_id", "label_id", "text_id", "figure_id", "parameter_provenance", "connection_expressions"} and not key.startswith("aetherpilot_")}
        return value

    inherited = []
    for definition in actual.get("inherited_connections", []):
        if isinstance(definition, dict):
            inherited.append({key: definition.get(key) for key in ("property", "default", "raw")})
    texts = []
    for text in actual.get("texts", []):
        if str(text.get("kind", "")).casefold() in {"attrdisplay", "propdisplay", "instattrdisplay", "evaltext"}:
            continue
        if text.get("text", "").startswith("from netSet "):
            continue
        properties = text.get("properties") if isinstance(text, dict) else None
        if isinstance(properties, list) and any(isinstance(prop, dict) and prop.get("name") == "netExpression" for prop in properties):
            continue
        texts.append(text)
    # Some backends materialize solder-dot ellipses as derived connectivity
    # markers.  Their coordinates change whenever wires are re-routed and
    # they are not user-authored schematic content; excluding them prevents
    # a native re-open from appearing as an unauthorized geometry delta.
    figures = [figure for figure in actual.get("figures", [])
               if not (str(figure.get("kind", "")).casefold() == "ellipse"
                       and figure.get("layer") == "wire" and figure.get("purpose") == "drawing")]
    return normalized({**({"port_order": actual["port_order"]} if "port_order" in actual else {}),
                       "technology": actual.get("technology"), "instances": actual.get("instances"), "pins": actual.get("pins"),
                       "nets": actual.get("nets"), "nets_detail": actual.get("nets_detail"), "wires": actual.get("wires"),
                       "labels": actual.get("labels"), "texts": texts, "figures": figures,
                       "inherited_connections": inherited, "properties": actual.get("properties")})
