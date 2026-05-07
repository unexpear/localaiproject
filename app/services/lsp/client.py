"""Minimal LSP client over stdio JSON-RPC.

No third-party dependencies. Speaks the parts of the LSP we need:

  - initialize / initialized
  - textDocument/didOpen
  - textDocument/publishDiagnostics  (server-pushed notification)
  - textDocument/didClose
  - shutdown / exit

Synchronous request/response model with a background reader thread that
demuxes incoming messages into a request-response map and a notifications
queue. This keeps the call sites simple while still handling the
asynchronous nature of the protocol.

Why we wrote this rather than depend on an LSP library:

  * No commercial-strings concerns — every line is ours.
  * One file, ~200 lines, easy to audit.
  * Diagnostics-only is enough for the first integration round; we'll
    extend with hover / definition in a follow-up.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote


logger = logging.getLogger(__name__)


def _path_to_uri(path: str) -> str:
    """Convert an absolute filesystem path to a `file://` URI (LSP requirement)."""
    p = Path(path).resolve()
    parts = quote(p.as_posix(), safe="/:")
    if parts.startswith("/"):
        return f"file://{parts}"
    # Windows: drive-letter path. URI is file:///C:/... (three slashes).
    return f"file:///{parts}"


def _frame_message(payload: Dict[str, Any]) -> bytes:
    """Frame a JSON-RPC message in LSP's Content-Length envelope."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _read_message(stream) -> Optional[Dict[str, Any]]:
    """Read one framed LSP message from stdout. Returns None on EOF."""
    headers: Dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.decode("ascii", errors="replace").rstrip("\r\n")
        if line == "":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    length_str = headers.get("content-length")
    if not length_str:
        return None
    try:
        length = int(length_str)
    except ValueError:
        return None
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


class LspClient:
    """One client per language-server subprocess.

    Lifecycle: start() -> initialize() -> ... requests/notifications ... -> shutdown().
    Reuse a single client for many file queries against the same workspace —
    starting a server is the slowest step (~1-3 seconds for pyright).
    """

    def __init__(self, command: List[str], workspace_root: str) -> None:
        if not command:
            raise ValueError("command must be non-empty")
        self._command = list(command)
        self._workspace_root = str(Path(workspace_root).resolve())
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._next_request_id = 1
        self._lock = threading.Lock()
        self._pending: Dict[int, threading.Event] = {}
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._notifications: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stopped = threading.Event()
        self._initialized = False

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._workspace_root,
            bufsize=0,
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                msg = _read_message(self._process.stdout)  # type: ignore[union-attr]
                if msg is None:
                    return
                if "id" in msg and ("result" in msg or "error" in msg):
                    rid = msg["id"]
                    with self._lock:
                        self._responses[rid] = msg
                        evt = self._pending.pop(rid, None)
                    if evt is not None:
                        evt.set()
                else:
                    # Notification or server->client request — we only consume notifications.
                    self._notifications.put(msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("LSP reader loop ended: %s", exc)

    def _next_id(self) -> int:
        with self._lock:
            rid = self._next_request_id
            self._next_request_id += 1
            return rid

    def _send(self, payload: Dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("server not started")
        self._process.stdin.write(_frame_message(payload))
        self._process.stdin.flush()

    def request(self, method: str, params: Any = None, timeout: float = 10.0) -> Dict[str, Any]:
        if self._process is None:
            raise RuntimeError("server not started")
        rid = self._next_id()
        evt = threading.Event()
        with self._lock:
            self._pending[rid] = evt
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self._send(payload)
        if not evt.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"LSP request '{method}' timed out after {timeout}s")
        with self._lock:
            return self._responses.pop(rid)

    def notify(self, method: str, params: Any = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._send(payload)

    # ------------------------------------------------------------------
    # Standard handshake
    # ------------------------------------------------------------------
    def initialize(self) -> Dict[str, Any]:
        if self._initialized:
            return {}
        root_uri = _path_to_uri(self._workspace_root)
        params = {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": False},
                    "synchronization": {"didOpen": True, "didClose": True},
                },
                "workspace": {"workspaceFolders": True},
            },
            "workspaceFolders": [{"uri": root_uri, "name": Path(self._workspace_root).name}],
        }
        response = self.request("initialize", params, timeout=15.0)
        self.notify("initialized", {})
        self._initialized = True
        return response.get("result") or {}

    def shutdown(self, timeout: float = 3.0) -> None:
        if self._process is None:
            return
        try:
            try:
                self.request("shutdown", None, timeout=timeout)
            except Exception:
                pass
            try:
                self.notify("exit")
            except Exception:
                pass
        finally:
            self._stopped.set()
            proc = self._process
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            # Explicitly close pipes so we don't leak file handles in tests / long-running apps.
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            self._process = None

    # ------------------------------------------------------------------
    # Diagnostics flow
    # ------------------------------------------------------------------
    def diagnostics_for(self, file_path: str, *, language_id: str, wait_seconds: float = 6.0) -> List[Dict[str, Any]]:
        """Open `file_path`, wait for `publishDiagnostics`, return the diagnostic list.

        `language_id` is the LSP `languageId` field (e.g. "python", "typescript").
        Returns an empty list if no diagnostics are pushed within `wait_seconds`.
        """
        if not self._initialized:
            self.initialize()
        target_uri = _path_to_uri(file_path)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise RuntimeError(f"could not read file: {exc}")

        # Drain pre-existing notifications so we don't keep a stale diagnostics
        # set for the same URI.
        self._drain_diagnostics_for(target_uri)

        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": target_uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            },
        )

        diagnostics = self._wait_for_diagnostics(target_uri, deadline=time.monotonic() + wait_seconds)

        # Politely close the document so the server frees it.
        try:
            self.notify("textDocument/didClose", {"textDocument": {"uri": target_uri}})
        except Exception:
            pass
        return diagnostics

    def _drain_diagnostics_for(self, uri: str) -> None:
        # Non-blocking drain
        leftover: list = []
        while True:
            try:
                msg = self._notifications.get_nowait()
            except queue.Empty:
                break
            if msg.get("method") == "textDocument/publishDiagnostics":
                continue  # discard
            leftover.append(msg)
        for msg in leftover:
            self._notifications.put(msg)

    def _wait_for_diagnostics(self, uri: str, deadline: float) -> List[Dict[str, Any]]:
        last: List[Dict[str, Any]] = []
        seen_first = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            try:
                msg = self._notifications.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if seen_first:
                    return last  # one set of diagnostics is usually enough
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                params = msg.get("params") or {}
                if params.get("uri") == uri:
                    last = list(params.get("diagnostics") or [])
                    seen_first = True
        return last

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    def __enter__(self) -> "LspClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
