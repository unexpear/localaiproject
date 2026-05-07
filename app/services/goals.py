"""High-level "goal verbs" — durable operations that loop until done.

The lower tier (primitives) already exists across `local_memory`, the
storage layer, and the dispatcher: `add_task`, `get_inbox`, `mark_task`,
`dispatch_input`, etc. Those return immediately after a single
side-effect.

This module layers durable verbs on top — operations that *wait for the
real-world outcome* before returning. Match for rs-sdk's split between
`bot.*` (waits) and `sdk.*` (returns immediately):

  primitive (returns immediately)        goal verb (waits for completion)
  ----------------------------------     ----------------------------------
  add_task(planner, text)                dispatch_and_wait(planner, text)
  dispatch_to_all(planner, text)         fan_out_and_wait(planner, text)
  next_task(builder)                     drain_inbox(builder)
  get_inbox(builder)                     wait_for_inbox_empty(builder)
  get_task(task_id)                      wait_for_task(task_id)
  get_session_context(session_id)        wait_for_state(session_id, predicate)

Polling-based by default (small `poll_interval`, optional `timeout`).
Every verb accepts an optional `cancel_event: threading.Event`-shape so
callers can abort cleanly — same convention `pull_model` uses.

Threading
---------
These verbs may BLOCK for a long time. Don't call them from the Qt main
thread; spawn a worker. The CLI dispatcher commands are fine because the
CLI is already off the GUI loop.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from app.memory import local_memory


logger = logging.getLogger(__name__)


# Statuses a task can be in once it's no longer eligible for further pulls
# (a worker won't process them; pollers can stop waiting).
TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})

# A task that's awaiting workspace approval is technically still "in_progress"
# in storage, but in_progress with the magic marker means a worker won't pick
# it up again until the approval is resolved. Treat it as a stop condition for
# wait verbs so they don't hang forever waiting on a pending approval.
APPROVAL_PENDING_MARKER = "(awaiting workspace approval)"


_DEFAULT_POLL_INTERVAL = 0.1


def _is_cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _sleep_with_cancel(seconds: float, cancel_event) -> bool:
    """Sleep up to `seconds`, returning True if cancelled mid-sleep."""
    if seconds <= 0:
        return _is_cancelled(cancel_event)
    if cancel_event is None:
        time.sleep(seconds)
        return False
    # Wait on the event with a timeout — returns True if event was set.
    return cancel_event.wait(timeout=seconds)


def _deadline(timeout: Optional[float]) -> Optional[float]:
    if timeout is None:
        return None
    return time.monotonic() + max(0.0, float(timeout))


def _remaining(deadline: Optional[float]) -> Optional[float]:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _is_terminal(task: Optional[Dict[str, Any]]) -> bool:
    if not task:
        return False
    if task.get("status") in TERMINAL_STATUSES:
        return True
    if task.get("status") == "in_progress" and task.get("result") == APPROVAL_PENDING_MARKER:
        return True
    return False


def _resolve_runner(runner: Optional[Callable]):
    """Pick the function that pops + runs one task. Lazy-imported to avoid
    a circular dependency between goals.py and command_dispatcher."""
    if runner is not None:
        return runner
    from app.services.command_dispatcher import _run_one_inbox_task
    return _run_one_inbox_task


# ---------------------------------------------------------------------------
# wait verbs (polling-based)
# ---------------------------------------------------------------------------

def wait_for_task(
    task_id: str,
    *,
    timeout: Optional[float] = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    cancel_event=None,
) -> Optional[Dict[str, Any]]:
    """Block until the task with `task_id` is terminal, or `timeout` elapses.

    Returns the final task dict if observed terminal, the latest task dict
    on timeout (still non-terminal), or None if the task id was never found.
    Returns None and stops promptly on cancellation.
    """
    if not task_id:
        raise ValueError("task_id is required")
    deadline = _deadline(timeout)
    last: Optional[Dict[str, Any]] = None
    while True:
        if _is_cancelled(cancel_event):
            return last
        task = local_memory.get_task(task_id)
        if task is None:
            return None
        last = task
        if _is_terminal(task):
            return task
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return last
        sleep_for = poll_interval if remaining is None else min(poll_interval, remaining)
        if _sleep_with_cancel(sleep_for, cancel_event):
            return last


def wait_for_inbox_empty(
    builder_session: str,
    *,
    timeout: Optional[float] = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    cancel_event=None,
) -> bool:
    """Block until `builder_session` has no queued or in-progress tasks.

    Returns True if the inbox emptied, False on timeout or cancellation.
    """
    if not builder_session:
        raise ValueError("builder_session is required")
    deadline = _deadline(timeout)
    while True:
        if _is_cancelled(cancel_event):
            return False
        queued = local_memory.get_inbox(builder_session, status="queued")
        in_progress = local_memory.in_progress_task_for(builder_session)
        if not queued and (in_progress is None or in_progress.get("result") == APPROVAL_PENDING_MARKER):
            # Approval-pending blocks the queue; treat as "drained for now".
            return True
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return False
        sleep_for = poll_interval if remaining is None else min(poll_interval, remaining)
        if _sleep_with_cancel(sleep_for, cancel_event):
            return False


def wait_for_state(
    session_id: str,
    predicate: Callable[[Dict[str, Any]], bool],
    *,
    timeout: Optional[float] = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    cancel_event=None,
) -> bool:
    """Generic wait — block until `predicate(session_context_dict)` returns True.

    Returns True if the predicate became true, False on timeout / cancel.
    """
    if not callable(predicate):
        raise TypeError("predicate must be callable")
    deadline = _deadline(timeout)
    while True:
        if _is_cancelled(cancel_event):
            return False
        context = local_memory.get_session_context(session_id=session_id)
        try:
            if predicate(context):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("wait_for_state predicate raised: %s", exc)
            # Treat predicate exceptions as 'not yet'; bail on timeout.
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return False
        sleep_for = poll_interval if remaining is None else min(poll_interval, remaining)
        if _sleep_with_cancel(sleep_for, cancel_event):
            return False


# ---------------------------------------------------------------------------
# drain — actively process inbox tasks until done
# ---------------------------------------------------------------------------

def drain_inbox(
    builder_session: str,
    *,
    max_iterations: int = 100,
    timeout: Optional[float] = None,
    cancel_event=None,
    runner: Optional[Callable] = None,
    stop_on_pause: bool = True,
) -> List[Dict[str, Any]]:
    """Pull and run tasks from `builder_session`'s inbox until empty / stopped.

    `runner(session_id) -> outcome_dict` is the per-task callable; defaults to
    the dispatcher's `_run_one_inbox_task`. The returned list contains one
    outcome dict per task processed (in order).

    Stops when:
      - inbox empty
      - cancellation
      - timeout reached
      - `stop_on_pause=True` and a task pauses for workspace approval
      - `max_iterations` reached
    """
    if not builder_session:
        raise ValueError("builder_session is required")
    runner = _resolve_runner(runner)
    deadline = _deadline(timeout)
    outcomes: List[Dict[str, Any]] = []

    for _ in range(max_iterations):
        if _is_cancelled(cancel_event):
            break
        if deadline is not None and _remaining(deadline) <= 0:
            break
        outcome = runner(builder_session)
        if outcome.get("outcome") == "empty":
            break
        outcomes.append(outcome)
        if stop_on_pause and outcome.get("outcome") == "paused":
            break
    return outcomes


# ---------------------------------------------------------------------------
# dispatch + wait — durable single-task verb
# ---------------------------------------------------------------------------

def dispatch_and_wait(
    planner_session: str,
    task_text: str,
    *,
    builder: Optional[str] = None,
    timeout: Optional[float] = None,
    cancel_event=None,
    runner: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """Queue a task and process the builder's inbox until that task is terminal.

    Returns the final task dict (with `status` in {done, failed, skipped} or
    `in_progress` if it paused for approval), or None on cancellation /
    irrecoverable timeout.

    Equivalent low-level sequence:
        task = local_memory.add_task(planner, text, builder=...)
        drain_inbox(task['builder_session'], timeout=...)
        return local_memory.get_task(task['id'])
    """
    task = local_memory.add_task(planner_session, task_text, builder=builder)
    builder_id = task["builder_session"]
    deadline = _deadline(timeout)

    runner_fn = _resolve_runner(runner)

    # Drain in a loop, checking after each task whether ours is terminal.
    while not _is_cancelled(cancel_event):
        latest = local_memory.get_task(task["id"])
        if _is_terminal(latest):
            return latest
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            return latest
        outcome = runner_fn(builder_id)
        if outcome.get("outcome") == "empty":
            # Nothing to drain but our task still isn't terminal — that means
            # someone else (another worker) consumed it but hasn't finished;
            # poll until it does.
            return wait_for_task(
                task["id"],
                timeout=remaining,
                cancel_event=cancel_event,
            )
        if outcome.get("outcome") == "paused":
            # The blocker may BE our task; either way, stop and report state.
            return local_memory.get_task(task["id"])
    return local_memory.get_task(task["id"])


# ---------------------------------------------------------------------------
# fan-out + wait
# ---------------------------------------------------------------------------

def fan_out_and_wait(
    planner_session: str,
    task_text: str,
    *,
    timeout: Optional[float] = None,
    cancel_event=None,
    runner: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """Dispatch one task to every linked builder, then drain each in turn.

    Builders are processed serially (one inbox at a time) — no parallelism in
    v1. `timeout` covers the WHOLE fan-out, not per-builder. Returns the
    final task dicts in dispatch order.
    """
    tasks = local_memory.dispatch_to_all(planner_session, task_text)
    deadline = _deadline(timeout)
    results: List[Dict[str, Any]] = []
    for task in tasks:
        if _is_cancelled(cancel_event):
            results.append(local_memory.get_task(task["id"]) or task)
            continue
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            results.append(local_memory.get_task(task["id"]) or task)
            continue
        final = dispatch_and_wait_for_existing(
            task,
            timeout=remaining,
            cancel_event=cancel_event,
            runner=runner,
        )
        results.append(final or task)
    return results


def dispatch_and_wait_for_existing(
    task: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
    cancel_event=None,
    runner: Optional[Callable] = None,
) -> Optional[Dict[str, Any]]:
    """Drain a builder's inbox just long enough to make `task` terminal.

    Used by fan_out_and_wait — same logic as dispatch_and_wait but operates
    on a task that's already been queued.
    """
    builder_id = task["builder_session"]
    runner_fn = _resolve_runner(runner)
    deadline = _deadline(timeout)
    while not _is_cancelled(cancel_event):
        latest = local_memory.get_task(task["id"])
        if _is_terminal(latest):
            return latest
        if deadline is not None and _remaining(deadline) <= 0:
            return latest
        outcome = runner_fn(builder_id)
        if outcome.get("outcome") in ("empty", "paused"):
            return local_memory.get_task(task["id"])
    return local_memory.get_task(task["id"])


# ---------------------------------------------------------------------------
# iterate_until_complete — re-dispatch until the builder declares done
# ---------------------------------------------------------------------------

#: Marker the builder must include on its own line in any response that
#: signals "I'm done with this task — no more iterations needed."
TASK_COMPLETE_MARKER = "<task-complete>"

#: Absolute safety cap; user-supplied max_iterations is clamped here.
ITERATE_HARD_CAP = 20


def _builder_signaled_complete(task: Optional[Dict[str, Any]]) -> bool:
    """True if the task's stored result contains the completion marker.

    The chat handler stores the builder's response text in `task.result`;
    we look for the literal marker on its own line (anywhere in the text).
    """
    if not task:
        return False
    result = task.get("result") or ""
    if not isinstance(result, str):
        return False
    for line in result.splitlines():
        if line.strip() == TASK_COMPLETE_MARKER:
            return True
    return False


def iterate_until_complete(
    planner_session: str,
    task_text: str,
    *,
    builder: Optional[str] = None,
    max_iterations: int = 5,
    timeout: Optional[float] = None,
    cancel_event=None,
    runner: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Repeatedly re-dispatch ``task_text`` to the same builder until either:

    - the builder's response contains a ``<task-complete>`` line, OR
    - the task ends with terminal status ``failed`` or ``skipped``, OR
    - the task pauses for workspace approval (caller resumes after approve), OR
    - ``max_iterations`` (or the hard cap of ``ITERATE_HARD_CAP``) is hit, OR
    - cancellation / timeout fires

    Returns a summary dict::

        {
            "iterations": <int>,
            "completed": <bool>,           # True iff <task-complete> seen
            "last_status": <str>,          # done | failed | skipped | in_progress
            "last_result": <str|None>,
            "task_ids": [<str>, ...],      # one per iteration
            "stopped_reason": <str>,       # marker | failed | paused | cap | timeout | cancelled
        }

    Each iteration is a fresh ``dispatch_and_wait`` call against the same
    builder. The builder accumulates context across iterations via its
    session history (existing chat-service behaviour).

    Builders MUST opt in by emitting ``<task-complete>`` on a line by itself
    in their response. Anything else means "I need another turn."
    """
    capped_max = min(max(1, int(max_iterations or 1)), ITERATE_HARD_CAP)
    deadline = _deadline(timeout)
    task_ids: List[str] = []
    last_task: Optional[Dict[str, Any]] = None
    completed = False
    stopped_reason = "cap"

    for iteration in range(1, capped_max + 1):
        if _is_cancelled(cancel_event):
            stopped_reason = "cancelled"
            break

        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            stopped_reason = "timeout"
            break

        try:
            last_task = dispatch_and_wait(
                planner_session,
                task_text,
                builder=builder,
                timeout=remaining,
                cancel_event=cancel_event,
                runner=runner,
            )
        except ValueError:
            # Re-raise: bad input shouldn't be silently swallowed by the
            # iteration loop — it means there's no link to dispatch to,
            # or the planner session is empty.
            raise

        if last_task is None:
            stopped_reason = "cancelled"
            break

        task_ids.append(last_task.get("id"))
        status = last_task.get("status")

        # Workspace approval mid-iteration: stop and let the caller resume.
        if (
            status == "in_progress"
            and last_task.get("result") == APPROVAL_PENDING_MARKER
        ):
            stopped_reason = "paused"
            break

        if _builder_signaled_complete(last_task):
            completed = True
            stopped_reason = "marker"
            break

        if status in ("failed", "skipped"):
            stopped_reason = status
            break
        # status == "done" without the marker means "this turn finished;
        # builder wants another try" — fall through to the next iteration.

    return {
        "iterations": len(task_ids),
        "completed": completed,
        "last_status": (last_task.get("status") if last_task else None),
        "last_result": (last_task.get("result") if last_task else None),
        "task_ids": task_ids,
        "stopped_reason": stopped_reason,
    }
