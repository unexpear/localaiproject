"""Tool calling Phase 1 — see ``docs/design/tool-calling.md``.

The model emits ``​```dispatcher​`` fenced blocks whose body is a
single dispatcher command (e.g. ``note set working_memory <text>``).
We extract them, surface the list to the user, and let the user
explicitly run individual calls via ``tool run <n>``. Phase 1
deliberately does NOT auto-execute — the user stays in the loop.

Mirrors ``workspace_service.parse_fenced_writes``: same fenced-
block scaffolding, different info-line marker. The two parsers
operate on the same response text without conflicting (writes
have ``file=...`` in the info line; tool calls have just
``dispatcher``).

Phase 2 (planned): a system-prompt slot that teaches the model
about the format. Phase 3: auto-emit as a typed Patch through
the patch-taxonomy approval gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ============================================================
# Regex
# ============================================================

#: Same fenced-block scaffolding as parse_fenced_writes. We match
#: blocks whose info line is exactly ``dispatcher`` (case-insensitive,
#: with optional surrounding whitespace).
_FENCED_BLOCK_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)


# ============================================================
# Dataclass
# ============================================================

@dataclass
class ToolCall:
    """One parsed tool call. ``index`` is the 1-based ordinal of
    this call in the response (so ``tool run 1`` matches the first
    block); ``raw_block`` carries the full original text including
    fence markers, useful for diff display."""

    command: str
    index: int = 0
    raw_block: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "index": self.index,
            "raw_block": self.raw_block,
        }


# ============================================================
# Parser
# ============================================================

def parse_tool_calls(text: Optional[str]) -> List[ToolCall]:
    """Scan ``text`` for ```dispatcher fenced blocks. Returns a
    list of ``ToolCall`` records in the order they appear.

    Conservative: only blocks whose info line is exactly
    ``dispatcher`` (case-insensitive, surrounding whitespace
    tolerated) qualify. Non-dispatcher blocks are ignored. Empty
    bodies are skipped.

    Multi-line bodies are joined with newlines preserved — most
    dispatcher commands are single-line, but ``write <path>``-
    style commands could span multiple lines. The body is
    trimmed of a single trailing newline (the one ``` always
    introduces).
    """
    if not text or not isinstance(text, str):
        return []

    results: List[ToolCall] = []
    for match in _FENCED_BLOCK_RE.finditer(text):
        info_line = match.group(1).strip().lower()
        if info_line != "dispatcher":
            continue
        body = match.group(2)
        if body.endswith("\n"):
            body = body[:-1]
        body = body.strip()
        if not body:
            continue
        results.append(ToolCall(
            command=body,
            index=len(results) + 1,
            raw_block=match.group(0),
        ))
    return results


# ============================================================
# Renderer
# ============================================================

def format_tool_calls(calls: List[ToolCall]) -> str:
    """Human-readable rendering of a list of ToolCall records,
    used by the ``tool show`` dispatcher subcommand."""
    if not calls:
        return "(no tool calls in this response)"
    lines: List[str] = [f"Tool calls ({len(calls)}):"]
    for call in calls:
        # Truncate the displayed command if it's huge — user can
        # still run by index.
        display = call.command
        if len(display) > 200:
            display = display[:197] + "..."
        # First line preview for multi-line commands
        first_line = display.split("\n", 1)[0]
        lines.append(f"  [{call.index}] {first_line}")
        if "\n" in display:
            extra_lines = display.count("\n")
            lines.append(f"      (+{extra_lines} more line(s))")
    lines.append("")
    lines.append("Run with `tool run <n>` to execute.")
    return "\n".join(lines)


# ============================================================
# Convenience: pull from session history
# ============================================================

def parse_from_last_assistant(session_id: str = "default") -> List[ToolCall]:
    """Walk back through the session's history, find the most
    recent assistant turn, and parse tool calls from its content.
    Returns an empty list if no assistant turns exist or the
    most recent has no parseable calls."""
    from app.memory import local_memory
    history = local_memory.get_history(session_id=session_id, limit=20) or []
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "assistant":
            continue
        content = entry.get("content") or ""
        return parse_tool_calls(content)
    return []


# ============================================================
# Phase 2: tool_calls_enabled flag + system-prompt teaching
# ============================================================

def is_enabled(session_id: str = "default") -> bool:
    """True iff the user has opted into tool-call teaching for
    this session. See ``tool enable on/off``."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id) or {}
    return bool(context.get("tool_calls_enabled"))


def set_enabled(session_id: str = "default", enabled: bool = False) -> bool:
    """Persist the ``tool_calls_enabled`` flag for ``session_id``.
    Returns the new value."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id)
    context["tool_calls_enabled"] = bool(enabled)
    local_memory.update_session_context(session_id=session_id, context=context)
    return bool(enabled)


#: The teaching message injected into the prompt assembly when
#: ``tool_calls_enabled`` is True. Deliberately concise — the
#: model needs to know the format, not a tutorial.
TOOL_CALLS_TEACHING_MESSAGE = """\
Tool calling is enabled for this session.

When you want to suggest a dispatcher command for the user to
review and run, emit a fenced code block with ``dispatcher`` as
the info line:

```dispatcher
note set working_memory User pattern: edits auth.py 5x in 20min
```

Each block contains exactly one dispatcher command. The user
sees a numbered list and runs them explicitly via
``tool run <n>`` — you do NOT execute commands directly.

Use this sparingly:
- Only suggest a command when it provides specific, non-trivial
  value (don't suggest commands for the sake of suggesting).
- Read-only commands (``recall``, ``status``, ``models``,
  ``retrieve <query>``) are low-friction and good candidates.
- State-mutating commands (``mode build``, ``write <path>``,
  ``note set ...``) should only appear when you have a clear
  reason — the user reviews each before it runs, but rapid-fire
  suggestions are still annoying.

If you have nothing to suggest, simply answer the user's
question without emitting any ``dispatcher`` blocks.
"""

