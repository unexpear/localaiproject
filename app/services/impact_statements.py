"""Shell-generated impact statements for the patch taxonomy
(Phase 2 — see ``docs/design/patch-taxonomy.md``).

The keystone safety idea: when a patch is dangerous enough to
require L3 typed confirmation, the *shell* (this module) produces
the impact statement — NOT the model. The model has every incentive
to soft-pedal consequences ("This is a small change to how I
respond"); the shell knows what each patch kind actually does and
emits a static, honest description from a fixed template.

This module is intentionally pure: no side effects, no I/O, no
model calls. Functions take a ``pending_approval``-shaped dict and
return strings. The dispatcher's approval banner consumes them.

Shipped today:
* ``impact_statement_for(action)`` — short text ("This will write 1
  file.") for L1/L2 banners and the leading line of L3 banners.
* ``confirmation_phrase_for(action)`` — the phrase the user must
  type after ``approve`` for L3 patches. Derived deterministically
  from the patch content so the same patch always wants the same
  phrase (and a re-issued patch picks up a fresh one when the
  content changes).
* ``tier_for(action)`` — the friction tier (L1 / L2 / L3) the
  approval banner should use.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional


# ============================================================
# Tier resolution
# ============================================================

#: Friction tier per pending_approval kind. Phase 2 maps the
#: existing kinds onto sensible defaults; an action MAY override
#: by setting its own ``tier`` field (e.g. a high-stakes
#: ``workspace_write`` to ``CONTROL_SYSTEM_MESSAGE.md`` could lift
#: itself to L3).
KIND_DEFAULT_TIER: Dict[str, str] = {
    # Workspace ops — diff-and-confirm (today's behaviour, preserved).
    "write": "L2",
    "delete": "L2",
    "bundle": "L2",
    # Read-only — single-tap (today's behaviour, preserved).
    "read": "L1",
    "search": "L1",
    # Cowork plan — diff-and-confirm (the plan IS the diff).
    "cowork_plan": "L2",
    # Browser nav — diff-and-confirm (origin allow-listing).
    "browser_navigate": "L2",
    # Tool call — diff-and-confirm. The command is shown verbatim;
    # the user reads it before approving. The recursive dispatch
    # then enforces per-command safety (workspace writes still
    # need build mode + the workspace approval gate even when
    # initiated via a tool call).
    "tool_call": "L2",
}


def tier_for(action: Optional[Dict[str, Any]]) -> str:
    """Return the L1 / L2 / L3 friction tier for an approval action.
    Honours an explicit ``tier`` on the action, else falls back to
    the per-kind default. Defaults to L2 for unknown kinds (= the
    safer default — unknown things ask for review)."""
    if not isinstance(action, dict):
        return "L2"
    explicit = str(action.get("tier") or "").strip().upper()
    if explicit in {"L1", "L2", "L3"}:
        return explicit
    kind = str(action.get("kind") or "").strip().lower()
    return KIND_DEFAULT_TIER.get(kind, "L2")


# ============================================================
# Impact statements (shell-generated, NOT model-generated)
# ============================================================

def impact_statement_for(action: Optional[Dict[str, Any]]) -> str:
    """Return a one-or-two-sentence impact description for the
    approval banner. Static text per kind — the shell, not the
    model, decides how to describe each change.

    For L3 patches, this is shown prominently above the typed
    confirmation prompt. For L1/L2 patches, callers may include it
    as a sub-line; some kinds get an empty string when the existing
    banner already conveys enough."""
    if not isinstance(action, dict):
        return ""
    kind = str(action.get("kind") or "").strip().lower()

    if kind == "write":
        path = action.get("path") or "(no path)"
        existed = bool(action.get("create")) is False  # `create=True` means new
        if existed:
            return f"Will MODIFY existing file '{path}' on disk. Undoable via `undo`."
        return f"Will CREATE new file '{path}' on disk. Undoable via `undo`."

    if kind == "delete":
        path = action.get("path") or "(no path)"
        return (
            f"Will DELETE '{path}' from disk. Undoable via `undo` "
            "(restores previous content)."
        )

    if kind == "bundle":
        sub = action.get("sub_actions") or []
        n_writes = sum(1 for s in sub if s.get("kind") == "write")
        n_deletes = sum(1 for s in sub if s.get("kind") == "delete")
        bits = []
        if n_writes:
            bits.append(f"{n_writes} write{'s' if n_writes != 1 else ''}")
        if n_deletes:
            bits.append(f"{n_deletes} delete{'s' if n_deletes != 1 else ''}")
        plan = " + ".join(bits) if bits else f"{len(sub)} action(s)"
        return (
            f"Atomic bundle: {plan}. All apply together; any failure "
            f"rolls back every write/delete already applied."
        )

    if kind == "cowork_plan":
        run_id = action.get("run_id") or "(unknown)"
        return (
            f"Approves cowork plan {run_id} for execution. Sub-tasks "
            f"will dispatch to linked builders."
        )

    if kind == "browser_navigate":
        origin = action.get("origin_host") or "(unknown)"
        return (
            f"Adds origin '{origin}' to the browser allow-list AND "
            f"performs the navigation. Subsequent visits to this "
            f"origin will not require re-approval."
        )

    if kind == "read":
        paths = action.get("paths") or []
        return f"Reads {len(paths)} file(s) from the workspace. No disk mutation."

    if kind == "search":
        query = action.get("query") or ""
        return f"Searches the workspace for '{query}'. No disk mutation."

    if kind == "tool_call":
        command = (action.get("command") or "").replace("\n", " ").strip()
        preview = command if len(command) <= 80 else command[:77] + "..."
        return (
            f"Will recursively run the dispatcher command "
            f"`{preview}` as if you typed it. Per-command safety "
            f"nets (mode gate, workspace approval, write cap) still "
            f"apply at execution time."
        )

    # System prompt update — placeholder (not yet runtime-supported,
    # but the impact text exists so the L3 framework is testable).
    if kind == "system_prompt_update":
        return (
            "Will REPLACE the control-layer's CONTROL_SYSTEM_MESSAGE. "
            "This affects every chat turn going forward, in every "
            "session. Cannot be undone via `undo`; revert requires "
            "re-issuing a system_prompt_update with the previous text."
        )

    return ""


# ============================================================
# Confirmation phrase (L3 only)
# ============================================================

#: Length of the L3 confirmation phrase. 6 chars of base32 = ~30
#: bits of entropy, enough that muscle-memory typing is impossible
#: but short enough that copy-paste-with-a-glance works.
_CONFIRMATION_PHRASE_LEN = 6


def confirmation_phrase_for(action: Optional[Dict[str, Any]]) -> str:
    """Return the case-insensitive phrase the user must type after
    ``approve`` for an L3 patch. Empty string for non-L3 patches.

    The phrase is a deterministic short hash of the patch's content
    (kind + path + content for writes, kind + run_id for cowork_plan,
    etc.) — same patch always wants the same phrase. If the user
    re-issues the patch with different content, the phrase changes
    and any stale memorised value is rejected.

    Lowercase a-z + 2-7 (Crockford base32 minus i/l/o/u to avoid
    confusables) so spelling out from the banner is unambiguous."""
    if tier_for(action) != "L3":
        return ""
    if not isinstance(action, dict):
        return ""
    kind = str(action.get("kind") or "").strip().lower()
    # Deterministic input string per kind. Use the fields that
    # actually drive the patch's effect; ignore origin / requested_at
    # so identical patches from different surfaces get the same
    # phrase.
    bits: list[str] = [kind]
    for field in ("workspace_path", "path", "content", "run_id", "url",
                  "origin_host"):
        value = action.get(field)
        if value:
            bits.append(f"{field}={value}")
    sub = action.get("sub_actions")
    if isinstance(sub, list):
        for entry in sub:
            if isinstance(entry, dict):
                bits.append(
                    f"sub:{entry.get('kind', '')}:{entry.get('path', '')}"
                )
    blob = "|".join(bits).encode("utf-8")
    digest = hashlib.sha256(blob).digest()
    # Base32 encode + lowercase + drop padding + pick first N safe chars.
    import base64
    encoded = base64.b32encode(digest).decode("ascii").lower()
    safe = "".join(ch for ch in encoded if ch in "abcdefghjkmnpqrstvwxyz234567")
    return safe[:_CONFIRMATION_PHRASE_LEN]


def confirmation_matches(action: Optional[Dict[str, Any]], supplied: str) -> bool:
    """Compare the user-supplied phrase against the expected one,
    case-insensitive. Returns True when they match (or when the
    action isn't L3 — non-L3 always 'matches' since no phrase is
    required)."""
    expected = confirmation_phrase_for(action)
    if not expected:
        # Non-L3 patches don't require a phrase; any input is fine
        # (callers shouldn't be calling this for non-L3 anyway).
        return True
    return (supplied or "").strip().lower() == expected
