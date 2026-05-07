"""Project-level context files for the layered prompt assembly
(Phase 3 of `docs/design/prompt-assembly.md`).

Adapted from Hermes Agent's
([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
``SOUL.md`` (persona) + ``AGENTS.md`` (project context) convention:
two well-known markdown files at the workspace root that carry
durable, project-scoped guidance the model should see on every turn
in this workspace. Reading them is automatic — no approval gate —
because the user pinned the workspace and these files are
well-known names (like ``.gitignore`` or ``README.md``).

This module is intentionally thin: bounded UTF-8 reads, binary
guard, no caching. Re-reading on every chat turn is cheap relative
to the model call, and avoids stale-cache bugs when the user
edits the file between turns.
"""
from __future__ import annotations

import os
from typing import Optional


#: Hard cap on bytes read from a project context file. Slots will
#: truncate further per their ``char_budget``. 64 KB is generous;
#: the persona / project-context files in practice are < 8 KB.
MAX_PROJECT_FILE_BYTES = 64 * 1024

#: First N bytes scanned for null bytes to detect binary files.
#: Same threshold as ``workspace_service._is_probably_binary``.
_BINARY_PROBE_BYTES = 2048

#: Filenames the producers look for, relative to the workspace root.
#: Case-sensitive on Linux/macOS, case-insensitive on Windows — we
#: accept both the canonical name and a couple of common variants.
PERSONA_FILENAMES = ("PERSONA.md", "Persona.md", "persona.md")
PROJECT_AGENTS_FILENAMES = ("AGENTS.md", "Agents.md", "agents.md")


def _read_text_bounded(absolute_path: str) -> Optional[str]:
    """Return the file's decoded UTF-8 content (capped at
    ``MAX_PROJECT_FILE_BYTES``) or None if missing / binary /
    unreadable. Never raises — file-IO errors return None."""
    try:
        if not os.path.isfile(absolute_path):
            return None
        with open(absolute_path, "rb") as fh:
            head = fh.read(_BINARY_PROBE_BYTES)
            if b"\x00" in head:
                return None
            rest = fh.read(MAX_PROJECT_FILE_BYTES - len(head) + 1)
        raw = head + rest
    except OSError:
        return None

    truncated = len(raw) > MAX_PROJECT_FILE_BYTES
    if truncated:
        raw = raw[:MAX_PROJECT_FILE_BYTES]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    if truncated:
        text = text.rstrip() + "\n\n…[file truncated to {} bytes]".format(
            MAX_PROJECT_FILE_BYTES
        )
    return text


def _find_first_existing(workspace_root: str, candidates) -> Optional[str]:
    """Return the absolute path of the first existing file from
    ``candidates`` under ``workspace_root``, or None."""
    if not workspace_root:
        return None
    if not os.path.isdir(workspace_root):
        return None
    for name in candidates:
        candidate = os.path.join(workspace_root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def read_persona(workspace_root: Optional[str]) -> Optional[str]:
    """Return the contents of ``<workspace>/PERSONA.md`` (or a
    case-variant), or None if the file is missing / binary.

    Hermes-inspired: the persona file is durable instructions about
    how the assistant should behave in this workspace — voice, tone,
    constraints, taboo topics. Higher priority than skills / workspace
    info because it shapes everything below."""
    if not workspace_root:
        return None
    path = _find_first_existing(workspace_root, PERSONA_FILENAMES)
    if not path:
        return None
    return _read_text_bounded(path)


def read_project_agents(workspace_root: Optional[str]) -> Optional[str]:
    """Return the contents of ``<workspace>/AGENTS.md`` (or a
    case-variant), or None if the file is missing / binary.

    Hermes-inspired: the project AGENTS file is durable
    project-level context — what this codebase is, how it's
    organised, conventions, command vocabulary. Distinct from the
    repo-root ``AGENTS.md`` (this control layer's own playbook):
    *that* file lives in the source tree of *this* project; the
    workspace's project-agents file lives at the *user's* pinned
    workspace root and tells the model about *their* project."""
    if not workspace_root:
        return None
    path = _find_first_existing(workspace_root, PROJECT_AGENTS_FILENAMES)
    if not path:
        return None
    return _read_text_bounded(path)
