"""Patch taxonomy — typed mutation envelope (Phase 1).

See ``docs/design/patch-taxonomy.md`` for the full design. Phase 1
introduces the ``Patch`` dataclass + the seven kind constants + a
``bundle`` aggregation helper. The existing approval flow continues
to work unchanged: today's ``pending_approval kind="write"`` rows
are equivalent to a Phase-1 ``Patch(kind=PATCH_WORKSPACE_WRITE)``
envelope. The new behaviour Phase 1 ships is the ``bundle`` kind —
one approval surface for N coordinated sub-patches with atomic
apply/rollback.

Phase 2 (planned) layers L1/L2/L3 friction tiers + per-kind
shell-generated impact statements.

Phase 3 (planned) adds the proactive ``honest null`` trigger,
emitting ``Patch`` proposals from a periodic agent loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ============================================================
# Patch kinds (the closed vocabulary)
# ============================================================
#
# Mirrors the seven kinds in patch-taxonomy.md. Phase 1 only fully
# implements the first two (``WORKSPACE_WRITE`` is just a typed wrap
# around today's pending_approval; ``BUNDLE`` is genuinely new).
# The remaining five are reserved as constants so callers can
# pattern-match against them when later phases land.
# ============================================================

PATCH_WORKSPACE_WRITE = "workspace_write"
PATCH_BUNDLE = "bundle"
PATCH_MEMORY_BLOCK_UPDATE = "memory_block_update"   # planned (Phase 2)
PATCH_CONFIG_UPDATE = "config_update"               # planned (Phase 2)
PATCH_PROMPT_UPDATE = "prompt_update"               # planned (Phase 2)
PATCH_SYSTEM_PROMPT_UPDATE = "system_prompt_update"  # planned (Phase 2 — L3)

#: Every kind currently registered. Used by validators to refuse
#: unknown kinds with ``unknown_patch_kind``.
KNOWN_PATCH_KINDS = frozenset({
    PATCH_WORKSPACE_WRITE,
    PATCH_BUNDLE,
    PATCH_MEMORY_BLOCK_UPDATE,
    PATCH_CONFIG_UPDATE,
    PATCH_PROMPT_UPDATE,
    PATCH_SYSTEM_PROMPT_UPDATE,
})

#: Phase 1 fully implements these. Other kinds in KNOWN_PATCH_KINDS
#: are placeholders for later phases.
PHASE_1_KINDS = frozenset({PATCH_WORKSPACE_WRITE, PATCH_BUNDLE})


# ============================================================
# Friction tiers (Phase 2 will start using these for per-kind
# defaults; Phase 1 already records them so the trace is right)
# ============================================================

TIER_L1 = "L1"   # single-tap, easily reversible
TIER_L2 = "L2"   # diff-and-confirm (today's default)
TIER_L3 = "L3"   # typed-confirmation w/ shell-generated impact statement

#: Default friction tier per kind. Phase 1 only acts on TIER_L2 and
#: above for workspace_write (since that's the existing behaviour).
#: Phase 2 will surface the others.
DEFAULT_TIER_FOR_KIND: Dict[str, str] = {
    PATCH_WORKSPACE_WRITE: TIER_L2,
    PATCH_BUNDLE: TIER_L2,
    PATCH_MEMORY_BLOCK_UPDATE: TIER_L1,
    PATCH_CONFIG_UPDATE: TIER_L1,
    PATCH_PROMPT_UPDATE: TIER_L2,
    PATCH_SYSTEM_PROMPT_UPDATE: TIER_L3,
}


#: Hard cap on bundle size to keep the approval UI sane and to bound
#: the worst-case rollback window. Mirrors `personal app`'s 10-item
#: cap. Bundles over this size are split or refused.
BUNDLE_MAX_SUB_PATCHES = 10


# ============================================================
# Patch dataclass
# ============================================================

@dataclass
class Patch:
    """One typed proposal that the user can approve or deny.

    ``payload`` is kind-specific (mirrors today's
    ``pending_approval`` content for the corresponding kind). For
    bundles, ``sub_patches`` carries the list of child Patches; the
    bundle's payload is empty.
    """

    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    tier: str = TIER_L2
    origin: str = "chat"           # "chat" | "dispatcher" | "mcp" | "proactive"
    proposed_at: str = ""
    sub_patches: Optional[List["Patch"]] = None

    def __post_init__(self) -> None:
        if not self.proposed_at:
            self.proposed_at = datetime.now(timezone.utc).isoformat()
        if self.kind not in DEFAULT_TIER_FOR_KIND:
            # Don't override a caller-supplied tier; only fall back
            # to the per-kind default when the caller didn't set one.
            pass
        elif self.tier == TIER_L2 and DEFAULT_TIER_FOR_KIND[self.kind] != TIER_L2:
            self.tier = DEFAULT_TIER_FOR_KIND[self.kind]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kind": self.kind,
            "payload": dict(self.payload or {}),
            "rationale": self.rationale,
            "tier": self.tier,
            "origin": self.origin,
            "proposed_at": self.proposed_at,
        }
        if self.sub_patches is not None:
            out["sub_patches"] = [p.to_dict() for p in self.sub_patches]
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Patch":
        sub = data.get("sub_patches")
        sub_patches = (
            [cls.from_dict(p) for p in sub] if isinstance(sub, list) else None
        )
        return cls(
            kind=str(data.get("kind") or ""),
            payload=dict(data.get("payload") or {}),
            rationale=str(data.get("rationale") or ""),
            tier=str(data.get("tier") or TIER_L2),
            origin=str(data.get("origin") or "chat"),
            proposed_at=str(data.get("proposed_at") or ""),
            sub_patches=sub_patches,
        )


# ============================================================
# Validators
# ============================================================
#
# A validator returns either None (= ok) or a string reason tag
# (canonical error_vocabulary tag — caller surfaces it). Phase 1
# implements the two kinds we actually act on (``workspace_write``
# delegates to the existing workspace_service path resolver;
# ``bundle`` recursively validates its sub-patches).
# ============================================================


def _validate_workspace_write_payload(payload: Dict[str, Any]) -> Optional[str]:
    """Mirrors the rules ``_normalize_pending_approval`` already
    enforces for ``kind="write"``: workspace_path + path are
    required. Heavier validation (path traversal, symlink, write
    cap) happens later at apply time."""
    if not isinstance(payload, dict):
        return "invalid_argument"
    if not payload.get("workspace_path"):
        return "no_workspace"
    if not payload.get("path"):
        return "invalid_argument"
    return None


def _validate_bundle(patch: Patch) -> Optional[str]:
    if not patch.sub_patches:
        return "invalid_argument"  # empty bundles are nonsense
    if len(patch.sub_patches) > BUNDLE_MAX_SUB_PATCHES:
        return "bundle_too_large"
    for sub in patch.sub_patches:
        if sub.kind == PATCH_BUNDLE:
            # Nested bundles flatten — refuse. Composers should
            # have already flattened before emitting.
            return "invalid_argument"
        reason = validate_patch(sub)
        if reason:
            return reason
    return None


def validate_patch(patch: Patch) -> Optional[str]:
    """Return None if the patch is well-formed for its kind, else
    a canonical reason tag. Does NOT check approval gates / mode /
    write cap — those are runtime-state concerns the apply path
    handles. This is shape-validation only."""
    if patch.kind not in KNOWN_PATCH_KINDS:
        return "unknown_patch_kind"
    if patch.kind == PATCH_WORKSPACE_WRITE:
        return _validate_workspace_write_payload(patch.payload)
    if patch.kind == PATCH_BUNDLE:
        return _validate_bundle(patch)
    # Other kinds are placeholders for later phases — for now
    # signal that they're not yet runtime-supported.
    return "unknown_patch_kind"


# ============================================================
# Bundle composition
# ============================================================

def bundle_from_action_dicts(
    actions: List[Dict[str, Any]],
    *,
    rationale: str = "chat-flow multi-file change",
    origin: str = "chat",
) -> Optional[Dict[str, Any]]:
    """Build a ``pending_approval kind="bundle"`` action dict from
    a list of single-action dicts (write / delete envelopes). Phase
    1 wraps the existing approval-storage shape rather than swapping
    storage outright — the dispatcher's apply path keys on
    ``kind="bundle"`` and iterates ``sub_actions``.

    Returns None when the input list is empty or oversize. Caller
    should fall back to the original single-action behaviour.
    """
    if not actions:
        return None
    if len(actions) > BUNDLE_MAX_SUB_PATCHES:
        return None
    if len(actions) == 1:
        # No need to bundle a single action — keeps the existing
        # single-write approval flow untouched for the common case.
        return None

    # Phase 3b of tool-calling.md: bundles can mix workspace ops
    # (write/delete — workspace-bound) with tool_call sub-actions
    # (workspace-less). The bundle's top-level ``workspace_path``
    # is REQUIRED iff at least one sub-action is a write/delete;
    # tool-call-only bundles can omit it.
    kinds = {str(a.get("kind") or "").strip().lower() for a in actions}
    if not kinds.issubset({"write", "delete", "tool_call"}):
        # Unknown sub-action kind — caller's bug to surface.
        return None

    needs_workspace = bool(kinds & {"write", "delete"})
    shared_workspace: Optional[str] = None
    if needs_workspace:
        workspace_paths = {
            a.get("workspace_path")
            for a in actions
            if a.get("kind") in ("write", "delete")
            and a.get("workspace_path")
        }
        if len(workspace_paths) != 1:
            return None
        (shared_workspace,) = workspace_paths

    bundle: Dict[str, Any] = {
        "kind": "bundle",
        "origin": origin,
        "rationale": rationale,
        "sub_actions": [dict(action) for action in actions],
        "reason": rationale,
    }
    if shared_workspace:
        bundle["workspace_path"] = shared_workspace
    return bundle


def describe_bundle_summary(action: Dict[str, Any]) -> str:
    """Render a one-line summary of a bundle for the approval
    request UI. Distinct from ``describe_pending_action`` which
    handles single actions; the dispatcher delegates here when
    ``action["kind"] == "bundle"``.

    Bundle kinds are heterogeneous (Phase 3b of tool-calling.md):
    file changes (write/delete) get path-based summaries; tool
    calls get command-based summaries; mixed bundles label both.
    """
    sub = action.get("sub_actions") or []
    if not sub:
        return "Empty bundle (no sub-actions)"

    file_paths: List[str] = []
    tool_commands: List[str] = []
    for entry in sub:
        entry_kind = (entry.get("kind") or "").strip().lower()
        if entry_kind in ("write", "delete"):
            path = entry.get("path")
            if path:
                file_paths.append(str(path))
        elif entry_kind == "tool_call":
            cmd = (entry.get("command") or "").strip()
            if cmd:
                first_line = cmd.split("\n", 1)[0]
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                tool_commands.append(first_line)

    parts: List[str] = []
    if file_paths:
        head = ", ".join(file_paths[:3])
        if len(file_paths) > 3:
            head = f"{head}, +{len(file_paths) - 3} more"
        parts.append(
            f"{len(file_paths)} file change{'s' if len(file_paths) != 1 else ''}: {head}"
        )
    if tool_commands:
        head = " | ".join(tool_commands[:3])
        if len(tool_commands) > 3:
            head = f"{head} | +{len(tool_commands) - 3} more"
        parts.append(
            f"{len(tool_commands)} tool call{'s' if len(tool_commands) != 1 else ''}: {head}"
        )
    if not parts:
        return f"{len(sub)} sub-action(s) (no recognised content)"
    return "; ".join(parts)
