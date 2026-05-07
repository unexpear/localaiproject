"""Layered prompt assembly (Phase 1 + Phase 2 — see docs/design/prompt-assembly.md).

Replaces the ad-hoc string-splicing in ``_build_runtime_messages``
with explicit, typed slot producers. One ``PromptSlot`` per
contributor; assembly emits each as a system message in priority
order and produces a per-slot trace the chat result attaches.

Phase 1 (shipped): pure refactor — same outputs as the legacy code
path, every contribution traceable.

Phase 2 (shipped): per-slot ``char_budget`` truncates long slots
in-place; ``total_budget`` evicts low-priority slots when the sum
of slot contents would otherwise exceed the cap. Slots with priority
0 are pinned and never evicted.

Phase 3 (planned): new sources — PERSONA.md / project AGENTS.md /
attached skills — registered as additional producers.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


#: Default priority used when a slot doesn't specify one. Higher
#: priority *number* = lower importance = evicted first when the
#: assembly is over its total budget.
DEFAULT_PRIORITY = 50

#: Default total-budget cap for ``PromptAssembly`` (in characters,
#: not tokens — see "honest gaps" in the design doc). 24 KB roughly
#: corresponds to ~6,000 words / ~6,000 tokens for English prose;
#: leaves plenty of room for chat history under typical 16k–32k
#: context windows. Override per-call via the ``total_budget``
#: constructor arg, or per-session via the ``prompt_total_budget``
#: session-context field (see ``chat_service.build_runtime_assembly``).
DEFAULT_TOTAL_BUDGET = 24_000

#: Marker appended to a slot's content when truncated to fit
#: ``char_budget``. Counts against the budget so the final string
#: never exceeds it.
_TRUNCATION_MARKER = "\n…[truncated]"

#: Reason recorded on the trace when a slot is evicted because the
#: assembly was over its total budget.
EVICT_REASON_OVER_BUDGET = "over_budget"

#: Slots at this priority are *pinned* — never evicted, even if the
#: assembly is way over budget. The CONTROL_SYSTEM_MESSAGE and the
#: mode framing live here.
PIN_PRIORITY = 0


# ============================================================
# Slot dataclass
# ============================================================

@dataclass
class PromptSlot:
    """One contribution to the runtime prompt.

    Each non-empty slot becomes one system message at assemble time.
    The ``trace`` returned by ``PromptAssembly.assemble`` mirrors
    these dataclasses (without the content) so consumers can see
    *which slot emitted what number of bytes* without re-reading
    the full prompt.
    """

    name: str
    content: str
    char_budget: int = 0          # 0 = unbounded (Phase 1 default)
    priority: int = DEFAULT_PRIORITY
    truncated: bool = False
    evicted: bool = False
    evicted_reason: str = ""

    def is_empty(self) -> bool:
        return not (self.content or "").strip()

    def char_count(self) -> int:
        return len(self.content or "")


# ============================================================
# Assembly
# ============================================================

class PromptAssembly:
    """Collects ``PromptSlot``s and emits the final message list.

    On ``assemble()``:

    1. Each slot with ``char_budget > 0`` whose content exceeds it
       is truncated in place; the trace records ``truncated=True``.
    2. If the sum of slot contents exceeds ``total_budget``, slots
       with priority > ``PIN_PRIORITY`` are evicted in order of
       *highest priority number first* (least important) until the
       running total fits, OR no more evictable slots remain.
    3. Surviving non-empty slots are emitted as system messages,
       ordered by ``(priority, insertion_index)``.

    Slots are not mutated — ``assemble()`` operates on deep copies
    so the caller's slot objects remain untouched.
    """

    def __init__(self, *, total_budget: int = 0) -> None:
        self._slots: List[PromptSlot] = []
        self._total_budget = int(total_budget or 0)

    def add(self, slot: Optional[PromptSlot]) -> None:
        """Add a slot. Silently no-ops on None so callers can pass
        the result of an Optional-returning producer directly."""
        if slot is None:
            return
        self._slots.append(slot)

    def add_all(self, slots) -> None:
        for slot in slots or ():
            self.add(slot)

    def apply_budgets(self, budgets: Optional[Dict[str, int]]) -> None:
        """Override per-slot ``char_budget`` from a name → budget
        mapping. Only slots whose name is in the dict are touched;
        slots already carrying a non-zero ``char_budget`` (e.g.
        memory blocks with a ``char_limit``) are NOT overwritten if
        the override is 0 — caller can pass an explicit non-zero
        override to clear that.

        Used by ``chat_service.build_runtime_assembly`` to thread
        ``session.context.prompt_budgets`` through to the slots
        before ``assemble()`` runs."""
        if not budgets:
            return
        for slot in self._slots:
            if slot.name not in budgets:
                continue
            override = int(budgets[slot.name] or 0)
            # Don't silently weaken a slot's existing budget by
            # passing 0 — only apply when the override is positive,
            # OR when the slot had no budget set yet.
            if override > 0 or slot.char_budget == 0:
                slot.char_budget = override

    @property
    def total_budget(self) -> int:
        return self._total_budget

    def set_total_budget(self, value: int) -> None:
        self._total_budget = int(value or 0)

    def assemble(self) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
        """Produce ``(messages, trace)``.

        ``messages`` is the list of ``{"role": "system", "content": ...}``
        dicts ready for the runtime client. ``trace`` is a list of
        per-slot dicts the chat result exposes to consumers.
        """
        # Work on deep copies so the caller's slot objects are not
        # mutated when we truncate / mark evicted. Pair each with
        # its insertion index — both eviction order and final emit
        # order use it as a stable tiebreaker.
        indexed: List[Tuple[int, PromptSlot]] = [
            (i, copy.deepcopy(s)) for i, s in enumerate(self._slots)
        ]

        # Phase 2 step 1: per-slot truncation.
        for _, slot in indexed:
            self._maybe_truncate(slot)

        # Phase 2 step 2: total-budget eviction (only when a budget
        # is set).
        if self._total_budget > 0:
            self._maybe_evict(indexed)

        # Phase 1 step 3: emit in (priority, insertion_index) order.
        indexed.sort(key=lambda pair: (pair[1].priority, pair[0]))
        messages: List[Dict[str, str]] = []
        trace: List[Dict[str, Any]] = []
        for _, slot in indexed:
            trace.append(self._trace_entry(slot))
            if slot.is_empty() or slot.evicted:
                continue
            messages.append({"role": "system", "content": slot.content})
        return messages, trace

    @staticmethod
    def _maybe_truncate(slot: PromptSlot) -> None:
        if slot.char_budget <= 0:
            return
        if slot.char_count() <= slot.char_budget:
            return
        marker = _TRUNCATION_MARKER
        keep = max(0, slot.char_budget - len(marker))
        if keep <= 0:
            slot.content = marker[: slot.char_budget]
        else:
            slot.content = (slot.content or "")[:keep] + marker
        slot.truncated = True

    def _maybe_evict(self, indexed: List[Tuple[int, PromptSlot]]) -> None:
        """Evict highest-priority-number slots first until under
        ``total_budget``. Pinned slots (priority == PIN_PRIORITY)
        and already-empty / already-evicted slots are skipped.

        ``indexed`` is the (insertion_index, slot) pairing — used as
        a stable tiebreaker so the LAST-inserted high-priority slot
        falls first when several share the same priority number.
        """
        def live_total() -> int:
            return sum(
                slot.char_count()
                for _, slot in indexed
                if not slot.evicted and not slot.is_empty()
            )

        running = live_total()
        if running <= self._total_budget:
            return

        # Candidates: not pinned, not already empty/evicted. Sort
        # by (-priority, -insertion_index) so the LAST inserted
        # highest-number slot is evicted first.
        candidates = [
            (idx, slot)
            for idx, slot in indexed
            if slot.priority > PIN_PRIORITY
            and not slot.is_empty()
            and not slot.evicted
        ]
        candidates.sort(key=lambda pair: (-pair[1].priority, -pair[0]))
        for _, slot in candidates:
            if running <= self._total_budget:
                return
            slot.evicted = True
            slot.evicted_reason = EVICT_REASON_OVER_BUDGET
            running -= slot.char_count()
        # If we exit the loop still over budget, the pinned slots
        # alone exceed total_budget — caller surfaces this via the
        # `prompt_budget_exceeded` warning. We don't truncate pinned
        # slots; honesty over silently dropping the preamble.

    @staticmethod
    def _trace_entry(slot: PromptSlot) -> Dict[str, Any]:
        return {
            "name": slot.name,
            "chars": slot.char_count(),
            "priority": slot.priority,
            "char_budget": slot.char_budget,
            "truncated": slot.truncated,
            "evicted": slot.evicted,
            "evicted_reason": slot.evicted_reason,
        }


# ============================================================
# Slot producers
# ============================================================
#
# Each producer takes the inputs it needs and returns either a
# PromptSlot or None (so callers can `assembly.add(producer(...))`
# without conditionals at the call site).
# ============================================================

def control_slot(control_message: Dict[str, str]) -> PromptSlot:
    """The CONTROL_SYSTEM_MESSAGE preamble. Always emitted (priority 0)."""
    return PromptSlot(
        name="control",
        content=control_message.get("content", ""),
        priority=0,
    )


def mode_slot(mode_message: Dict[str, str]) -> PromptSlot:
    """The Plan/Build mode framing. Always emitted (priority 0)."""
    return PromptSlot(
        name="mode",
        content=mode_message.get("content", ""),
        priority=0,
    )


def tool_calls_slot(enabled: bool) -> Optional[PromptSlot]:
    """Phase 2 of tool-calling.md — when ``enabled`` is True,
    inject the teaching message that tells the model about the
    ``​```dispatcher​`` fence format. When False (the default),
    contributes nothing.

    Priority 8 sits between mode (0) and skills (10) so the
    behavioural framing arrives early but doesn't interrupt the
    Plan/Build mode message."""
    if not enabled:
        return None
    from app.services.tool_calls import TOOL_CALLS_TEACHING_MESSAGE
    return PromptSlot(
        name="tool_calls",
        content=TOOL_CALLS_TEACHING_MESSAGE,
        priority=8,
    )


def persona_slot(
    content: Optional[str], *, compact: bool = False,
) -> Optional[PromptSlot]:
    """Hermes-inspired ``PERSONA.md`` contents — how the assistant
    should behave in this workspace. Loaded by
    ``project_context_service.read_persona``. Higher priority than
    skills/workspace because it shapes everything below.

    ``compact=True`` (Phase 1 of compact-prompt.md) strips the
    prose framing — the markdown already came from a file, the
    model knows what `## Persona` means."""
    if not content or not content.strip():
        return None
    body = content.strip()
    if compact:
        rendered = f"## Persona\n{body}"
    else:
        rendered = (
            "Project persona (read from `PERSONA.md` at the workspace root):\n\n"
            + body
        )
    return PromptSlot(name="persona", content=rendered, priority=5)


def project_context_slot(
    content: Optional[str], *, compact: bool = False,
) -> Optional[PromptSlot]:
    """Hermes-inspired project ``AGENTS.md`` contents — durable
    project-level guidance loaded from the user's pinned workspace
    root. Distinct from the control-layer's own ``AGENTS.md`` in
    the source tree: this one is at *the user's* workspace and
    tells the model about *their* project."""
    if not content or not content.strip():
        return None
    body = content.strip()
    if compact:
        rendered = f"## Project\n{body}"
    else:
        rendered = (
            "Project guidance (read from `AGENTS.md` at the workspace root):\n\n"
            + body
        )
    return PromptSlot(name="project_context", content=rendered, priority=7)


#: Render order within the custom-instructions slot. ``override``
#: lands first because it reshapes every later rule's
#: interpretation; ``style`` lands last because it's the
#: lowest-stakes layer. See `docs/design/custom-instructions.md`.
_CUSTOM_INSTRUCTION_KIND_ORDER = (
    "override",
    "constraint",
    "persona",
    "knowledge",
    "style",
)

#: Two-letter prefixes used in compact mode.
_CUSTOM_INSTRUCTION_COMPACT_PREFIX = {
    "override": "O",
    "constraint": "C",
    "persona": "P",
    "knowledge": "K",
    "style": "S",
}


def _filter_applicable_instructions(
    instructions: Optional[List[Dict[str, Any]]],
    *,
    mode: Optional[str] = None,
    workspace_path: Optional[str] = None,
    tags: Optional[List[str]] = None,
    lane: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the subset of ``instructions`` whose ``applies_to``
    predicate matches the runtime context. Disabled entries are
    dropped. AND across keys; ANY across list-valued keys.

    See `docs/design/custom-instructions.md` Filter semantics.
    """
    if not instructions:
        return []
    session_tags = set(tags or [])
    out: List[Dict[str, Any]] = []
    for entry in instructions:
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        # Phase 2.5 — `instructions hush <id> --turns N` mutes an
        # entry for N turns without flipping ``enabled``. Treat a
        # positive ``hushed_remaining`` as if it were disabled.
        try:
            hush_left = int(entry.get("hushed_remaining") or 0)
        except (TypeError, ValueError):
            hush_left = 0
        if hush_left > 0:
            continue
        applies_to = entry.get("applies_to") or {}
        if not isinstance(applies_to, dict):
            applies_to = {}
        # mode predicate
        wanted_mode = applies_to.get("mode")
        if wanted_mode and wanted_mode != (mode or ""):
            continue
        # workspace predicate (exact match)
        wanted_ws = applies_to.get("workspace")
        if wanted_ws and wanted_ws != (workspace_path or ""):
            continue
        # tags predicate (any-of)
        wanted_tags = applies_to.get("tags")
        if wanted_tags:
            if not session_tags.intersection(wanted_tags):
                continue
        # lanes predicate (any-of against the resolved lane)
        wanted_lanes = applies_to.get("lanes")
        if wanted_lanes:
            if not lane or lane not in wanted_lanes:
                continue
        out.append(entry)
    return out


def custom_instructions_slot(
    instructions: Optional[List[Dict[str, Any]]],
    *,
    mode: Optional[str] = None,
    workspace_path: Optional[str] = None,
    tags: Optional[List[str]] = None,
    lane: Optional[str] = None,
    compact: bool = False,
) -> Optional[PromptSlot]:
    """Phase 1 of `docs/design/custom-instructions.md` — render the
    user's typed/conditional behavioural rules as one slot.

    Filters by ``applies_to`` against the runtime context, sorts by
    ``(_CUSTOM_INSTRUCTION_KIND_ORDER, priority, label)``, then
    renders each surviving entry with a tight type-aware header.

    Returns ``None`` when no instruction is applicable, so the slot
    contributes nothing to the prompt in the common (empty list)
    case.

    Priority **3** — between ``mode`` (0) and ``persona`` (5).
    Custom instructions are an explicit override of *what we send*,
    so they arrive before the file-derived persona/AGENTS context.
    """
    applicable = _filter_applicable_instructions(
        instructions,
        mode=mode,
        workspace_path=workspace_path,
        tags=tags,
        lane=lane,
    )
    if not applicable:
        return None

    def _kind_index(kind: str) -> int:
        try:
            return _CUSTOM_INSTRUCTION_KIND_ORDER.index(kind)
        except ValueError:
            return len(_CUSTOM_INSTRUCTION_KIND_ORDER)

    applicable.sort(
        key=lambda e: (
            _kind_index(e.get("kind") or ""),
            int(e.get("priority") or 50),
            (e.get("label") or "").lower(),
        )
    )

    sections: List[str] = []
    for entry in applicable:
        kind = entry.get("kind") or "constraint"
        label = (entry.get("label") or "").strip()
        body = (entry.get("body") or "").strip()
        if compact:
            prefix = _CUSTOM_INSTRUCTION_COMPACT_PREFIX.get(kind, kind[:1].upper())
            header = f"{prefix}:{label}" if label else prefix
            sections.append(f"{header}\n{body}")
        else:
            kind_title = kind.capitalize()
            header = f"### {kind_title}: {label}" if label else f"### {kind_title}"
            sections.append(f"{header}\n{body}")
    if not sections:
        return None
    if compact:
        content = "\n\n".join(sections)
    else:
        content = "Custom instructions:\n\n" + "\n\n".join(sections)
    return PromptSlot(name="custom_instructions", content=content, priority=3)


def skills_slot(skill_message: Optional[Dict[str, str]]) -> Optional[PromptSlot]:
    if not skill_message:
        return None
    return PromptSlot(
        name="skills",
        content=skill_message.get("content", ""),
        priority=10,
    )


def workspace_slot(workspace_message: Optional[Dict[str, str]]) -> Optional[PromptSlot]:
    if not workspace_message:
        return None
    return PromptSlot(
        name="workspace",
        content=workspace_message.get("content", ""),
        priority=15,
    )


def inspection_slot(inspection_message: Optional[Dict[str, str]]) -> Optional[PromptSlot]:
    """Approved workspace read/search result rendered for the model.
    Higher priority (lower number) than `summary` because the
    inspection is the user's explicit consent for THIS turn."""
    if not inspection_message:
        return None
    return PromptSlot(
        name="inspection",
        content=inspection_message.get("content", ""),
        priority=20,
    )


def mentions_slot(mentions_context_block: Optional[str]) -> Optional[PromptSlot]:
    if not mentions_context_block:
        return None
    return PromptSlot(
        name="mentions",
        content=mentions_context_block,
        priority=25,
    )


def memory_block_slots(
    blocks: Optional[Dict[str, Dict[str, Any]]], *, compact: bool = False,
) -> List[PromptSlot]:
    """One slot per non-empty memory block. The block's ``priority``
    field is mirrored onto the slot so persona (priority 0) renders
    before working_memory (priority 3).

    Slot name format: ``memory:<label>`` so the trace tells the
    consumer *which* block contributed (vs. one merged 'memory'
    entry that hides the per-block detail).

    ``compact=True`` (Phase 1 of compact-prompt.md) drops the
    ``Memory block 'label': description\\n\\n`` prose framing in
    favour of a tight ``## label\\n<value>`` header. The
    description is dropped from the rendered output (it was
    framing for the model; the label + value carry the meaning).
    """
    if not blocks:
        return []
    out: List[PromptSlot] = []
    # Sort by (priority, label) so equal-priority blocks render
    # alphabetically — matches the legacy `_build_runtime_messages`
    # behaviour. The assembler's `(priority, insertion_index)` sort
    # then preserves this order in the final message list.
    for label, block in sorted(
        blocks.items(),
        key=lambda kv: (kv[1].get("priority", 100), kv[0]),
    ):
        value = (block.get("value") or "").strip()
        if not value:
            continue
        description = (block.get("description") or "").strip()
        if compact:
            content = f"## {label}\n{value}"
        else:
            header = f"Memory block '{label}'"
            if description:
                header += f": {description}"
            content = f"{header}\n\n{value}"
        # Priority: keep memory blocks ordered after the existing
        # "structural" slots (control/mode/skills/workspace/inspection
        # /mentions all use 0..25) but before the summary.
        # We bias by 30 so default working_memory (priority 3) still
        # comes after mentions (25). Persona (priority 0) → 30.
        slot_priority = 30 + int(block.get("priority") or 0)
        out.append(PromptSlot(
            name=f"memory:{label}",
            content=content,
            char_budget=int(block.get("char_limit") or 0),
            priority=slot_priority,
        ))
    return out


def summary_slot(summary_message: Optional[Dict[str, str]]) -> Optional[PromptSlot]:
    if not summary_message:
        return None
    return PromptSlot(
        name="summary",
        content=summary_message.get("content", ""),
        priority=80,
    )


#: Phase 2b of recall-memory.md — minimum characters in the user
#: message before we'll consult the BM25 index. Below this we skip
#: the slot entirely (greetings / tiny replies aren't worth a
#: retrieval round trip).
_RETRIEVED_DOCS_MIN_QUERY_CHARS = 8

#: Phase 2b — top-N BM25 hits to inject. Beyond ~5, returns get
#: noisy and the marginal hit's score is usually low.
_RETRIEVED_DOCS_LIMIT = 5


def retrieved_docs_slot(
    query: Optional[str],
    *,
    session_id: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> Optional[PromptSlot]:
    """Phase 2b of recall-memory.md (Hermes/local-AI-workbench
    inspired): inject the top BM25 matches against the workspace's
    text files as a system message the model sees on this turn.

    Deliberately conservative — returns None (no slot) when:

    * No workspace is pinned (nothing to retrieve from).
    * No BM25 index has been built yet for this session. This
      avoids a surprise multi-second indexing pause on the first
      chat turn after pinning a workspace; the user opts in by
      running ``retrieve build`` (or any ``retrieve <query>``)
      once. Future Phase 2c could add a background warm-up.
    * The query is shorter than ``_RETRIEVED_DOCS_MIN_QUERY_CHARS``
      (greetings / yes-no answers don't merit retrieval).
    * No hits are returned (the message has no in-vocab terms).

    Output shape: a single system message listing the top hits
    with a ``[file:<path>]`` citation marker, BM25 score, and a
    short snippet — the same citation format the existing
    ``recall`` command emits, so the model can reproduce it in
    its replies.

    Priority 28 — slots between mentions (25) and memory blocks
    (30+) so retrieved context appears AFTER the user's explicit
    @file mentions but BEFORE the persisted memory blocks.
    """
    if not workspace_path:
        return None
    if not query or len(query.strip()) < _RETRIEVED_DOCS_MIN_QUERY_CHARS:
        return None
    if not session_id:
        return None

    # Lazy-import so prompt_assembly stays a leaf module for
    # callers that don't want the retrieval dependency.
    from app.services import retrieval_index as ri

    cached = ri.status(session_id)
    if not cached.get("cached"):
        # Don't auto-build — see docstring. The user opts in via
        # ``retrieve build``.
        return None

    hits, _diag = ri.search(
        session_id, query=query, limit=_RETRIEVED_DOCS_LIMIT,
        workspace_path=workspace_path,
    )
    if not hits:
        return None

    lines: List[str] = [
        "Workspace context (top BM25 matches for the user's message). "
        "Cite as [file:<path>] in your reply when you use one:",
        "",
    ]
    for hit in hits:
        lines.append(
            f"[file:{hit.path}]  (score: {hit.score:.3f})"
        )
        if hit.snippet:
            lines.append(f"  {hit.snippet}")
        lines.append("")
    content = "\n".join(lines).rstrip()
    return PromptSlot(
        name="retrieved_docs",
        content=content,
        priority=28,
    )


# ============================================================
# Phase 2 — per-slot budget defaults
# ============================================================
#
# Sane character budgets per slot. 0 = unbounded. Memory blocks set
# their own char_budget from the block's ``char_limit`` (notes-buffer
# Phase 2) so they aren't listed here. Override per-session via
# ``session.context["prompt_budgets"]`` — see
# ``chat_service.build_runtime_assembly``.
#
# Defaults are deliberately generous: truncation only kicks in when
# a slot's content is genuinely huge (a 16 KB workspace inspection,
# a 20 KB mentions expansion, etc.). The Phase 2 "honest gap": these
# are character counts, not token counts.

SLOT_BUDGET_DEFAULTS: Dict[str, int] = {
    "control": 0,
    "mode": 0,
    "tool_calls": 2_000,
    "custom_instructions": 8_192,
    "persona": 8_000,
    "project_context": 12_000,
    "skills": 4_000,
    "workspace": 4_000,
    "inspection": 16_000,
    "mentions": 16_000,
    "retrieved_docs": 10_000,
    "summary": 8_000,
}


def live_total_chars(trace: Optional[List[Dict[str, Any]]]) -> int:
    """Sum of ``chars`` across non-evicted entries in the trace —
    the actual byte cost of what got rendered. Used by callers to
    detect ``prompt_budget_exceeded`` (pinned slots alone exceed
    the total budget; eviction can't recover)."""
    if not trace:
        return 0
    return sum(int(t.get("chars", 0) or 0) for t in trace if not t.get("evicted"))


#: Phase 1 of compact-prompt.md — module-level helpers for the
#: opt-in compact mode. Each slot producer reads its ``compact``
#: kwarg; ``build_runtime_assembly`` reads
#: ``session.context["compact_format"]`` and passes it through.

def is_compact_enabled(session_id: str = "default") -> bool:
    """True iff this session has opted into dense slot rendering."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id) or {}
    return bool(context.get("compact_format"))


def set_compact(session_id: str = "default", enabled: bool = False) -> bool:
    """Persist the ``compact_format`` flag for a session. Returns the
    new value."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id)
    context["compact_format"] = bool(enabled)
    local_memory.update_session_context(session_id=session_id, context=context)
    return bool(enabled)


def trace_has_pressure(trace: Optional[List[Dict[str, Any]]]) -> bool:
    """True if the assembly trace records any truncation, eviction,
    or an over-budget warning.

    Used by the chat-output formatter to decide whether to render the
    ``[prompt: ...]`` budget footer."""
    if not trace:
        return False
    for entry in trace:
        if entry.get("truncated") or entry.get("evicted"):
            return True
        if entry.get("name") == "_diagnostics" and entry.get("over_budget"):
            return True
    return False


def render_trace_footer(trace: Optional[List[Dict[str, Any]]]) -> str:
    """Render a single-line ``[prompt: name/N · name/EVICTED]``-style
    footer for the chat output. Empty string if no pressure was
    recorded — the caller can append unconditionally."""
    if not trace_has_pressure(trace):
        return ""
    parts: List[str] = []
    over_budget = False
    for entry in trace or []:
        if entry.get("name") == "_diagnostics":
            if entry.get("over_budget"):
                over_budget = True
            continue
        name = entry.get("name", "?")
        if entry.get("evicted"):
            parts.append(f"{name}/EVICTED")
        elif entry.get("truncated"):
            parts.append(f"{name}/{entry.get('chars', 0)}~")
        else:
            # Non-pressure entries are skipped — the footer is only
            # for what changed.
            continue
    if over_budget:
        parts.append("OVER_BUDGET")
    if not parts:
        return ""
    return "[prompt: " + " · ".join(parts) + "]"


def trace_over_budget(trace: Optional[List[Dict[str, Any]]]) -> bool:
    """Return True if the trace's diagnostic entry indicates the
    pinned slots alone exceed ``total_budget`` (the
    ``prompt_budget_exceeded`` warning)."""
    if not trace:
        return False
    for entry in trace:
        if entry.get("name") == "_diagnostics" and entry.get("over_budget"):
            return True
    return False
