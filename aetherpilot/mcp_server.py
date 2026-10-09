"""Minimal MCP stdio server exposing read-only AetherPilot tools.

Implements a bounded subset of the Model Context Protocol over
newline-delimited JSON-RPC 2.0: ``initialize``, ``ping``, ``tools/list`` and
``tools/call``.  Only read-only operations are exposed; database writes stay
behind the reviewed CLI/transaction workflow and are deliberately not MCP
tools.
"""

from __future__ import annotations

import json

from . import __version__
from .errors import HarnessError

PROTOCOL_VERSION = "2024-11-05"
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TOOL_TEXT = 200_000

_OBJECT = {"type": "object", "additionalProperties": False}


def _tool_definitions():
    return [
        {"name": "aetherpilot_capabilities",
         "description": "Static AetherPilot capability declaration, including unimplemented gates and deferred scope.",
         "inputSchema": {**_OBJECT, "properties": {}}},
        {"name": "aetherpilot_backend_status",
         "description": "Side-effect-free probe of the optional native Aether backend (never starts Aether or a license).",
         "inputSchema": {**_OBJECT, "properties": {}}},
        {"name": "aetherpilot_read_schematic",
         "description": "Guarded read-only schematic readback from the configured Aether workspace.",
         "inputSchema": {**_OBJECT, "required": ["library", "cell"],
                         "properties": {"library": {"type": "string"}, "cell": {"type": "string"},
                                        "view": {"type": "string", "default": "schematic"},
                                        "max_depth": {"type": "integer", "default": 10}}}},
        {"name": "aetherpilot_review_files",
         "description": "Run the bounded read-only Design Reviewer on immutable readback/OP/intent/netlist files.",
         "inputSchema": {**_OBJECT, "required": ["readback_path"],
                         "properties": {"readback_path": {"type": "string"}, "op_path": {"type": "string"},
                                        "intent_path": {"type": "string"}, "netlist_path": {"type": "string"}}}},
        {"name": "aetherpilot_render_report",
         "description": "Render a self-contained HTML evidence report for a completed run operation; never overwrites output.",
         "inputSchema": {**_OBJECT, "required": ["run_operation", "output"],
                         "properties": {"run_operation": {"type": "string"}, "output": {"type": "string"}}}},
        {"name": "aetherpilot_apply_plan",
         "description": "Apply a declarative schematic plan JSON through the native transaction pipeline. Write operation: requires approved=true (explicit user consent) and only ever creates/updates through the governed flow.",
         "inputSchema": {**_OBJECT, "required": ["plan_path", "approved"],
                         "properties": {"plan_path": {"type": "string"}, "approved": {"type": "boolean"}}}},
        {"name": "aetherpilot_natural_apply",
         "description": "Compile a bounded natural-language description and create a NEW native schematic cell. Write operation: requires approved=true; existing cells are never overwritten.",
         "inputSchema": {**_OBJECT, "required": ["description", "library", "cell", "approved"],
                         "properties": {"description": {"type": "string"}, "library": {"type": "string"},
                                        "cell": {"type": "string"}, "approved": {"type": "boolean"}}}},
    ]


def _require_approved(arguments):
    if arguments.get("approved") is not True:
        raise ValueError("write tools require approved=true (explicit user consent recorded in the request)")


def _call_tool(config, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    from .backend import backend_status
    from .capabilities import capabilities
    from .harness import Harness

    if name == "aetherpilot_capabilities":
        if arguments:
            raise ValueError("aetherpilot_capabilities takes no arguments")
        return capabilities()
    if name == "aetherpilot_backend_status":
        if arguments:
            raise ValueError("aetherpilot_backend_status takes no arguments")
        return backend_status(config)
    harness = Harness(config)
    if name == "aetherpilot_read_schematic":
        return harness.read_schematic({"library": arguments["library"], "cell": arguments["cell"],
                                       "view": arguments.get("view", "schematic")}, max_depth=arguments.get("max_depth", 10))
    if name == "aetherpilot_review_files":
        return harness.review_files(arguments["readback_path"], op_path=arguments.get("op_path"),
                                    intent_path=arguments.get("intent_path"), netlist_path=arguments.get("netlist_path"))
    if name == "aetherpilot_render_report":
        return harness.render_report(arguments["run_operation"], arguments["output"])
    if name == "aetherpilot_apply_plan":
        _require_approved(arguments)
        return harness.run(arguments["plan_path"])
    if name == "aetherpilot_natural_apply":
        _require_approved(arguments)
        return harness.natural_language_apply(arguments["description"],
                                              target={"library": arguments["library"], "cell": arguments["cell"]})
    raise KeyError(name)


def _tool_payload(config, name, arguments):
    try:
        result = _call_tool(config, name, arguments)
    except KeyError:
        raise
    except (HarnessError, ValueError, TypeError, OSError) as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if len(text) > MAX_TOOL_TEXT:
        text = text[:MAX_TOOL_TEXT] + "\n... (truncated)"
    is_error = isinstance(result, dict) and result.get("ok") is False
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(payload, config=None):
    """Handle one JSON-RPC payload; return the response dict or None for notifications."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return {"jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
                "error": {"code": -32600, "message": "invalid JSON-RPC request"}}
    method, request_id = payload["method"], payload.get("id")
    if method.startswith("notifications/"):
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "aetherpilot", "version": __version__}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_definitions()}
        elif method == "tools/call":
            params = payload.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                raise TypeError("tools/call requires params.name")
            try:
                result = _tool_payload(config, params["name"], params.get("arguments", {}))
            except KeyError:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unknown tool: " + params["name"]}}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found: " + method}}
    except (TypeError, ValueError) as exc:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(stdin, stdout, config=None):
    """Serve newline-delimited JSON-RPC until EOF; returns the process exit code."""
    for line in stdin:
        if len(line.encode("utf-8", "replace")) > MAX_LINE_BYTES:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "request line too large"}}
        else:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                response = handle(payload, config=config)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
            stdout.flush()
    return 0


__all__ = ["handle", "serve", "PROTOCOL_VERSION"]
