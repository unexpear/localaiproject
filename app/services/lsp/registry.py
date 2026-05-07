"""Free, no-strings language server registry.

Detects what's already on the user's PATH. Never auto-installs, never
calls home, never bundles a server. If a language server isn't found,
the operation politely degrades and tells the user the install command
they could run themselves.

Server preferences (highest priority first):

  python   pylsp                          (BSD-3, pure Python community fork)
           pyright-langserver --stdio     (MIT, Microsoft's TS implementation)
  go       gopls                          (BSD-3)
  rust     rust-analyzer                   (MIT/Apache)
  typescript / javascript
           typescript-language-server --stdio  (MIT)
  c / cpp  clangd                         (Apache)
  bash     bash-language-server start     (MIT)
  yaml     yaml-language-server --stdio   (MIT)
  json     vscode-json-language-server --stdio  (MIT)
  lua      lua-language-server            (MIT)

`available_servers()` returns only those that are actually installed on
the running machine; missing entries appear in `missing_servers()` along
with the recommended install command so the dispatcher can render a
friendly hint.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ServerInfo:
    language_id: str        # LSP languageId (e.g. "python")
    label: str              # human label (e.g. "pylsp")
    command: Tuple[str, ...]  # full command, with args, e.g. ("pylsp",)
    install_hint: str       # friendly install instruction
    license: str            # free-form license tag for display

    def is_available(self) -> bool:
        return shutil.which(self.command[0]) is not None


# Ordered preferences per language. The first available wins.
_PREFERENCES: Dict[str, List[ServerInfo]] = {
    "python": [
        ServerInfo(
            language_id="python",
            label="pylsp",
            command=("pylsp",),
            install_hint="pip install python-lsp-server",
            license="BSD-3 (community)",
        ),
        ServerInfo(
            language_id="python",
            label="pyright-langserver",
            command=("pyright-langserver", "--stdio"),
            install_hint="npm install -g pyright   (or: pip install pyright)",
            license="MIT (Microsoft)",
        ),
    ],
    "go": [
        ServerInfo(
            language_id="go",
            label="gopls",
            command=("gopls",),
            install_hint="go install golang.org/x/tools/gopls@latest",
            license="BSD-3",
        ),
    ],
    "rust": [
        ServerInfo(
            language_id="rust",
            label="rust-analyzer",
            command=("rust-analyzer",),
            install_hint="rustup component add rust-analyzer",
            license="MIT/Apache",
        ),
    ],
    "typescript": [
        ServerInfo(
            language_id="typescript",
            label="typescript-language-server",
            command=("typescript-language-server", "--stdio"),
            install_hint="npm install -g typescript-language-server typescript",
            license="MIT",
        ),
    ],
    "javascript": [
        ServerInfo(
            language_id="javascript",
            label="typescript-language-server",
            command=("typescript-language-server", "--stdio"),
            install_hint="npm install -g typescript-language-server typescript",
            license="MIT",
        ),
    ],
    "c": [
        ServerInfo(
            language_id="c",
            label="clangd",
            command=("clangd",),
            install_hint="apt install clangd  /  brew install llvm  /  winget install LLVM.LLVM",
            license="Apache 2.0",
        ),
    ],
    "cpp": [
        ServerInfo(
            language_id="cpp",
            label="clangd",
            command=("clangd",),
            install_hint="apt install clangd  /  brew install llvm  /  winget install LLVM.LLVM",
            license="Apache 2.0",
        ),
    ],
    "bash": [
        ServerInfo(
            language_id="bash",
            label="bash-language-server",
            command=("bash-language-server", "start"),
            install_hint="npm install -g bash-language-server",
            license="MIT",
        ),
    ],
    "yaml": [
        ServerInfo(
            language_id="yaml",
            label="yaml-language-server",
            command=("yaml-language-server", "--stdio"),
            install_hint="npm install -g yaml-language-server",
            license="MIT",
        ),
    ],
    "json": [
        ServerInfo(
            language_id="json",
            label="vscode-json-language-server",
            command=("vscode-json-language-server", "--stdio"),
            install_hint="npm install -g vscode-langservers-extracted",
            license="MIT",
        ),
    ],
    "lua": [
        ServerInfo(
            language_id="lua",
            label="lua-language-server",
            command=("lua-language-server",),
            install_hint="brew install lua-language-server  /  apt install lua-language-server",
            license="MIT",
        ),
    ],
}


# Map file extensions to LSP language IDs.
_EXT_TO_LANGUAGE: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".go": "go",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".sh": "bash", ".bash": "bash",
    ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
    ".lua": "lua",
}


def language_for_path(path: str) -> Optional[str]:
    if not path:
        return None
    lower = path.lower()
    for ext, lang in _EXT_TO_LANGUAGE.items():
        if lower.endswith(ext):
            return lang
    return None


def server_for_language(language_id: str) -> Optional[ServerInfo]:
    """Return the highest-priority installed server for `language_id`, or None."""
    candidates = _PREFERENCES.get(language_id) or []
    for server in candidates:
        if server.is_available():
            return server
    return None


def all_known_servers() -> List[ServerInfo]:
    """Return every server we know about, regardless of install status."""
    flat: List[ServerInfo] = []
    seen: set[Tuple[str, str]] = set()
    for language, servers in _PREFERENCES.items():
        for s in servers:
            key = (language, s.label)
            if key in seen:
                continue
            seen.add(key)
            flat.append(s)
    return flat


def available_servers() -> List[ServerInfo]:
    """Return servers that are currently installed on PATH."""
    return [s for s in all_known_servers() if s.is_available()]


def missing_servers() -> List[ServerInfo]:
    return [s for s in all_known_servers() if not s.is_available()]
