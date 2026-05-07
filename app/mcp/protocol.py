"""JSON-RPC 2.0 message framing + dispatch for the MCP server.

MCP uses **line-delimited JSON** over stdio: every message is a
single-line JSON object terminated by `\\n`. We don't use
Content-Length framing — that's LSP, not MCP.

This module is transport-agnostic:

  - ``read_message(reader)`` / ``write_message(writer, msg)`` handle
    the framing.
  - ``dispatch(message, handlers)`` routes a request to the right
    handler and produces a response. Notifications (no ``id``) get
    no response.
  - ``serve_loop(reader, writer, handlers)`` ties it all together
    for tests + the real ``server.py`` main loop.

Everything is plain dict in / dict out, so tests can drive it without
real stdio at all.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple


logger = logging.getLogger(__name__)


# ============================================================
# JSON-RPC 2.0 error codes (from the spec + MCP additions)
# ============================================================

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: MCP-specific implementation error code (per the design doc + MCP
#: convention of using -32000..-32099 for implementation-defined errors).
MCP_TOOL_ERROR = -32000


# ============================================================
# Message construction
# ============================================================

def build_response(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    """Successful JSON-RPC 2.0 response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def build_error(
    request_id: Any,
    code: int,
    message: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Error JSON-RPC 2.0 response. ``data`` is for structured error
    metadata (we put canonical error reason tags here)."""
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ============================================================
# Framing — line-delimited JSON
# ============================================================

def read_message(reader) -> Optional[Dict[str, Any]]:
    """Read one line, parse as JSON. Returns None on EOF or blank line.

    Malformed JSON raises ``json.JSONDecodeError`` — caller should map
    that to a parse-error response if the bytes had any content.
    """
    line = reader.readline()
    if not line:
        return None
    line = line.rstrip("\r\n")
    if not line:
        return None
    return json.loads(line)


def write_message(writer, msg: Dict[str, Any]) -> None:
    """Write one line-terminated JSON message + flush."""
    writer.write(json.dumps(msg) + "\n")
    writer.flush()


# ============================================================
# Dispatch — call the right handler for an incoming request
# ============================================================

#: A handler signature: ``(params: dict) -> dict`` (returned dict goes
#: into the JSON-RPC ``result`` field). Handlers may raise
#: :class:`HandlerError` to produce a structured JSON-RPC error.
Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


class HandlerError(Exception):
    """Raised by a method handler to surface a structured JSON-RPC error.

    ``code`` defaults to MCP_TOOL_ERROR. ``data`` is an optional dict
    carrying the canonical reason tag etc.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int = MCP_TOOL_ERROR,
        data: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _is_notification(message: Dict[str, Any]) -> bool:
    """Per JSON-RPC 2.0: a request with no ``id`` is a notification.

    ``id`` may be 0 / null / a string, all of which count as present.
    """
    return "id" not in message


def dispatch(
    message: Dict[str, Any],
    handlers: Dict[str, Handler],
) -> Optional[Dict[str, Any]]:
    """Route a parsed message to the right handler. Returns the
    response dict, or None for notifications (no response is sent)."""
    method = message.get("method")
    params = message.get("params") or {}
    request_id = message.get("id")

    if not isinstance(method, str) or not method:
        if _is_notification(message):
            return None
        return build_error(request_id, INVALID_REQUEST, "method is required")

    handler = handlers.get(method)
    if handler is None:
        if _is_notification(message):
            # Notifications for unknown methods are dropped silently
            # (per the JSON-RPC 2.0 spec — no response = no error).
            return None
        return build_error(
            request_id,
            METHOD_NOT_FOUND,
            f"unknown method: {method}",
        )

    try:
        result = handler(params)
    except HandlerError as exc:
        if _is_notification(message):
            logger.debug("Notification handler %s raised: %s", method, exc)
            return None
        return build_error(request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001
        # Unhandled exceptions become INTERNAL_ERROR. We don't leak the
        # full traceback — a short class+message is enough for the
        # client and the project log captures the rest.
        logger.exception("Handler %s raised", method)
        if _is_notification(message):
            return None
        return build_error(
            request_id,
            INTERNAL_ERROR,
            f"{type(exc).__name__}: {exc}",
        )

    if _is_notification(message):
        # Per spec, notifications never produce a response, even if the
        # handler returned something.
        return None

    if not isinstance(result, dict):
        # Wrap non-dict returns so the response schema stays valid.
        result = {"value": result}
    return build_response(request_id, result)


# ============================================================
# Serve loop — tie framing + dispatch together
# ============================================================

def serve_loop(
    reader,
    writer,
    handlers: Dict[str, Handler],
    *,
    on_idle: Optional[Callable[[], None]] = None,
) -> None:
    """Drive the JSON-RPC server until the reader closes.

    Each iteration: read one line, parse, dispatch, write the response
    if any. Malformed JSON gets a parse-error response; blank input is
    skipped.

    ``on_idle`` is called once after each message — useful for tests
    that want to assert state between turns.
    """
    while True:
        try:
            message = read_message(reader)
        except json.JSONDecodeError as exc:
            # Malformed input — emit a parse-error response (with id=null
            # since we don't know what the request id was).
            write_message(
                writer,
                build_error(None, PARSE_ERROR, f"invalid JSON: {exc}"),
            )
            continue

        if message is None:
            # EOF — graceful shutdown.
            break

        response = dispatch(message, handlers)
        if response is not None:
            write_message(writer, response)

        if on_idle is not None:
            on_idle()
