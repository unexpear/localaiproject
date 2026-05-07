"""LSP integration — detects free, no-strings language servers already on PATH
and exposes a small synchronous API for queries (currently: diagnostics).

Public surface:
    available_servers() -> list[ServerInfo]
    get_diagnostics(path, workspace_root=None) -> DiagnosticsResult
"""
from app.services.lsp.registry import ServerInfo, available_servers, server_for_language
from app.services.lsp.service import DiagnosticsResult, get_diagnostics

__all__ = [
    "ServerInfo",
    "available_servers",
    "server_for_language",
    "DiagnosticsResult",
    "get_diagnostics",
]
