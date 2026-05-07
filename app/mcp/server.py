"""MCP server main loop.

Entry points:

  - ``main()`` runs the stdio loop using ``sys.stdin``/``sys.stdout``
    (the typical Claude-Code-spawned-subprocess pattern).
  - ``serve(reader, writer)`` is the testable variant — point it at any
    pair of file-like objects and it'll handshake + dispatch.

We support these MCP methods in Phase 1:

    initialize                      — handshake; returns server info + capabilities
    notifications/initialized       — client ack; no response
    ping                            — empty response (keep-alive)
    tools/list                      — return the tool registry
    tools/call                      — invoke a tool by name + args
    resources/list                  — return the resource registry
    resources/read                  — return a resource body
    shutdown                        — clean exit (some clients use this)

Anything else returns ``METHOD_NOT_FOUND``.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Dict

from app.mcp import protocol
from app.mcp import tools as mcp_tools


logger = logging.getLogger(__name__)


SERVER_INFO = {
    "name": "localaiproject",
    "version": "0.1.0",
}

# MCP protocol version we support. We echo back whatever the client
# sent in `initialize` if it's plausibly compatible; otherwise return
# our preferred version.
PREFERRED_PROTOCOL_VERSION = "2024-11-05"


# ============================================================
# Method handlers
# ============================================================

def _handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    """Server-side of the ``initialize`` handshake.

    The client sends its protocol version, capabilities, and client
    info; we respond with ours. We don't enforce a strict protocol-
    version match — older + newer clients should still work for the
    minimal Phase 1 surface.
    """
    client_version = (params or {}).get("protocolVersion") or PREFERRED_PROTOCOL_VERSION
    return {
        "protocolVersion": client_version,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "logging": {},
        },
        "serverInfo": SERVER_INFO,
        "instructions": (
            "localaiproject MCP server. Inspect with `status` / `doctor`, "
            "send chat with `chat`, queue work with `dispatch` / `do`, "
            "approve workspace actions with `approve`. The default "
            "session_id is 'default'."
        ),
    }


def _handle_initialized(_params: Dict[str, Any]) -> Dict[str, Any]:
    """Client tells us it's done initializing. No-op for us."""
    return {}


def _handle_ping(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def _handle_shutdown(_params: Dict[str, Any]) -> Dict[str, Any]:
    """Some clients send shutdown before closing the pipe. Reply OK
    and let the EOF on stdin tear down the loop naturally."""
    return {}


def _handle_tools_list(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {"tools": mcp_tools.list_tool_descriptors()}


def _handle_tools_call(params: Dict[str, Any]) -> Dict[str, Any]:
    name = (params or {}).get("name")
    args = (params or {}).get("arguments") or {}
    if not isinstance(name, str) or not name:
        raise protocol.HandlerError(
            "tools/call requires a 'name' parameter",
            code=protocol.INVALID_PARAMS,
        )
    if not isinstance(args, dict):
        raise protocol.HandlerError(
            "tools/call 'arguments' must be a JSON object",
            code=protocol.INVALID_PARAMS,
        )

    tool = mcp_tools.get_tool(name)
    if tool is None:
        raise protocol.HandlerError(
            f"unknown tool: {name}",
            code=protocol.METHOD_NOT_FOUND,
            data={"available": [t.name for t in mcp_tools.TOOLS]},
        )

    try:
        event = tool.handler(args)
    except Exception as exc:  # noqa: BLE001
        # Tool-level exceptions become structured tool-call failures —
        # NOT JSON-RPC errors. The client gets isError=True with a
        # canonical reason if we can extract one.
        logger.exception("Tool %s raised", name)
        return {
            "content": [
                {"type": "text", "text": f"{type(exc).__name__}: {exc}"}
            ],
            "isError": True,
            "structuredContent": {
                "action": name,
                "output": str(exc),
                "reason": "internal_error",
            },
        }

    return mcp_tools.event_to_call_result(event)


def _handle_resources_list(_params: Dict[str, Any]) -> Dict[str, Any]:
    return {"resources": mcp_tools.list_resource_descriptors()}


def _handle_resources_read(params: Dict[str, Any]) -> Dict[str, Any]:
    uri = (params or {}).get("uri")
    if not isinstance(uri, str) or not uri:
        raise protocol.HandlerError(
            "resources/read requires a 'uri' parameter",
            code=protocol.INVALID_PARAMS,
        )
    try:
        return mcp_tools.read_resource(uri)
    except KeyError as exc:
        raise protocol.HandlerError(
            f"unknown resource: {uri}",
            code=protocol.METHOD_NOT_FOUND,
            data={"uri": uri},
        ) from exc


# ============================================================
# Handler registry
# ============================================================

def build_handlers() -> Dict[str, protocol.Handler]:
    """Return the method→handler dict the protocol loop dispatches on."""
    return {
        "initialize": _handle_initialize,
        "notifications/initialized": _handle_initialized,
        "ping": _handle_ping,
        "shutdown": _handle_shutdown,
        "tools/list": _handle_tools_list,
        "tools/call": _handle_tools_call,
        "resources/list": _handle_resources_list,
        "resources/read": _handle_resources_read,
    }


# ============================================================
# Public entry points
# ============================================================

def serve(reader, writer) -> None:
    """Run the MCP loop against the given file-like objects.

    Tests pass an ``io.StringIO`` pair; ``main`` passes
    ``sys.stdin``/``sys.stdout``. Returns when the reader hits EOF.
    """
    handlers = build_handlers()
    protocol.serve_loop(reader, writer, handlers)


def main(argv=None) -> int:
    """Stdio entry point. Spawnable as ``python -m app.mcp``."""
    # Keep stdout clean: route library logging to stderr so we don't
    # corrupt the JSON-RPC stream with log lines.
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="[mcp %(levelname)s] %(name)s: %(message)s",
    )
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
