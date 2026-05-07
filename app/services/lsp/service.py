"""High-level LSP service used by the dispatcher and (later) the chat flow.

Single public entry point today: `get_diagnostics(file_path, workspace_root)`.
It picks the right server for the file's language, starts a server if none
is currently running for that workspace+language, sends the file in a
didOpen, and waits briefly for the server's diagnostics push.

Servers are cached per (workspace_root, language) so repeated queries
don't pay the start-up cost again. A best-effort `shutdown_all()` is
provided for clean exits.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.services.lsp.client import LspClient
from app.services.lsp.registry import (
    ServerInfo,
    language_for_path,
    server_for_language,
)


logger = logging.getLogger(__name__)


@dataclass
class DiagnosticsResult:
    ok: bool
    file_path: str
    language: Optional[str] = None
    server: Optional[ServerInfo] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


_clients_lock = threading.Lock()
_clients: Dict[Tuple[str, str], LspClient] = {}


def _client_key(workspace_root: str, language: str) -> Tuple[str, str]:
    return (os.path.realpath(workspace_root), language)


def _get_or_start_client(workspace_root: str, server: ServerInfo) -> LspClient:
    key = _client_key(workspace_root, server.language_id)
    with _clients_lock:
        existing = _clients.get(key)
        if existing is not None:
            return existing
    # Start outside the lock so a slow start doesn't block other languages.
    client = LspClient(list(server.command), workspace_root)
    client.start()
    client.initialize()
    with _clients_lock:
        # In the rare race where another thread also started a client, prefer
        # the one already cached and shut down ours.
        if key in _clients:
            client.shutdown()
            return _clients[key]
        _clients[key] = client
        return client


def get_diagnostics(file_path: str, workspace_root: Optional[str] = None) -> DiagnosticsResult:
    """Return diagnostics for `file_path`, starting the server if necessary."""
    if not file_path:
        return DiagnosticsResult(ok=False, file_path=file_path, error="missing_path")
    abs_path = os.path.realpath(file_path)
    if not os.path.isfile(abs_path):
        return DiagnosticsResult(ok=False, file_path=abs_path, error="path_not_found")
    language = language_for_path(abs_path)
    if not language:
        return DiagnosticsResult(ok=False, file_path=abs_path, error="unknown_language")
    server = server_for_language(language)
    if not server:
        return DiagnosticsResult(
            ok=False,
            file_path=abs_path,
            language=language,
            error="no_server_installed",
        )
    if not workspace_root:
        workspace_root = os.path.dirname(abs_path)
    try:
        client = _get_or_start_client(workspace_root, server)
        diagnostics = client.diagnostics_for(abs_path, language_id=language)
    except Exception as exc:  # noqa: BLE001
        logger.debug("LSP diagnostics for %s failed: %s", abs_path, exc)
        return DiagnosticsResult(
            ok=False,
            file_path=abs_path,
            language=language,
            server=server,
            error=str(exc),
        )
    return DiagnosticsResult(
        ok=True,
        file_path=abs_path,
        language=language,
        server=server,
        diagnostics=diagnostics,
    )


def shutdown_all(timeout: float = 3.0) -> None:
    """Shut down every cached client (called on app exit, or for tests)."""
    with _clients_lock:
        items = list(_clients.items())
        _clients.clear()
    for _key, client in items:
        try:
            client.shutdown(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


# ---------- Formatting helpers (used by the dispatcher) ----------

_SEVERITY_LABEL = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def format_diagnostics(result: DiagnosticsResult) -> str:
    """Render a DiagnosticsResult as plain text for the chat transcript."""
    if not result.ok:
        if result.error == "no_server_installed":
            from app.services.lsp.registry import server_for_language as _slf
            # Use the FIRST candidate (preferred) so we can show its install hint.
            from app.services.lsp.registry import _PREFERENCES  # type: ignore
            preferred = (_PREFERENCES.get(result.language or "") or [None])[0]
            if preferred:
                return (
                    f"No LSP server installed for {result.language}. "
                    f"To enable, install one of:\n"
                    f"  · {preferred.label} ({preferred.license}) — {preferred.install_hint}"
                )
            return f"No LSP server installed for {result.language}."
        if result.error == "unknown_language":
            return f"Cannot run LSP on '{result.file_path}': unsupported file type."
        if result.error == "path_not_found":
            return f"File not found: {result.file_path}."
        return f"LSP diagnostics failed: {result.error}"
    if not result.diagnostics:
        return f"LSP ({result.server.label if result.server else 'unknown'}) found no issues in {result.file_path}."
    lines = [f"LSP ({result.server.label if result.server else 'unknown'}) · {len(result.diagnostics)} diagnostic(s) for {result.file_path}:"]
    for d in result.diagnostics:
        sev = _SEVERITY_LABEL.get(int(d.get("severity") or 1), "msg")
        rng = (d.get("range") or {}).get("start") or {}
        line = (rng.get("line") or 0) + 1
        col = (rng.get("character") or 0) + 1
        message = (d.get("message") or "").replace("\n", " ").strip()
        source = d.get("source") or ""
        code = d.get("code") or ""
        prefix = f"  {sev} {line}:{col}"
        suffix_bits = []
        if source:
            suffix_bits.append(str(source))
        if code:
            suffix_bits.append(str(code))
        suffix = f" [{', '.join(suffix_bits)}]" if suffix_bits else ""
        lines.append(f"{prefix} {message}{suffix}")
    return "\n".join(lines)
