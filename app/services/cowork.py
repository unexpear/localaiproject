"""CoWork service — plan → approve → execute → synthesize state machine.

A higher-tier orchestration mode on top of the shipped planner/builder
+ goal-verbs primitives. See `docs/design/cowork-mode.md`.

Phase 1 ("manual cowork"): sub-tasks run serially via
`goals.dispatch_and_wait_for_existing`; no parallelism, no Qt UI, no
chat-flow auto-detection. File deliverables go through the existing
workspace-writes approval flow per sub-task.

Three public entry points:

  ``plan_run(session_id, goal, *, planner_call=None) -> dict``
      Asks the planner lane for a JSON plan, parses it, persists a
      cowork_run with status ``planning``, and queues a
      ``kind: "cowork_plan"`` pending approval.

  ``start_run_after_approval(run_id) -> dict``
      Called from ``_handle_approval_command`` when the user approves
      the plan. Flips status to ``approved``.

  ``execute_run(run_id, *, runner=None, cancel_event=None) -> dict``
      Walks the plan in topological order, dispatches each sub-task as
      a regular builder task, awaits terminal status, then runs the
      synthesis pass. Flips status to ``done`` / ``failed`` / ``cancelled``.

Tests inject a fake ``planner_call`` (for plan + synthesize) and a
fake ``runner`` (for sub-task execution); see
``tests/test_cowork.py``.
"""
from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from app.memory import local_memory
from app.services import error_vocabulary as ER
from app.services import goals
from app.services.gateway.bus import GatewayEvent, default_bus


logger = logging.getLogger(__name__)


# Bus event kind for cowork lifecycle. Re-exported from bus.py so the
# value is canonical there (matches all other KIND_* constants);
# kept here as an alias for backwards compat with anything importing
# from cowork.py directly.
from app.services.gateway.bus import KIND_COWORK


#: Allowed values for the optional plan-level ``branch_strategy`` field
#: (Phase 5, sandcastle-inspired). See cowork-mode.md.
VALID_BRANCH_STRATEGIES = frozenset({"head", "merge-to-head", "branch"})
DEFAULT_BRANCH_STRATEGY = "head"


# Regex captures the first ```json ... ``` fenced block in a planner
# response. Multi-line, lazy match for the body.
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


# Module-level cancel-event registry. Keyed by run_id so a `cowork
# cancel` from one thread can signal an `execute_run` running in
# another. Survives only the duration of the call (popped at end).
#
# Two parallel registries — same lock guards both:
#  * ``_cancel_events`` is the legacy ``threading.Event`` view.
#    Existing call sites (sub-task pre-work / pre-queue / synthesis
#    final check) read this directly via ``cancel_event.is_set()``.
#  * ``_cancel_tokens`` is the typed ``CancelToken`` view (adopted
#    from `local_ai_workbench/shared/cancel.py`). The token wraps
#    the SAME event (via ``cancel.from_event``) so signalling either
#    side is symmetric — but the token additionally captures a
#    *reason* (`user-requested` / `dependency_failed` / etc.) so
#    consumers can pattern-match without re-deriving it.
_cancel_events: Dict[str, threading.Event] = {}
_cancel_tokens: Dict[str, "Any"] = {}  # forward-ref to avoid import cycle
_cancel_lock = threading.Lock()

#: Default cap on concurrent sub-task workers. Each worker runs ONE
#: sub-task at a time; per-builder locks (built inside `execute_run`)
#: serialise sub-tasks targeting the same builder, so the effective
#: parallelism is min(max_workers, distinct builders in plan).
DEFAULT_MAX_WORKERS = 4

#: Hard cap regardless of caller request. We don't want a malicious or
#: buggy plan to spawn 50 threads.
MAX_WORKERS_CAP = 8


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class CoworkPlanError(ValueError):
    """Raised when the planner's plan response can't be parsed/validated.

    Carries a ``reason`` string from the canonical error vocabulary so
    callers can pattern-match without inspecting the message text.
    """

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Plan parsing
# --------------------------------------------------------------------------

def parse_plan_response(response_text: str) -> Dict[str, Any]:
    """Extract + validate a JSON cowork plan from a planner response.

    Returns a dict ``{goal, sub_tasks: [...]}`` where each sub-task
    has been validated for required keys and dependency closure.
    Raises :class:`CoworkPlanError` with a canonical reason on any
    failure path.
    """
    if not response_text:
        raise CoworkPlanError(ER.INVALID_PLAN_JSON, "empty planner response")

    match = _JSON_FENCE_RE.search(response_text)
    if not match:
        raise CoworkPlanError(
            ER.INVALID_PLAN_JSON,
            "no ```json``` fenced block found in planner response",
        )

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CoworkPlanError(
            ER.INVALID_PLAN_JSON, f"plan JSON failed to parse: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise CoworkPlanError(ER.INVALID_PLAN_JSON, "plan must be a JSON object")

    sub_tasks_raw = data.get("sub_tasks")
    if not isinstance(sub_tasks_raw, list) or not sub_tasks_raw:
        raise CoworkPlanError(ER.EMPTY_PLAN, "plan has no sub_tasks")
    if len(sub_tasks_raw) > local_memory.MAX_SUBTASKS:
        raise CoworkPlanError(
            ER.TOO_MANY_SUBTASKS,
            f"plan has {len(sub_tasks_raw)} sub-tasks (max {local_memory.MAX_SUBTASKS})",
        )

    seen_ids: set[str] = set()
    sub_tasks: List[Dict[str, Any]] = []
    for raw in sub_tasks_raw:
        if not isinstance(raw, dict):
            raise CoworkPlanError(ER.INVALID_PLAN_JSON, "sub_task must be a JSON object")
        sub_id = str(raw.get("id") or "").strip()
        task_text = str(raw.get("task") or "").strip()
        builder = str(raw.get("builder") or "").strip()
        if not sub_id or not task_text or not builder:
            raise CoworkPlanError(
                ER.INVALID_PLAN_JSON,
                "each sub_task needs id, task, and builder",
            )
        if sub_id in seen_ids:
            raise CoworkPlanError(
                ER.INVALID_PLAN_JSON, f"duplicate sub_task id {sub_id!r}"
            )
        seen_ids.add(sub_id)
        depends_on = []
        for dep in raw.get("depends_on") or []:
            depends_on.append(str(dep or "").strip())
        sub_tasks.append(
            {
                "id": sub_id,
                "task": task_text,
                "builder": builder,
                "depends_on": [d for d in depends_on if d],
                "acceptance": str(raw.get("acceptance") or "").strip(),
                # Phase 5 lifecycle hooks. Either may be empty.
                "before": str(raw.get("before") or "").strip(),
                "after": str(raw.get("after") or "").strip(),
            }
        )

    # Dependency closure check: every depends_on must reference a known id.
    for sub in sub_tasks:
        for dep in sub["depends_on"]:
            if dep not in seen_ids:
                raise CoworkPlanError(
                    ER.UNKNOWN_DEPENDENCY,
                    f"sub_task {sub['id']!r} depends on unknown {dep!r}",
                )

    # Detect cycles via Kahn's algorithm. If we can't drain every node,
    # there's a cycle.
    incoming = {s["id"]: list(s["depends_on"]) for s in sub_tasks}
    queue = [sid for sid, deps in incoming.items() if not deps]
    drained = []
    while queue:
        node = queue.pop(0)
        drained.append(node)
        for sid, deps in incoming.items():
            if node in deps:
                deps.remove(node)
                if not deps and sid not in drained and sid not in queue:
                    queue.append(sid)
    if len(drained) != len(sub_tasks):
        raise CoworkPlanError(
            ER.CYCLE_IN_PLAN,
            "plan dependency graph contains a cycle",
        )

    # Phase 5 plan-level fields: branch strategy + optional branch name.
    raw_strategy = str(data.get("branch_strategy") or DEFAULT_BRANCH_STRATEGY).strip().lower()
    if raw_strategy not in VALID_BRANCH_STRATEGIES:
        raise CoworkPlanError(
            ER.INVALID_PLAN_JSON,
            f"branch_strategy {raw_strategy!r} not in "
            f"{sorted(VALID_BRANCH_STRATEGIES)}",
        )
    branch_name = str(data.get("branch_name") or "").strip()
    if raw_strategy == "branch" and not branch_name:
        raise CoworkPlanError(
            ER.INVALID_PLAN_JSON,
            "branch_strategy=branch requires a branch_name",
        )

    return {
        "goal": str(data.get("goal") or "").strip(),
        "sub_tasks": sub_tasks,
        "branch_strategy": raw_strategy,
        "branch_name": branch_name,
    }


def topological_order(plan: List[Dict[str, Any]]) -> List[str]:
    """Return sub_task ids in a dependency-respecting order.

    Stable: ties are broken by the order sub-tasks appear in the plan
    list. Assumes the plan has already been validated (no cycles).
    """
    incoming = {s["id"]: list(s["depends_on"]) for s in plan}
    by_id = {s["id"]: s for s in plan}
    order: List[str] = []
    remaining = list(s["id"] for s in plan)
    while remaining:
        ready = [sid for sid in remaining if not incoming[sid]]
        if not ready:
            # Should never happen on a validated plan.
            break
        picked = ready[0]  # stable: pick first in plan order
        remaining.remove(picked)
        order.append(picked)
        for sid in remaining:
            if picked in incoming[sid]:
                incoming[sid].remove(picked)
    return order


# --------------------------------------------------------------------------
# Planner / synthesis prompts
# --------------------------------------------------------------------------

def build_planner_prompt(goal: str, available_builders: List[str]) -> str:
    """Construct the prompt asking the planner for a JSON plan."""
    builders_list = ", ".join(repr(b) for b in available_builders) or "(none linked)"
    return (
        "You are the planner for a CoWork run. Decompose the goal into "
        f"{local_memory.MAX_SUBTASKS} or fewer concrete sub-tasks, each "
        "assignable to one of the linked builders.\n\n"
        f"Goal: {goal}\n"
        f"Linked builders available: {builders_list}\n\n"
        "Output a JSON plan inside a ```json fenced block with this shape:\n\n"
        "```json\n"
        "{\n"
        '  "goal": "<echo the goal>",\n'
        '  "sub_tasks": [\n'
        "    {\n"
        '      "id": "sub_1",\n'
        '      "task": "<one imperative line>",\n'
        '      "builder": "<one of the linked builder ids>",\n'
        '      "depends_on": [],\n'
        '      "acceptance": "<how the builder knows it is done>"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "Rules:\n"
        f"- At most {local_memory.MAX_SUBTASKS} sub-tasks.\n"
        "- `id` values must be unique within the plan.\n"
        "- `depends_on` may only reference sub_task ids that appear earlier in the plan.\n"
        "- Keep `task` to one short imperative line.\n"
        "- Output ONLY the fenced JSON block — no other commentary."
    )


def build_synthesis_prompt(
    goal: str,
    plan: List[Dict[str, Any]],
    subtask_results: Dict[str, Dict[str, Any]],
) -> str:
    """Construct the synthesis prompt summarising what each sub-task did."""
    lines = [
        "Synthesise the result of a CoWork run. Original goal:",
        f"  {goal}",
        "",
        "Per-sub-task outcomes:",
    ]
    for sub in plan:
        sub_id = sub["id"]
        result = subtask_results.get(sub_id) or {}
        status = result.get("status", "(no result)")
        body = result.get("result") or "(no output)"
        if isinstance(body, str) and len(body) > 400:
            body = body[:397] + "..."
        lines.append(
            f"- {sub_id} ({sub['builder']}, {status}): {sub['task']}"
        )
        lines.append(f"    output: {body}")
    lines.extend(
        [
            "",
            "Write a polished plain-English summary covering:",
            "1. What was accomplished overall.",
            "2. Any sub-tasks that failed, with a brief root-cause reading.",
            "3. Suggested follow-ups the user should consider next.",
            "",
            "Be honest about partial successes — don't paper over failures.",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Bus event helpers
# --------------------------------------------------------------------------

def _publish_cowork_event(event_name: str, run_id: str, session_id: str, **payload):
    """Best-effort publish of a `KIND_COWORK` event."""
    try:
        default_bus().publish(
            GatewayEvent(
                kind=KIND_COWORK,
                session_id=session_id,
                payload={"event": event_name, "run_id": run_id, **payload},
            )
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Plan / approve / execute entry points
# --------------------------------------------------------------------------

def _default_planner_call(prompt: str, session_id: str) -> Dict[str, Any]:
    """Default planner call: dispatch through chat_service.

    Lazy import avoids the circular dependency with command_dispatcher.
    Returns the chat result dict; caller pulls `.response` for plan text.
    """
    from app.services.chat_service import handle_chat
    return handle_chat(prompt, session_id=session_id)


def plan_run(
    session_id: str,
    goal: str,
    *,
    planner_call: Optional[Callable[[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Ask the planner for a plan; persist a cowork run; queue approval.

    Returns ``{ok: bool, run: {...}, pending_action: {...}, reason: ...,
    output: ...}``. On failure the ``reason`` is a canonical error tag;
    callers (the dispatcher handler) translate it to a user-facing
    message.
    """
    if not goal or not str(goal).strip():
        return {"ok": False, "reason": ER.INVALID_ARGUMENT, "output": "Cowork goal is empty."}

    if local_memory.active_cowork_run(session_id) is not None:
        return {
            "ok": False,
            "reason": ER.COWORK_IN_PROGRESS,
            "output": "This session already has an active cowork run.",
        }

    builders = local_memory.get_builders(session_id)
    if not builders:
        return {
            "ok": False,
            "reason": ER.NO_LINK,
            "output": (
                "Cowork needs at least one linked builder. "
                "Use `link <builder>` first."
            ),
        }

    planner = planner_call or _default_planner_call
    prompt = build_planner_prompt(goal, builders)
    last_error: Optional[CoworkPlanError] = None
    plan_result: Optional[Dict[str, Any]] = None

    # One retry: if the planner's first response doesn't parse, append
    # a "your last plan was malformed" hint and try once more.
    for attempt in range(2):
        attempt_prompt = prompt
        if attempt == 1 and last_error is not None:
            attempt_prompt = (
                prompt
                + "\n\n(Your previous response could not be parsed: "
                + str(last_error)
                + ". Please respond with ONLY the JSON fenced block.)"
            )
        chat_result = planner(attempt_prompt, session_id)
        response_text = (chat_result or {}).get("response") or ""
        try:
            plan_result = parse_plan_response(response_text)
            break
        except CoworkPlanError as exc:
            last_error = exc
            continue

    if plan_result is None:
        return {
            "ok": False,
            "reason": last_error.reason if last_error else ER.INVALID_PLAN_JSON,
            "output": (
                "Planner did not produce a valid plan after one retry: "
                f"{last_error}"
            ),
        }

    # Validate that every sub_task's builder is actually linked.
    builder_set = set(builders)
    for sub in plan_result["sub_tasks"]:
        if sub["builder"] not in builder_set:
            return {
                "ok": False,
                "reason": ER.UNKNOWN_BUILDER,
                "output": (
                    f"Plan assigns sub_task {sub['id']} to builder "
                    f"{sub['builder']!r}, which isn't linked. "
                    f"Linked: {sorted(builder_set)}."
                ),
            }

    run = local_memory.add_cowork_run(
        session_id=session_id,
        goal=plan_result["goal"] or goal,
        plan=plan_result["sub_tasks"],
        branch_strategy=plan_result.get("branch_strategy", DEFAULT_BRANCH_STRATEGY),
        branch_name=plan_result.get("branch_name", ""),
    )

    # Queue the plan approval through the existing pending-approval gate.
    pending_action = {
        "kind": "cowork_plan",
        "origin": "command",
        "run_id": run["id"],
    }
    local_memory.set_pending_approval(session_id=session_id, pending_approval=pending_action)

    _publish_cowork_event(
        "planned",
        run_id=run["id"],
        session_id=session_id,
        goal=run["goal"],
        subtask_count=len(run["plan"]),
    )

    return {
        "ok": True,
        "run": run,
        "pending_action": pending_action,
    }


def start_run_after_approval(run_id: str) -> Dict[str, Any]:
    """Flip a run from `planning` to `approved`. Called by the dispatcher's
    approve handler when the pending action is a cowork_plan kind."""
    run = local_memory.get_cowork_run(run_id)
    if run is None:
        return {"ok": False, "reason": ER.UNKNOWN_TARGET, "output": "Unknown cowork run."}
    if run["status"] != "planning":
        return {
            "ok": False,
            "reason": ER.INVALID_STATE,
            "output": f"Cowork run {run_id} is in state '{run['status']}', not 'planning'.",
        }
    updated = local_memory.update_cowork_run(
        run_id,
        status="approved",
        approved_at=local_memory._now(),
    )
    _publish_cowork_event("approved", run_id=run_id, session_id=run["session_id"])
    return {"ok": True, "run": updated}


def cancel_run_after_deny(run_id: str) -> Dict[str, Any]:
    """Mark a cowork run as cancelled. Called by the dispatcher's deny
    handler when the pending action is a cowork_plan kind."""
    run = local_memory.get_cowork_run(run_id)
    if run is None:
        return {"ok": False, "reason": ER.UNKNOWN_TARGET}
    if run["status"] not in {"planning", "approved"}:
        return {"ok": False, "reason": ER.INVALID_STATE}
    updated = local_memory.update_cowork_run(
        run_id,
        status="cancelled",
        completed_at=local_memory._now(),
    )
    _publish_cowork_event("cancelled", run_id=run_id, session_id=run["session_id"])
    return {"ok": True, "run": updated}


def execute_run(
    run_id: str,
    *,
    runner: Optional[Callable] = None,
    planner_call: Optional[Callable] = None,
    cancel_event: Optional[threading.Event] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> Dict[str, Any]:
    """Walk an approved plan, dispatching independent sub-tasks in parallel,
    then synthesise the final report.

    A sub-task becomes "ready" once every entry in its ``depends_on`` is
    terminal (done / failed / skipped). Up to ``max_workers`` ready
    sub-tasks run concurrently in a thread pool; per-builder locks
    serialise sub-tasks that target the same builder (one inbox each).

    Pass ``max_workers=1`` to force serial execution (Phase-1-equivalent
    behaviour). The hard ceiling is ``MAX_WORKERS_CAP``.

    All other guarantees from the serial implementation hold:
      - Failure isolation: a failed/skipped sub-task auto-skips its
        downstream dependents with reason ``dependency_failed``.
      - Cancellation: ``cancel_event`` stops new sub-task submission;
        in-flight sub-tasks see the same event via
        ``goals.dispatch_and_wait_for_existing``.
      - Synthesis runs once every sub-task is terminal.
    """
    run = local_memory.get_cowork_run(run_id)
    if run is None:
        return {"ok": False, "reason": ER.UNKNOWN_TARGET, "output": "Unknown cowork run."}
    if run["status"] != "approved":
        return {
            "ok": False,
            "reason": ER.INVALID_STATE,
            "output": f"Cowork run {run_id} is in state '{run['status']}', not 'approved'.",
        }

    session_id = run["session_id"]
    plan = run["plan"]
    sub_by_id = {sub["id"]: sub for sub in plan}

    # Register a cancel event for this run so `cowork cancel` can signal
    # us from another thread. If the caller didn't supply one, create a
    # fresh event; either way, store it under the run_id.
    own_event = False
    if cancel_event is None:
        cancel_event = threading.Event()
        own_event = True
    # Build a CancelToken that wraps the same event so
    # `cancel_active_run` / `cancel_run_by_id` can capture a reason
    # while existing event-polling code keeps working unchanged.
    from app.services import cancel as _cancel
    cancel_token = _cancel.from_event(cancel_event, label=run_id)
    with _cancel_lock:
        _cancel_events[run_id] = cancel_event
        _cancel_tokens[run_id] = cancel_token

    # Clamp worker count: at least 1, at most MAX_WORKERS_CAP.
    workers = max(1, min(int(max_workers or 1), MAX_WORKERS_CAP))

    # Phase 5 branch strategy. Returns a context dict the loop will
    # finalise at run end; safe to call even on `head` strategy
    # (returns a no-op context). Side-effects: may check out a new
    # branch + subscribe to KIND_WORKSPACE_WRITE for auto-commit.
    branch_ctx = _setup_branch_strategy(run, run_id, session_id)
    if branch_ctx.get("error"):
        return {
            "ok": False,
            "reason": ER.INTERNAL_ERROR,
            "output": f"branch setup failed: {branch_ctx['error']}",
        }

    local_memory.update_cowork_run(run_id, status="executing")

    # Shared mutable state — every read/write is guarded by `state_lock`.
    state_lock = threading.Lock()
    subtask_results: Dict[str, Dict[str, Any]] = {}
    terminal_subs: set = set()           # sub_ids that have reached a terminal state
    failed_or_skipped: set = set()        # subset of terminal_subs whose status != "done"

    # One lock per builder so two sub-tasks targeting the same builder
    # can't double-dispatch its inbox. Created lazily as we encounter
    # builder names; access is guarded by `builder_locks_lock`.
    builder_locks: Dict[str, threading.Lock] = {}
    builder_locks_lock = threading.Lock()

    def _builder_lock_for(builder_id: str) -> threading.Lock:
        with builder_locks_lock:
            lock = builder_locks.get(builder_id)
            if lock is None:
                lock = threading.Lock()
                builder_locks[builder_id] = lock
            return lock

    def _record_terminal(sub_id: str, result: Dict[str, Any]) -> None:
        """Atomic 'sub-task is now terminal' bookkeeping."""
        with state_lock:
            subtask_results[sub_id] = result
            terminal_subs.add(sub_id)
            if result.get("status") != "done":
                failed_or_skipped.add(sub_id)

    def _run_hook(sub_id: str, hook_kind: str, cmd: str) -> Optional[Dict[str, Any]]:
        """Dispatch a `before` / `after` hook through the dispatcher.

        Returns None on success (no `reason` on the resulting event)
        or the offending event dict on failure. The caller decides
        whether the failure aborts the sub-task (only `before` hooks
        have abort semantics; `after` failures are logged but the
        sub-task is already done).
        """
        if not cmd:
            return None
        # Lazy import to dodge the cowork ↔ command_dispatcher cycle.
        from app.services.command_dispatcher import dispatch_input
        try:
            event = dispatch_input(cmd, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cowork %s sub-task %s %s hook raised: %s",
                run_id, sub_id, hook_kind, exc,
            )
            return {
                "action": hook_kind,
                "session_id": session_id,
                "output": str(exc),
                "reason": "internal_error",
            }
        if event.get("reason"):
            logger.info(
                "Cowork %s sub-task %s %s hook returned reason=%s",
                run_id, sub_id, hook_kind, event.get("reason"),
            )
            return event
        return None

    def _execute_one(sub_id: str) -> None:
        """Run one sub-task end-to-end. Catches exceptions so they don't
        bubble up to the executor thread (we want the run to keep going
        even when one sub-task explodes)."""
        try:
            sub = sub_by_id[sub_id]

            # Re-check dependency failure under the state lock — by the
            # time we run, an upstream sub-task may have failed.
            with state_lock:
                dep_failed = any(
                    dep in failed_or_skipped for dep in sub["depends_on"]
                )

            if dep_failed:
                result = {
                    "status": "skipped",
                    "result": "skipped — upstream dependency failed",
                }
                _record_terminal(sub_id, result)
                local_memory.record_cowork_subtask_result(run_id, sub_id, result)
                _publish_cowork_event(
                    "subtask_failed",
                    run_id=run_id,
                    session_id=session_id,
                    subtask_id=sub_id,
                    error="dependency_failed",
                )
                return

            if cancel_event.is_set():
                result = {"status": "skipped", "result": "skipped — run cancelled"}
                _record_terminal(sub_id, result)
                local_memory.record_cowork_subtask_result(run_id, sub_id, result)
                return

            # Phase 5 lifecycle: `before` hook gates the sub-task. A
            # non-empty `reason` from the hook fails the sub-task with
            # `precondition_failed`; downstream dependents auto-skip.
            before_failure = _run_hook(sub_id, "before", sub.get("before") or "")
            if before_failure is not None:
                preview = before_failure.get("output") or "before hook failed"
                preview = preview if len(preview) <= 240 else preview[:237] + "..."
                result = {
                    "status": "failed",
                    "result": f"precondition_failed: {preview}",
                    "hook_reason": before_failure.get("reason"),
                }
                _record_terminal(sub_id, result)
                local_memory.record_cowork_subtask_result(run_id, sub_id, result)
                _publish_cowork_event(
                    "subtask_failed",
                    run_id=run_id,
                    session_id=session_id,
                    subtask_id=sub_id,
                    error="precondition_failed",
                )
                return

            task_text = sub["task"]
            if sub["acceptance"]:
                task_text = f"{task_text}\n\nAcceptance: {sub['acceptance']}"

            # Per-builder serialisation: queue + drain happen under the
            # builder's lock so two sub-tasks aren't fighting over the
            # same inbox.
            with _builder_lock_for(sub["builder"]):
                if cancel_event.is_set():
                    result = {"status": "skipped", "result": "skipped — run cancelled"}
                    _record_terminal(sub_id, result)
                    local_memory.record_cowork_subtask_result(run_id, sub_id, result)
                    return

                try:
                    queued = local_memory.add_task(
                        session_id, task_text, builder=sub["builder"]
                    )
                except ValueError as exc:
                    result = {"status": "failed", "result": str(exc)}
                    _record_terminal(sub_id, result)
                    local_memory.record_cowork_subtask_result(run_id, sub_id, result)
                    _publish_cowork_event(
                        "subtask_failed",
                        run_id=run_id,
                        session_id=session_id,
                        subtask_id=sub_id,
                        error=str(exc),
                    )
                    return

                local_memory.append_cowork_task_id(run_id, queued["id"])

                final = goals.dispatch_and_wait_for_existing(
                    queued, runner=runner, cancel_event=cancel_event
                )

            final = final or queued
            status = final.get("status", "failed")
            preview = final.get("result")
            result = {"status": status, "result": preview}
            _record_terminal(sub_id, result)
            local_memory.record_cowork_subtask_result(run_id, sub_id, result)

            if status == "done":
                # Phase 5 lifecycle: `after` hook runs once the
                # sub-task itself succeeded. A failing `after` hook is
                # logged but does NOT flip the sub-task to failed —
                # the work was already done; the after-hook is a
                # separate cleanup/verification ritual whose outcome
                # we surface in the result dict but not in the
                # status. (If the user wants a strict gate they make
                # the verification a separate sub-task.)
                after_failure = _run_hook(sub_id, "after", sub.get("after") or "")
                if after_failure is not None:
                    after_preview = (after_failure.get("output") or "after hook failed")
                    after_preview = (
                        after_preview if len(after_preview) <= 200
                        else after_preview[:197] + "..."
                    )
                    result["after_hook_warning"] = after_preview
                    # Re-record so consumers can see the warning.
                    local_memory.record_cowork_subtask_result(run_id, sub_id, result)
                _publish_cowork_event(
                    "subtask_done",
                    run_id=run_id,
                    session_id=session_id,
                    subtask_id=sub_id,
                    task_id=queued["id"],
                    result_preview=(str(preview) or "")[:200],
                )
            else:
                _publish_cowork_event(
                    "subtask_failed",
                    run_id=run_id,
                    session_id=session_id,
                    subtask_id=sub_id,
                    task_id=queued["id"],
                    error=str(preview),
                )
        except Exception as exc:  # noqa: BLE001
            # A worker exception must not bring down the run. Mark this
            # sub-task failed and let the rest continue.
            logger.exception("Cowork sub-task %s raised", sub_id)
            result = {"status": "failed", "result": f"worker exception: {exc}"}
            _record_terminal(sub_id, result)
            local_memory.record_cowork_subtask_result(run_id, sub_id, result)
            _publish_cowork_event(
                "subtask_failed",
                run_id=run_id,
                session_id=session_id,
                subtask_id=sub_id,
                error=str(exc),
            )

    try:
        # Driver loop: keep submitting ready sub-tasks until everything
        # is terminal (or cancelled). Uses a small thread pool with a
        # per-iteration FIRST_COMPLETED wait so we react quickly when
        # any sub-task finishes (and unblocks downstream).
        pending_ids = collections.deque(s["id"] for s in plan)
        in_flight: Dict[concurrent.futures.Future, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            while pending_ids or in_flight:
                if cancel_event.is_set():
                    # Don't submit anything new; drain in-flight by
                    # waiting (they observe the cancel_event themselves).
                    pending_ids.clear()

                # Identify ready sub-tasks (all deps terminal) and submit.
                still_pending = collections.deque()
                with state_lock:
                    snapshot = set(terminal_subs)
                while pending_ids:
                    sid = pending_ids.popleft()
                    sub = sub_by_id[sid]
                    if all(dep in snapshot for dep in sub["depends_on"]):
                        fut = pool.submit(_execute_one, sid)
                        in_flight[fut] = sid
                    else:
                        still_pending.append(sid)
                pending_ids = still_pending

                if not in_flight:
                    # Should be impossible on a validated plan: nothing
                    # in flight + nothing ready means the dep graph is
                    # impossible to satisfy. Bail to avoid hanging.
                    if pending_ids:
                        logger.warning(
                            "Cowork run %s has unsatisfiable deps; bailing.",
                            run_id,
                        )
                    break

                # Wait for at least one to finish (timeout lets us
                # re-check cancel_event periodically).
                done, _not_done = concurrent.futures.wait(
                    list(in_flight.keys()),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                    timeout=0.25,
                )
                for fut in done:
                    sid = in_flight.pop(fut)
                    # _execute_one swallows its own exceptions; this
                    # `result()` only re-raises in the rare case of a
                    # bug above the try/except. Log and move on.
                    try:
                        fut.result()
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Unexpected exception bubbled past _execute_one(%s)", sid
                        )

        # Cancellation path: storage flipped to "cancelled" and we exit
        # without running synthesis (nothing useful to summarise).
        if cancel_event.is_set():
            # Cancellation: leave any branch in place for the user to
            # inspect; the partial commits stay on the run branch.
            _finalise_branch_strategy(branch_ctx, run_id, success=False)
            local_memory.update_cowork_run(
                run_id,
                status="cancelled",
                completed_at=local_memory._now(),
                subtask_results=subtask_results,
            )
            _publish_cowork_event("cancelled", run_id=run_id, session_id=session_id)
            return local_memory.get_cowork_run(run_id)

        # Synthesis runs even if some sub-tasks failed; the prompt is
        # told what failed and produces an honest summary.
        local_memory.update_cowork_run(run_id, status="synthesizing")
        synthesis_text = _synthesize(
            run_id=run_id,
            session_id=session_id,
            goal=run["goal"],
            plan=plan,
            subtask_results=subtask_results,
            planner_call=planner_call,
        )

        with state_lock:
            any_failed = bool(failed_or_skipped)
        final_status = "failed" if any_failed else "done"

        # Phase 5: finalise the branch strategy. For `merge-to-head`
        # this attempts to merge the run branch back; on any sub-task
        # failure we leave the branch checked out for inspection.
        _finalise_branch_strategy(
            branch_ctx, run_id, success=(not any_failed),
        )

        local_memory.update_cowork_run(
            run_id,
            status=final_status,
            completed_at=local_memory._now(),
            synthesis=synthesis_text,
            subtask_results=subtask_results,
        )
        _publish_cowork_event(
            "completed",
            run_id=run_id,
            session_id=session_id,
            status=final_status,
            summary_preview=(synthesis_text or "")[:200],
        )
        return local_memory.get_cowork_run(run_id)

    finally:
        # Always tear down branch resources (bus subscription, etc.)
        # even if execution exploded mid-flight.
        try:
            _teardown_branch_strategy(branch_ctx)
        except Exception:  # noqa: BLE001
            pass
        if own_event:
            with _cancel_lock:
                _cancel_events.pop(run_id, None)
                _cancel_tokens.pop(run_id, None)


def _synthesize(
    *,
    run_id: str,
    session_id: str,
    goal: str,
    plan: List[Dict[str, Any]],
    subtask_results: Dict[str, Dict[str, Any]],
    planner_call: Optional[Callable] = None,
) -> str:
    """Produce the final synthesis text. Errors are reported in the text
    rather than raised — synthesis must always produce SOMETHING so the
    user sees what happened."""
    planner = planner_call or _default_planner_call
    prompt = build_synthesis_prompt(goal, plan, subtask_results)
    try:
        result = planner(prompt, session_id)
        return (result or {}).get("response") or "(synthesis returned no response)"
    except Exception as exc:  # noqa: BLE001
        logger.warning("CoWork synthesis failed: %s", exc)
        return f"(synthesis failed: {exc})"


# ============================================================
# Phase 5 — branch strategy helpers
# ============================================================

def _setup_branch_strategy(
    run: Dict[str, Any], run_id: str, session_id: str,
) -> Dict[str, Any]:
    """Detect git, create the run branch (if non-`head` strategy),
    and subscribe to ``KIND_WORKSPACE_WRITE`` for auto-commit.

    Returns a context dict with everything ``_finalise_branch_strategy``
    + ``_teardown_branch_strategy`` need. On `head` strategy or when
    git isn't available, returns an inert context (no side-effects).
    """
    strategy = (run.get("branch_strategy") or "head").lower()
    ctx: Dict[str, Any] = {
        "strategy": strategy,
        "active": False,
        "workspace": None,
        "original_branch": None,
        "target_branch": None,
        "subscription_id": None,
        "commits": [],
        "error": None,
    }

    if strategy == "head":
        return ctx

    workspace = local_memory.get_session_context(session_id).get("workspace_path")
    if not workspace:
        ctx["error"] = "no workspace pinned (branch strategy needs git)"
        return ctx

    # Pre-flight: scan stored state for cross-workspace path leakage
    # before kicking off git operations. A leak here usually means
    # the user repinned the workspace and stale write_backups /
    # approved_search are still pointing at the old root — running a
    # branch strategy in that state can scribble into the wrong tree.
    # See `docs/design/workspace-writes.md` Phase 3.
    from app.services import workspace_isolation
    audit = workspace_isolation.audit_session(session_id=session_id)
    if audit.errors:
        sample_paths = ", ".join(
            sorted({f.path for f in audit.errors[:3]})
        )
        ctx["error"] = (
            f"workspace audit found {len(audit.errors)} cross-workspace "
            f"path(s) in stored state (e.g. {sample_paths!r}); run "
            "`workspace audit` to inspect, then `workspace clear` + re-pin "
            "before starting a branch-strategy run"
        )
        return ctx

    from app.services import cowork_git as git
    if not git.is_git_repo(workspace):
        ctx["error"] = f"workspace {workspace!r} is not a git repository"
        return ctx

    if git.has_uncommitted_changes(workspace):
        ctx["error"] = (
            "workspace has uncommitted changes; commit or stash first "
            "(branch strategy refuses to run on a dirty tree)"
        )
        return ctx

    original = git.current_branch(workspace)
    if original is None:
        ctx["error"] = "could not determine current branch (detached HEAD?)"
        return ctx

    if strategy == "branch":
        target = (run.get("branch_name") or "").strip()
        if not target:
            ctx["error"] = "branch_strategy=branch requires a branch_name"
            return ctx
    else:  # merge-to-head
        target = git.auto_branch_name(session_id, run_id)

    if git.branch_exists(workspace, target):
        co = git.checkout_branch(workspace, target)
        if not co.ok:
            ctx["error"] = f"checkout {target!r} failed: {co.stderr.strip()}"
            return ctx
    else:
        cb = git.create_and_checkout_branch(workspace, target)
        if not cb.ok:
            ctx["error"] = f"create branch {target!r} failed: {cb.stderr.strip()}"
            return ctx

    # Subscribe to KIND_WORKSPACE_WRITE for THIS session and auto-commit
    # each approved write/delete. Bus-driven so we don't have to thread
    # state into the workspace_writes path.
    from app.services.gateway.bus import KIND_WORKSPACE_WRITE, default_bus

    def _on_write_event(event):
        try:
            if event.kind != KIND_WORKSPACE_WRITE:
                return
            if event.session_id != session_id:
                return
            payload = event.payload or {}
            path = payload.get("path")
            if not path:
                return
            kind = payload.get("kind", "write")
            sub_id = payload.get("subtask_id") or "anon"
            commit_msg = (
                f"{run_id}: {kind} {path}\n\n"
                f"(cowork branch={target} session={session_id})"
            )
            commit_result = git.commit_path(workspace, path, commit_msg)
            ctx["commits"].append({
                "path": path,
                "kind": kind,
                "ok": commit_result.ok,
                "stderr": commit_result.stderr.strip()[:200],
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("Branch-strategy commit hook raised: %s", exc)

    sub_id = default_bus().subscribe(default_bus().ANY, _on_write_event)

    ctx.update({
        "active": True,
        "workspace": workspace,
        "original_branch": original,
        "target_branch": target,
        "subscription_id": sub_id,
    })
    # Persist initial git_state so consumers can see what branch we're on.
    local_memory.update_cowork_run(
        run_id,
        git_state={
            "strategy": strategy,
            "original_branch": original,
            "target_branch": target,
            "commits": [],
            "merge_status": None,
        },
    )
    return ctx


def _finalise_branch_strategy(ctx: Dict[str, Any], run_id: str, *, success: bool) -> None:
    """Per-strategy run-end actions. Idempotent — safe to call twice."""
    if not ctx.get("active"):
        # Always persist the (possibly empty) commit log so consumers
        # see how many writes were captured.
        if ctx.get("strategy") and ctx["strategy"] != "head":
            try:
                local_memory.update_cowork_run(
                    run_id,
                    git_state={
                        "strategy": ctx.get("strategy"),
                        "original_branch": ctx.get("original_branch"),
                        "target_branch": ctx.get("target_branch"),
                        "commits": list(ctx.get("commits", [])),
                        "merge_status": "skipped (branch setup failed or no-op)",
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return

    from app.services import cowork_git as git
    workspace = ctx["workspace"]
    target = ctx["target_branch"]
    original = ctx["original_branch"]
    strategy = ctx["strategy"]

    merge_status = "left-on-branch"

    if strategy == "merge-to-head" and success:
        # Try to merge target back to the original branch. We do NOT
        # delete the run branch automatically — leaves an audit trail.
        co = git.checkout_branch(workspace, original)
        if not co.ok:
            merge_status = f"merge-failed (checkout {original}: {co.stderr.strip()})"
        else:
            merge = git.merge_branch(
                workspace, target, no_ff=True,
                message=f"cowork {run_id}: merge {target}",
            )
            if merge.ok:
                merge_status = f"merged-into {original}"
            else:
                merge_status = f"merge-failed: {merge.stderr.strip()}"

    elif strategy == "merge-to-head" and not success:
        # Leave the run branch checked out for the user to inspect.
        merge_status = "left-on-branch (run had failures)"

    # `branch` strategy always leaves the branch checked out.

    try:
        local_memory.update_cowork_run(
            run_id,
            git_state={
                "strategy": strategy,
                "original_branch": original,
                "target_branch": target,
                "commits": list(ctx.get("commits", [])),
                "merge_status": merge_status,
            },
        )
    except Exception:  # noqa: BLE001
        pass

    # Mark inactive so a subsequent _finalise call is a no-op.
    ctx["active"] = False


def _teardown_branch_strategy(ctx: Dict[str, Any]) -> None:
    """Always-callable cleanup: drop the bus subscription."""
    sub_id = (ctx or {}).get("subscription_id")
    if sub_id is None:
        return
    try:
        from app.services.gateway.bus import default_bus
        default_bus().unsubscribe(sub_id)
    except Exception:  # noqa: BLE001
        pass
    ctx["subscription_id"] = None


def cancel_active_run(
    session_id: str, *, reason: str = "user-requested",
) -> Dict[str, Any]:
    """Find the active run for `session_id` and signal cancellation.

    Sets the in-process cancel event (so an in-flight `execute_run` can
    bail out) AND flips storage to `cancelled` so a new run can be
    started immediately.

    Phase: this passes through ``CancelToken.cancel(reason=...)`` so
    downstream consumers can pattern-match on *why* (user vs.
    timeout vs. dependency_failed). See
    ``app/services/cancel.py`` (adopted from
    ``local_ai_workbench/shared/cancel.py``).
    """
    run = local_memory.active_cowork_run(session_id)
    if run is None:
        return {"ok": False, "reason": ER.INVALID_ARGUMENT, "output": "No active cowork run."}

    with _cancel_lock:
        token = _cancel_tokens.get(run["id"])
        event = _cancel_events.get(run["id"])
    if token is not None:
        token.cancel(reason=reason)
    elif event is not None:
        # Legacy path: caller created an event without going through
        # the token registry (very rare; only happens if someone
        # called execute_run directly with a bare event).
        event.set()

    if run["status"] in {"planning", "approved"}:
        # Not running yet — clean up pending approval too.
        local_memory.clear_pending_approval(session_id=session_id)
        local_memory.update_cowork_run(
            run["id"], status="cancelled", completed_at=local_memory._now()
        )
        _publish_cowork_event("cancelled", run_id=run["id"], session_id=session_id)
    elif run["status"] in {"executing", "synthesizing"}:
        # The execute loop will see the event on its next iteration; we
        # also flip storage so other consumers see the cancellation
        # immediately.
        local_memory.update_cowork_run(
            run["id"], status="cancelled", completed_at=local_memory._now()
        )
        _publish_cowork_event("cancelled", run_id=run["id"], session_id=session_id)
    return {"ok": True, "run": local_memory.get_cowork_run(run["id"])}


def get_cancel_token(run_id: str):
    """Return the live ``CancelToken`` for ``run_id`` (or None if no
    run is active). Useful for inspecting the cancellation reason
    after a run finishes — note the token is popped at run exit, so
    callers that want to read the reason should do so before the
    cleanup window."""
    with _cancel_lock:
        return _cancel_tokens.get(run_id)
