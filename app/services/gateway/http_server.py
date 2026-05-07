"""Local HTTP gateway · stdlib-only.

Exposes a tiny REST surface around the dispatcher so external processes /
agents can drive one localaiproject session and stream events back.

  POST /dispatch                      JSON: {session_id, text}  ->  dispatcher event
  GET  /sessions/<id>/state           JSON snapshot: context + recent history + tasks
  GET  /events?session=<id>           Server-Sent Events stream of GatewayEvents
  GET  /health                        Liveness probe

Hard rules:
  - Always binds to 127.0.0.1 (no network exposure ever).
  - Optional bearer token via the LOCALAI_GATEWAY_TOKEN env var. If set,
    every request must send `Authorization: Bearer <token>`. If empty,
    the gateway accepts loopback callers unauthenticated.
  - Zero third-party dependencies; uses stdlib http.server + threading.
  - Calls `dispatch_input` directly so business logic is unforked.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.memory import local_memory
from app.services.gateway.bus import GatewayEvent, default_bus


logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7780
TOKEN_ENV = "LOCALAI_GATEWAY_TOKEN"


def _expected_token() -> str:
    return (os.environ.get(TOKEN_ENV) or "").strip()


def _client_authorised(headers) -> bool:
    expected = _expected_token()
    if not expected:
        return True
    auth = headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer "):].strip() == expected


class GatewayHandler(BaseHTTPRequestHandler):
    """Per-request handler. One per connection in stdlib's ThreadingHTTPServer."""

    server_version = "LocalAIGateway/0.1"
    # Suppress the default access log so we don't scrawl across the user's
    # transcript every time a Qt subscriber pings.
    def log_message(self, fmt, *args):  # noqa: N802 (stdlib API)
        logger.debug("%s - %s", self.client_address[0], fmt % args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        if _client_authorised(self.headers):
            return True
        self._send_json(401, {"error": "unauthorised"})
        return False

    def _read_body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------
    # GET routes
    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802 (stdlib API)
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._send_json(200, {"ok": True, "name": "localaiproject-gateway"})
            return

        if not self._check_auth():
            return

        if path.startswith("/sessions/") and path.endswith("/state"):
            sid = path[len("/sessions/"):-len("/state")] or "default"
            self._send_json(200, _state_snapshot(sid))
            return

        if path == "/events":
            qs = parse_qs(parsed.query or "")
            sid = (qs.get("session") or [default_bus().ANY])[0]
            self._stream_events(sid)
            return

        self._send_json(404, {"error": "not found", "path": path})

    # ------------------------------------------------------------------
    # POST routes
    # ------------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._check_auth():
            return

        if path == "/dispatch":
            body = self._read_body_json()
            if body is None:
                self._send_json(400, {"error": "invalid json body"})
                return
            text = body.get("text") or body.get("input") or ""
            session_id = body.get("session_id") or body.get("session") or "default"
            if not isinstance(text, str) or not text.strip():
                self._send_json(400, {"error": "missing 'text'"})
                return
            from app.services.command_dispatcher import dispatch_input
            event = dispatch_input(text, session_id=session_id)
            self._send_json(200, event)
            return

        self._send_json(404, {"error": "not found", "path": path})

    # ------------------------------------------------------------------
    # SSE event stream
    # ------------------------------------------------------------------
    def _stream_events(self, session_id: str) -> None:
        """Open a Server-Sent Events stream for one client.

        Subscribes to the bus for `session_id` (or ANY if `session_id == '*'`),
        forwards each incoming GatewayEvent as an SSE frame, and times-out
        cleanly when the client disconnects.
        """
        event_q: "queue.Queue[GatewayEvent]" = queue.Queue(maxsize=200)

        def on_event(event: GatewayEvent) -> None:
            try:
                event_q.put_nowait(event)
            except queue.Full:
                # Drop events for slow clients rather than blocking the bus.
                pass

        sub_id = default_bus().subscribe(session_id, on_event)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Initial hello so clients know they're connected.
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while True:
                try:
                    event = event_q.get(timeout=15.0)
                except queue.Empty:
                    # Heartbeat keeps proxies / clients honest.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(
                    {
                        "kind": event.kind,
                        "session_id": event.session_id,
                        "payload": event.payload,
                        "timestamp": event.timestamp,
                    },
                    ensure_ascii=False,
                )
                self.wfile.write(b"data: ")
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.write(b"\n\n")
                self.wfile.flush()
        except (ConnectionError, BrokenPipeError, ValueError):
            # Client disconnected.
            pass
        finally:
            default_bus().unsubscribe(sub_id)


def _state_snapshot(session_id: str) -> dict:
    """Read-only snapshot a remote agent can use to plan its next move."""
    history = local_memory.get_history(session_id=session_id, limit=20)
    context = local_memory.get_session_context(session_id=session_id)
    inbox = local_memory.get_inbox(session_id, status="queued")
    in_progress = local_memory.in_progress_task_for(session_id)
    return {
        "session_id": session_id,
        "context": context,
        "recent_history": history,
        "inbox": inbox,
        "in_progress_task": in_progress,
        "linked_partners": local_memory.get_link_partners(session_id),
    }


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class GatewayServer:
    """Wraps ThreadingHTTPServer so the dispatcher can start/stop it cleanly."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> dict:
        if self.is_running():
            return {"ok": False, "error": "already_running", "host": self.host, "port": self.port}
        # Bind first so we can report port-in-use immediately rather than
        # crashing the background thread.
        try:
            server = ThreadingHTTPServer((self.host, self.port), GatewayHandler)
        except OSError as exc:
            return {"ok": False, "error": str(exc), "host": self.host, "port": self.port}
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name=f"gateway-{self.host}:{self.port}",
            daemon=True,
        )
        self._thread.start()
        return {
            "ok": True,
            "host": self.host,
            "port": self.port,
            "auth_required": bool(_expected_token()),
        }

    def stop(self) -> dict:
        if not self.is_running():
            return {"ok": False, "error": "not_running"}
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        self._server = None
        self._thread = None
        return {"ok": True}


# ---------------------------------------------------------------------------
# Process-singleton accessor
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton: Optional[GatewayServer] = None


def get_singleton() -> GatewayServer:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = GatewayServer()
    return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        if _singleton is not None and _singleton.is_running():
            _singleton.stop()
        _singleton = None
