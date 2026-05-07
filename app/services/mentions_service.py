"""@file mentions in chat input.

When a user types `@app/services/router.py what's wrong here?`, the chat
service expands the @-mention into an inlined snippet of the file's
content before the prompt reaches the model. This bypasses the
workspace-approval gate (the user typed the path explicitly = consent),
matches the OpenCode-style @ flow, and stays read-only — files are
loaded but never written.

The parser is conservative: it only matches `@<path>` where `@` is not
preceded by a word character, so `email@domain.com` is left alone. The
expansion is bounded by per-file and per-message byte limits, so a
mention of a large file doesn't blow up the prompt.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, List, Tuple

from app.services.workspace_service import (
    _is_probably_binary,
    resolve_workspace_item_path,
)


_MENTION_RE = re.compile(r"(?<![\w])@([^\s@]+)")
PER_FILE_LIMIT = 8000      # ~250 lines of code
TOTAL_LIMIT = 24000         # 3 files at the per-file limit


def extract_mentions(text: str) -> List[str]:
    """Return the unique `@<path>` paths in `text`, in first-seen order."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for match in _MENTION_RE.finditer(text):
        token = match.group(1).rstrip(".,;:!?)")  # strip trailing punctuation
        if not token:
            continue
        if token not in seen:
            seen[token] = None
    return list(seen.keys())


def _read_file_truncated(absolute_path: str, max_bytes: int) -> Tuple[str, bool]:
    """Read a file as UTF-8 text, truncating to `max_bytes` of decoded content."""
    try:
        with open(absolute_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_bytes + 1)
    except OSError as exc:
        raise RuntimeError(f"could not read: {exc}")
    truncated = len(text) > max_bytes
    if truncated:
        text = text[:max_bytes]
    return text, truncated


def expand_mentions(
    text: str,
    workspace_path: str | None,
    *,
    per_file_limit: int = PER_FILE_LIMIT,
    total_limit: int = TOTAL_LIMIT,
) -> dict:
    """Expand `@<path>` tokens in `text` into a context block.

    Returns a dict:

      {
        "original": <input>,
        "stripped":  text with @path tokens replaced by `app/foo.py` (no @),
        "context_block": multi-file system message body, or "" if no mentions,
        "expanded": [{path, ok, truncated, reason}],
        "had_workspace": bool,
      }

    The chat service appends `context_block` (when non-empty) as a system
    message before the user turn, so the model sees the inlined files but
    the user's history retains the natural `@path` token.
    """
    mentions = extract_mentions(text)
    expanded: list[dict] = []
    had_workspace = bool(workspace_path)

    if not mentions:
        return {
            "original": text,
            "stripped": text,
            "context_block": "",
            "expanded": expanded,
            "had_workspace": had_workspace,
        }

    blocks: list[str] = []
    bytes_used = 0

    for mention in mentions:
        if not had_workspace:
            expanded.append({"path": mention, "ok": False, "reason": "no_workspace"})
            continue
        resolved = resolve_workspace_item_path(workspace_path, mention)
        if not resolved.get("ok"):
            expanded.append({"path": mention, "ok": False, "reason": resolved.get("error", "unresolvable")})
            continue
        absolute = resolved["absolute_path"]
        if resolved.get("is_dir"):
            expanded.append({"path": mention, "ok": False, "reason": "is_directory"})
            continue
        if _is_probably_binary(absolute):
            expanded.append({"path": mention, "ok": False, "reason": "binary_file"})
            continue
        remaining = max(0, total_limit - bytes_used)
        if remaining <= 0:
            expanded.append({"path": mention, "ok": False, "reason": "total_limit_reached"})
            continue
        budget = min(per_file_limit, remaining)
        try:
            content, truncated = _read_file_truncated(absolute, budget)
        except RuntimeError as exc:
            expanded.append({"path": mention, "ok": False, "reason": str(exc)})
            continue
        bytes_used += len(content)
        rel = resolved.get("relative_path", mention)
        ext = os.path.splitext(rel)[1].lstrip(".") or ""
        block = (
            f"--- @{rel}{' (truncated)' if truncated else ''} ---\n"
            f"```{ext}\n{content}\n```"
        )
        blocks.append(block)
        expanded.append({"path": mention, "ok": True, "truncated": truncated, "resolved": rel})

    # In the user-visible text, drop the leading `@` so the prompt reads naturally.
    stripped = _MENTION_RE.sub(lambda m: m.group(1), text)

    if not blocks:
        return {
            "original": text,
            "stripped": stripped,
            "context_block": "",
            "expanded": expanded,
            "had_workspace": had_workspace,
        }

    context_block = "Inlined files referenced via @-mention (read-only context):\n\n" + "\n\n".join(blocks)
    return {
        "original": text,
        "stripped": stripped,
        "context_block": context_block,
        "expanded": expanded,
        "had_workspace": had_workspace,
    }


def format_expansion_summary(expansion: dict) -> str:
    """Short text summary of what got inlined / what didn't."""
    lines = []
    for entry in expansion.get("expanded", []):
        path = entry.get("path") or "?"
        if entry.get("ok"):
            note = " (truncated)" if entry.get("truncated") else ""
            lines.append(f"  · @{path} loaded{note}")
        else:
            reason = entry.get("reason", "unknown")
            lines.append(f"  · @{path} skipped — {reason}")
    return "\n".join(lines)
