"""Proactive triggers — Phase 3 of `docs/design/patch-taxonomy.md`.

Adopted from the user's `personal app` self-modifying agent: a
periodic check that gives the model permission to *not* propose
anything. The model's response schema explicitly accepts
``{patch: null, reason: "..."}`` and the system prompt tells it
"the user did not summon you; respond null when there's no genuine
improvement to propose."

This module is structured for testability: the heuristic +
response parser are pure functions, and ``tick`` accepts an
injectable ``model_responder`` so unit tests don't need a live
LLM. The default responder routes through the chat-providers
infrastructure on the proactive lane.

State is persisted on ``session.context.proactive`` so the
heuristic survives process restart.

Honest scope (Phase 3a): we ship the trigger heuristic, the
prompt builder, the response parser, the null-streak idle
behaviour, and the dispatcher surface (``proactive status / tick /
reset / prompt``). We do NOT yet wire an automatic scheduler that
calls ``tick`` every 90s — that's Phase 3b. For now ``tick`` is
manually-invoked (or invoked by a future scheduler) and the
heuristic + lifecycle are correct.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# Heuristic constants (mirror personal app's defaults)
# ============================================================

#: Minimum interval between proactive checks. The heuristic refuses
#: to fire if less than this elapsed since ``last_check_at``.
MIN_POLL_INTERVAL_SECONDS = 90

#: After a proposal goes out (regardless of whether the user
#: approves), suppress further proactive ticks for this long. Keeps
#: the agent from spamming back-to-back proposals.
COOLDOWN_AFTER_PROPOSAL_SECONDS = 5 * 60

#: The trailing-window the activity gate considers. The user must
#: have done >= MIN_RECENT_ACTIONS distinct dispatcher actions in
#: this window for the proactive trigger to fire.
ACTIVITY_WINDOW_SECONDS = 5 * 60

#: Minimum count of recent user actions required to fire.
MIN_RECENT_ACTIONS = 2

#: After this many consecutive ``patch: null`` responses, the
#: trigger goes idle until the next user-initiated action resets
#: the streak. Prevents the model from being asked the same
#: ``anything to suggest?`` question forever when the answer is
#: clearly no.
MAX_NULL_STREAK = 3

#: Hard cap on stored ``recent_action_times`` so the field doesn't
#: grow without bound. We only need the trailing window count, so
#: anything older is fine to drop.
MAX_RECENT_ACTION_HISTORY = 64


# ============================================================
# Reasons the heuristic might refuse to fire — exposed as constants
# so callers can pattern-match without parsing English.
# ============================================================

REFUSE_TOO_SOON = "too_soon"
REFUSE_COOLDOWN = "cooldown"
REFUSE_TOO_QUIET = "too_quiet"
REFUSE_NULL_STREAK = "null_streak_idle"


# ============================================================
# State shape
# ============================================================

@dataclass
class ProactiveState:
    """In-memory mirror of ``session.context.proactive``. Loaded
    via ``load_state(session_id)`` and saved via
    ``save_state(session_id, state)`` — both go through
    ``local_memory`` so JSON / DuckDB storage stays consistent."""

    last_check_at: float = 0.0       # epoch seconds
    last_proposal_at: float = 0.0    # epoch seconds
    null_streak: int = 0             # consecutive null responses
    recent_action_times: List[float] = field(default_factory=list)
    last_response: Optional[Dict[str, Any]] = None  # last parsed response

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_check_at": float(self.last_check_at or 0.0),
            "last_proposal_at": float(self.last_proposal_at or 0.0),
            "null_streak": int(self.null_streak or 0),
            "recent_action_times": [
                float(t) for t in (self.recent_action_times or [])
            ],
            "last_response": self.last_response,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ProactiveState":
        data = data or {}
        return cls(
            last_check_at=float(data.get("last_check_at") or 0.0),
            last_proposal_at=float(data.get("last_proposal_at") or 0.0),
            null_streak=int(data.get("null_streak") or 0),
            recent_action_times=[
                float(t) for t in (data.get("recent_action_times") or [])
                if isinstance(t, (int, float))
            ],
            last_response=data.get("last_response"),
        )


# ============================================================
# Pure heuristic
# ============================================================

@dataclass
class ShouldFireResult:
    """Outcome of ``should_fire``. ``ok`` is the bottom-line bool
    the dispatcher uses; ``reason`` and ``detail`` give the user-
    facing explanation in ``proactive status``."""

    ok: bool
    reason: Optional[str] = None
    detail: str = ""


def should_fire(state: ProactiveState, *, now: Optional[float] = None) -> ShouldFireResult:
    """Pure decision function: given the state and the current
    time, should the proactive trigger fire?

    Refuses (with a typed reason) when:
    * Less than ``MIN_POLL_INTERVAL_SECONDS`` since the last check
      (cheap rate-limit on the heuristic itself).
    * Inside the post-proposal cooldown window.
    * Fewer than ``MIN_RECENT_ACTIONS`` user actions in the
      trailing ``ACTIVITY_WINDOW_SECONDS``.
    * The null streak has reached ``MAX_NULL_STREAK`` (model has
      said "nothing to suggest" enough times that we stop asking
      until the user does something new — see
      ``note_user_action`` for the streak reset).
    """
    if now is None:
        now = time.time()

    if state.last_check_at and (now - state.last_check_at) < MIN_POLL_INTERVAL_SECONDS:
        elapsed = int(now - state.last_check_at)
        return ShouldFireResult(
            ok=False,
            reason=REFUSE_TOO_SOON,
            detail=f"only {elapsed}s since last check (need {MIN_POLL_INTERVAL_SECONDS}s)",
        )

    if state.last_proposal_at and (now - state.last_proposal_at) < COOLDOWN_AFTER_PROPOSAL_SECONDS:
        elapsed = int(now - state.last_proposal_at)
        return ShouldFireResult(
            ok=False,
            reason=REFUSE_COOLDOWN,
            detail=(
                f"in cooldown ({elapsed}s of "
                f"{COOLDOWN_AFTER_PROPOSAL_SECONDS}s after last proposal)"
            ),
        )

    if state.null_streak >= MAX_NULL_STREAK:
        return ShouldFireResult(
            ok=False,
            reason=REFUSE_NULL_STREAK,
            detail=(
                f"null streak at {state.null_streak} (>= "
                f"{MAX_NULL_STREAK}); waiting for next user action"
            ),
        )

    cutoff = now - ACTIVITY_WINDOW_SECONDS
    recent = [t for t in state.recent_action_times if t >= cutoff]
    if len(recent) < MIN_RECENT_ACTIONS:
        return ShouldFireResult(
            ok=False,
            reason=REFUSE_TOO_QUIET,
            detail=(
                f"only {len(recent)} action(s) in trailing "
                f"{ACTIVITY_WINDOW_SECONDS}s (need {MIN_RECENT_ACTIONS})"
            ),
        )

    return ShouldFireResult(ok=True, detail=f"{len(recent)} recent action(s)")


# ============================================================
# Prompt builder
# ============================================================

PROACTIVE_SYSTEM_PROMPT = """\
You are a proactive review agent. The user did NOT summon you;
this is a periodic background check.

Your job: look at the recent state and decide whether there is a
GENUINE improvement worth proposing. The user is busy and will be
annoyed by low-quality suggestions. Returning `null` is the NORMAL
outcome — only propose something when you have specific,
non-trivial value to add.

Respond with a JSON object on a single line:

  {"patch": null, "reason": "<one short sentence>"}

OR

  {"patch": {"command": "<dispatcher command to run>"}, "reason": "<why>"}

Examples of GOOD non-null responses:
  - The user has been editing the same file 5 times; suggest a
    `note set working_memory <pattern observed>` to record the
    pattern for next time.
  - The user has the same long-running goal but `current_goal`
    block is stale; suggest updating it.

Examples of patches you should NEVER propose (return null instead):
  - Generic "you might want to..." suggestions with no concrete
    action.
  - Suggestions to run commands the user is already running.
  - Reminders about features the user clearly knows about.

Output only the JSON object; no surrounding prose. Do not wrap in
fenced code blocks. Be honest with `null` — the user will trust
your proposals more if they are rare and high-quality.
"""


def build_proactive_prompt(session_id: str = "default") -> List[Dict[str, str]]:
    """Build the system+user message list to send to the model
    when the heuristic fires. Phase 3a does NOT include the rich
    layered prompt assembly (control / persona / mentions etc.) —
    the proactive prompt is intentionally narrow so the model
    focuses on the question 'is there anything worth proposing'."""
    from app.memory import local_memory

    context = local_memory.get_session_context(session_id) or {}
    blocks = context.get("memory_blocks") or {}
    history = local_memory.get_history(session_id=session_id, limit=8) or []

    block_summary_lines: List[str] = []
    for label, block in sorted(blocks.items()):
        value = (block.get("value") or "").strip()
        if not value:
            continue
        preview = value.replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        block_summary_lines.append(f"- {label}: {preview}")
    block_summary = "\n".join(block_summary_lines) or "(no memory blocks set)"

    history_lines: List[str] = []
    for entry in history[-6:]:
        role = entry.get("role", "?")
        content = (entry.get("content") or "")
        if len(content) > 200:
            content = content[:197] + "..."
        history_lines.append(f"  [{role}] {content}")
    history_summary = "\n".join(history_lines) or "  (no recent turns)"

    user_payload = (
        f"Session: {session_id}\n"
        f"Mode: {context.get('mode', '?')}\n"
        f"Workspace: {context.get('workspace_path') or '(none)'}\n"
        f"Memory blocks:\n{block_summary}\n"
        f"Recent turns:\n{history_summary}\n\n"
        "Decide: is there a genuine improvement to propose? "
        "Reply with one JSON object as instructed."
    )

    return [
        {"role": "system", "content": PROACTIVE_SYSTEM_PROMPT},
        {"role": "user", "content": user_payload},
    ]


# ============================================================
# Response parser
# ============================================================

@dataclass
class ProactiveResponse:
    """Parsed response from the proactive lane."""

    raw_text: str
    is_null: bool
    reason: str = ""
    patch: Optional[Dict[str, Any]] = None  # the proposed action, or None
    parse_error: Optional[str] = None       # set when raw_text was malformed


# Recognise a fenced code block wrapping the JSON, so models that
# can't be perfectly trained out of markdown still parse correctly.
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)```")
_LOOSE_JSON_RE = re.compile(r"\{[\s\S]+\}")


def parse_proactive_response(text: str) -> ProactiveResponse:
    """Parse the model's reply into a ``ProactiveResponse``.

    Accepts:
    * ``{"patch": null, "reason": "..."}`` — the honest null case.
    * ``{"patch": {...}, "reason": "..."}`` — a real proposal.

    Tolerates:
    * Surrounding prose / fenced code blocks (we extract the first
      ``{...}`` substring that parses as JSON).
    * Missing ``reason`` (warn but accept).

    Rejects (sets ``parse_error``):
    * Non-JSON output.
    * JSON object missing the ``patch`` key entirely.
    """
    raw = (text or "").strip()
    if not raw:
        return ProactiveResponse(
            raw_text="", is_null=True, parse_error="empty_response",
        )

    # Strategy: try parsing the whole thing as JSON first; fall back
    # to fenced extraction; then loose-brace extraction.
    obj = None
    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            obj = parsed
            break

    if obj is None:
        return ProactiveResponse(
            raw_text=raw, is_null=True, parse_error="not_json",
        )

    if "patch" not in obj:
        return ProactiveResponse(
            raw_text=raw, is_null=True, parse_error="missing_patch_field",
        )

    reason = str(obj.get("reason") or "").strip()
    patch_value = obj.get("patch")

    if patch_value is None:
        return ProactiveResponse(
            raw_text=raw, is_null=True, reason=reason or "(no reason given)",
            patch=None,
        )

    if not isinstance(patch_value, dict):
        return ProactiveResponse(
            raw_text=raw, is_null=True,
            parse_error="patch_not_object",
        )

    return ProactiveResponse(
        raw_text=raw, is_null=False, reason=reason,
        patch=patch_value,
    )


def _candidates(raw: str):
    """Yield JSON-extraction candidates in priority order."""
    yield raw
    fenced = _FENCED_JSON_RE.search(raw)
    if fenced:
        yield fenced.group(1).strip()
    loose = _LOOSE_JSON_RE.search(raw)
    if loose:
        yield loose.group(0).strip()


# ============================================================
# State persistence
# ============================================================

def load_state(session_id: str = "default") -> ProactiveState:
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id) or {}
    return ProactiveState.from_dict(context.get("proactive"))


def save_state(session_id: str, state: ProactiveState) -> None:
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id)
    context["proactive"] = state.to_dict()
    local_memory.update_session_context(session_id=session_id, context=context)


def note_user_action(session_id: str = "default", *, now: Optional[float] = None) -> None:
    """Hook called by the dispatcher whenever the user issues a
    command. Adds ``now`` to ``recent_action_times`` (capped at
    ``MAX_RECENT_ACTION_HISTORY`` entries) and resets the null
    streak — a fresh user action means new context the model
    hasn't seen, so the "nothing to suggest" idle should clear."""
    if now is None:
        now = time.time()
    state = load_state(session_id)
    state.recent_action_times.append(float(now))
    if len(state.recent_action_times) > MAX_RECENT_ACTION_HISTORY:
        state.recent_action_times = state.recent_action_times[
            -MAX_RECENT_ACTION_HISTORY:
        ]
    if state.null_streak:
        state.null_streak = 0
    save_state(session_id, state)


def reset_state(session_id: str = "default") -> None:
    """Clear all proactive state. Used by ``proactive reset``."""
    save_state(session_id, ProactiveState())


# ============================================================
# Tick orchestrator
# ============================================================

#: A function that takes the runtime message list (list of
#: {role, content} dicts) and returns the model's reply text. The
#: default implementation routes through chat_providers; tests
#: inject a fake.
ModelResponder = Callable[[List[Dict[str, str]]], str]


def _default_model_responder(messages: List[Dict[str, str]]) -> str:
    """Default responder — calls the chat-providers infrastructure
    on the reasoning lane. The proactive prompt is small enough
    that even a 4-bit local model handles it; we don't strongly
    prefer a remote provider.

    Returns an empty string on any failure (network, no model
    installed, etc.) — the caller treats that as a parse_error.
    """
    try:
        from app.models import model_registry
        from app.services import chat_providers
    except Exception:  # noqa: BLE001
        return ""
    resolution = model_registry.resolve_model_for_role("reasoning")
    if not resolution.get("model"):
        return ""
    inference = chat_providers.dispatch_chat(
        "ollama", resolution["model"], messages=messages,
    )
    if not inference.get("ok"):
        return ""
    return str(inference.get("content") or "")


@dataclass
class TickResult:
    """Outcome of one ``tick`` call. Always populated even when the
    heuristic refused; ``fired`` distinguishes "we asked the model"
    from "we declined."""

    fired: bool                    # did we actually call the model?
    refused_reason: Optional[str] = None
    detail: str = ""
    response: Optional[ProactiveResponse] = None
    proposal_emitted: bool = False  # did we set a pending_approval?


def tick(
    session_id: str = "default",
    *,
    model_responder: Optional[ModelResponder] = None,
    now: Optional[float] = None,
) -> TickResult:
    """Run one proactive cycle for ``session_id``.

    1. Load state, check ``should_fire``. If refused, update
       ``last_check_at`` and return early.
    2. Build the proactive prompt and call ``model_responder`` (or
       the default LLM path).
    3. Parse the response. On null: increment ``null_streak`` and
       record the response. On non-null: emit the proposed
       command via the dispatcher (which goes through the existing
       Phase 1+2 approval gate).
    4. Save state, return a ``TickResult``.
    """
    if now is None:
        now = time.time()
    state = load_state(session_id)
    decision = should_fire(state, now=now)
    if not decision.ok:
        # Even when refused, update last_check_at so we don't
        # re-evaluate every dispatch_input until enough time passes.
        state.last_check_at = now
        save_state(session_id, state)
        return TickResult(
            fired=False, refused_reason=decision.reason,
            detail=decision.detail,
        )

    state.last_check_at = now
    messages = build_proactive_prompt(session_id)
    responder = model_responder or _default_model_responder
    raw = responder(messages)
    parsed = parse_proactive_response(raw)
    state.last_response = {
        "raw_text": parsed.raw_text[:500],
        "is_null": parsed.is_null,
        "reason": parsed.reason,
        "parse_error": parsed.parse_error,
    }

    if parsed.is_null or parsed.parse_error:
        state.null_streak += 1
        save_state(session_id, state)
        return TickResult(
            fired=True, response=parsed,
            detail=(
                f"null #{state.null_streak}: {parsed.reason or parsed.parse_error}"
            ),
        )

    # Non-null patch: emit it via the dispatcher. Phase 3a accepts
    # only the simplest shape ``{"command": "<text>"}`` — the
    # proactive agent suggests a dispatcher command (e.g. ``note set
    # working_memory ...``) and we run it. The dispatcher's existing
    # approval gate handles the rest. Future phases will support
    # richer payloads (typed memory_block_update, etc.).
    state.null_streak = 0
    state.last_proposal_at = now
    save_state(session_id, state)

    proposal_emitted = False
    command = (parsed.patch or {}).get("command") if parsed.patch else None
    if isinstance(command, str) and command.strip():
        from app.services.command_dispatcher import dispatch_input
        try:
            dispatch_input(command.strip(), session_id=session_id)
            proposal_emitted = True
        except Exception:  # noqa: BLE001
            # Dispatcher errors don't roll back state — the proposal
            # was made, the *execution* failed. Caller can inspect
            # last_response for the parsed reason.
            proposal_emitted = False

    return TickResult(
        fired=True, response=parsed,
        proposal_emitted=proposal_emitted,
        detail=parsed.reason or "(no reason)",
    )


# ============================================================
# Phase 3b: auto-scheduler (dispatch-driven)
# ============================================================
#
# Today ``tick`` is invoked manually via ``proactive tick``. Phase 3b
# adds an event-driven auto-fire: every user-initiated dispatch
# (excluding the meta-state ops in command_dispatcher.
# _PROACTIVE_HOOK_EXCLUDES) calls ``maybe_tick_async``, which checks
# the heuristic and — if it agrees — runs ``tick`` in a daemon
# thread. The user's command never waits for the model call.
#
# Two safety features:
# 1. Module-level ``AUTOFIRE_ENABLED`` defaults to False so tests
#    don't spawn background threads. Production code sets it True
#    on app start (or the user toggles it via ``proactive autofire
#    on``).
# 2. Per-session lock for coalescing — back-to-back dispatches don't
#    spawn N parallel ticks. The lock is released when the daemon
#    thread finishes.

#: Toggle at module level. Tests leave this False; production sets
#: True. Can also be flipped at runtime via ``proactive autofire``.
AUTOFIRE_ENABLED: bool = False

#: Per-session locks for coalescing. Each session has its own lock
#: so one session's slow tick doesn't block another's autofire.
_TICK_LOCKS: Dict[str, threading.Lock] = {}
_TICK_LOCKS_GUARD = threading.Lock()


def _tick_lock(session_id: str) -> threading.Lock:
    """Return (creating if needed) the per-session coalescing lock."""
    with _TICK_LOCKS_GUARD:
        lock = _TICK_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _TICK_LOCKS[session_id] = lock
        return lock


def maybe_tick_async(
    session_id: str = "default",
    *,
    model_responder: Optional[ModelResponder] = None,
) -> bool:
    """Non-blocking autofire entry point. Returns True iff a
    background tick was spawned. Returns False (without side
    effects beyond a possible ``last_check_at`` update inside
    ``tick``) when:

    * ``AUTOFIRE_ENABLED`` is False (the default — tests opt in by
      flipping it).
    * The heuristic refuses to fire (``should_fire(...).ok`` is
      False).
    * Another tick is already in progress for this session
      (coalescing).

    The daemon thread runs ``tick`` and releases the lock on exit;
    exceptions inside the thread are swallowed (the proactive layer
    is best-effort).
    """
    if not AUTOFIRE_ENABLED:
        return False

    state = load_state(session_id)
    if not should_fire(state).ok:
        return False

    lock = _tick_lock(session_id)
    if not lock.acquire(blocking=False):
        # Another tick is in progress; coalesce.
        return False

    def _runner():
        try:
            tick(session_id, model_responder=model_responder)
        except Exception:  # noqa: BLE001
            # Best-effort: a failure inside the daemon thread must
            # not crash anything. The state is what gets observed,
            # not exceptions.
            pass
        finally:
            lock.release()

    threading.Thread(
        target=_runner,
        daemon=True,
        name=f"proactive-autofire-{session_id}",
    ).start()
    return True


def set_autofire(enabled: bool) -> bool:
    """Runtime toggle for ``AUTOFIRE_ENABLED``. Returns the new value."""
    global AUTOFIRE_ENABLED
    AUTOFIRE_ENABLED = bool(enabled)
    return AUTOFIRE_ENABLED
