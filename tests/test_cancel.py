"""Tests for the CancelToken cooperative cancellation primitive
(adopted from `local_ai_workbench/shared/cancel.py`).

Covers:
* Token state machine: initial, cancel, idempotent re-cancel,
  reason capture (first wins).
* ``raise_if_cancelled`` raises ``OperationCancelled`` carrying
  the reason + label.
* ``wait_with_timeout`` returns False on timeout, True on cancel,
  honours zero/negative durations.
* ``cancellable_sleep`` bails out early when an async cancel
  fires; tolerates ``token=None``.
* ``from_event`` wraps an existing ``threading.Event`` and stays
  in sync (signals from either side propagate).
* CoWork integration: ``cancel_active_run`` captures a reason on
  the registered token; ``get_cancel_token`` exposes it.
"""
import threading
import time
import unittest
import uuid
from unittest.mock import patch as mock_patch

from app.services import cancel


def _unique_sid(prefix: str) -> str:
    """Each test gets a fresh session id so leaked cowork runs from
    prior pytest invocations (the conftest tempdir is reused across
    runs in the same shell) don't fool ``active_cowork_run``."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class CancelTokenStateMachineTests(unittest.TestCase):
    def test_initial_state(self):
        t = cancel.CancelToken("x")
        self.assertFalse(t.is_cancelled)
        self.assertEqual(t.cancelled_reason, "")
        self.assertEqual(t.label, "x")

    def test_cancel_flips_state_and_returns_true(self):
        t = cancel.CancelToken("x")
        self.assertTrue(t.cancel("user-requested"))
        self.assertTrue(t.is_cancelled)
        self.assertEqual(t.cancelled_reason, "user-requested")

    def test_cancel_is_idempotent_first_reason_wins(self):
        t = cancel.CancelToken("x")
        t.cancel("user-requested")
        self.assertFalse(t.cancel("dependency_failed"))
        self.assertEqual(t.cancelled_reason, "user-requested")

    def test_default_reason_is_cancelled(self):
        t = cancel.CancelToken("x")
        t.cancel()
        self.assertEqual(t.cancelled_reason, "cancelled")

    def test_blank_reason_falls_back_to_default(self):
        t = cancel.CancelToken("x")
        t.cancel("")
        self.assertEqual(t.cancelled_reason, "cancelled")


class RaiseIfCancelledTests(unittest.TestCase):
    def test_does_not_raise_when_not_cancelled(self):
        t = cancel.CancelToken("x")
        t.raise_if_cancelled()  # no-op

    def test_raises_with_reason_and_label(self):
        t = cancel.CancelToken("run-42")
        t.cancel("timeout")
        with self.assertRaises(cancel.OperationCancelled) as ctx:
            t.raise_if_cancelled()
        exc = ctx.exception
        self.assertEqual(exc.reason, "timeout")
        self.assertEqual(exc.token_label, "run-42")
        self.assertIn("run-42", str(exc))
        self.assertIn("timeout", str(exc))

    def test_message_omits_brackets_when_label_empty(self):
        t = cancel.CancelToken("")
        t.cancel("nope")
        with self.assertRaises(cancel.OperationCancelled) as ctx:
            t.raise_if_cancelled()
        self.assertNotIn("[", str(ctx.exception))


class WaitWithTimeoutTests(unittest.TestCase):
    def test_returns_false_on_timeout(self):
        t = cancel.CancelToken("x")
        start = time.monotonic()
        ok = t.wait_with_timeout(0.05)
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.5)

    def test_returns_true_on_pre_cancelled(self):
        t = cancel.CancelToken("x")
        t.cancel()
        self.assertTrue(t.wait_with_timeout(5.0))  # returns immediately

    def test_zero_timeout_returns_current_state(self):
        t = cancel.CancelToken("x")
        self.assertFalse(t.wait_with_timeout(0))
        t.cancel()
        self.assertTrue(t.wait_with_timeout(0))

    def test_returns_true_when_cancelled_during_wait(self):
        t = cancel.CancelToken("x")

        def fire_async():
            time.sleep(0.05)
            t.cancel("async-fire")

        threading.Thread(target=fire_async, daemon=True).start()
        start = time.monotonic()
        ok = t.wait_with_timeout(5.0)
        elapsed = time.monotonic() - start
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.5)


class CancellableSleepTests(unittest.TestCase):
    def test_returns_false_on_normal_sleep(self):
        t = cancel.CancelToken("x")
        start = time.monotonic()
        result = cancel.cancellable_sleep(t, 0.05)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.04)

    def test_returns_true_when_cancel_fires_during_sleep(self):
        t = cancel.CancelToken("x")

        def fire_async():
            time.sleep(0.03)
            t.cancel()

        threading.Thread(target=fire_async, daemon=True).start()
        start = time.monotonic()
        result = cancel.cancellable_sleep(t, 5.0)
        elapsed = time.monotonic() - start
        self.assertTrue(result)
        self.assertLess(elapsed, 0.5)

    def test_tolerates_none_token(self):
        start = time.monotonic()
        result = cancel.cancellable_sleep(None, 0.02)
        elapsed = time.monotonic() - start
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.015)


class FromEventTests(unittest.TestCase):
    def test_returns_none_for_none_input(self):
        self.assertIsNone(cancel.from_event(None))

    def test_external_event_set_propagates_to_token(self):
        event = threading.Event()
        token = cancel.from_event(event, label="wrapped")
        self.assertFalse(token.is_cancelled)
        event.set()
        self.assertTrue(token.is_cancelled)
        # Reason stays empty when signalled directly via the event
        # (this is documented in the from_event docstring).
        self.assertEqual(token.cancelled_reason, "")

    def test_token_cancel_propagates_to_event(self):
        event = threading.Event()
        token = cancel.from_event(event, label="wrapped")
        token.cancel("via-token")
        self.assertTrue(event.is_set())
        self.assertEqual(token.cancelled_reason, "via-token")


# ============================================================
# CoWork integration
# ============================================================

class CoWorkCancellationIntegrationTests(unittest.TestCase):
    def test_cancel_active_run_records_reason_on_token(self):
        # Set up a session with a fake active run + register a token.
        from app.memory import local_memory
        from app.services import cowork
        sid = _unique_sid("cancel_token_cowork_test")

        # Create a synthetic run in storage
        run = local_memory.add_cowork_run(
            session_id=sid, goal="test goal",
            plan=[{"id": "s1", "builder": "b1", "task": "t1", "depends_on": []}],
        )
        run_id = run["id"]
        local_memory.update_cowork_run(run_id, status="executing")

        # Register a token in the parallel registry, mimicking what
        # execute_run does on entry.
        event = threading.Event()
        token = cancel.from_event(event, label=run_id)
        with cowork._cancel_lock:
            cowork._cancel_events[run_id] = event
            cowork._cancel_tokens[run_id] = token

        try:
            result = cowork.cancel_active_run(sid, reason="dependency_failed")
            self.assertTrue(result["ok"])
            # Token captured the reason.
            self.assertTrue(token.is_cancelled)
            self.assertEqual(token.cancelled_reason, "dependency_failed")
            # Underlying event also signalled (legacy callers still work).
            self.assertTrue(event.is_set())
        finally:
            with cowork._cancel_lock:
                cowork._cancel_events.pop(run_id, None)
                cowork._cancel_tokens.pop(run_id, None)

    def test_get_cancel_token_returns_live_token(self):
        from app.memory import local_memory
        from app.services import cowork
        sid = _unique_sid("cancel_token_get_test")
        run = local_memory.add_cowork_run(
            session_id=sid, goal="test",
            plan=[{"id": "s1", "builder": "b1", "task": "t1", "depends_on": []}],
        )
        run_id = run["id"]
        local_memory.update_cowork_run(run_id, status="executing")

        event = threading.Event()
        token = cancel.from_event(event, label=run_id)
        with cowork._cancel_lock:
            cowork._cancel_tokens[run_id] = token
            cowork._cancel_events[run_id] = event

        try:
            looked_up = cowork.get_cancel_token(run_id)
            self.assertIs(looked_up, token)
            looked_up.cancel("custom-reason")
            self.assertEqual(token.cancelled_reason, "custom-reason")
        finally:
            with cowork._cancel_lock:
                cowork._cancel_events.pop(run_id, None)
                cowork._cancel_tokens.pop(run_id, None)

    def test_cancel_active_run_with_no_active_run_returns_invalid_argument(self):
        from app.services import cowork
        result = cowork.cancel_active_run("nonexistent_session_xyz")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_argument")

    def test_default_reason_is_user_requested(self):
        from app.memory import local_memory
        from app.services import cowork
        sid = _unique_sid("cancel_default_reason_test")
        run = local_memory.add_cowork_run(
            session_id=sid, goal="test",
            plan=[{"id": "s1", "builder": "b1", "task": "t1", "depends_on": []}],
        )
        run_id = run["id"]
        local_memory.update_cowork_run(run_id, status="executing")

        event = threading.Event()
        token = cancel.from_event(event, label=run_id)
        with cowork._cancel_lock:
            cowork._cancel_tokens[run_id] = token
            cowork._cancel_events[run_id] = event

        try:
            cowork.cancel_active_run(sid)  # no reason kwarg
            self.assertEqual(token.cancelled_reason, "user-requested")
        finally:
            with cowork._cancel_lock:
                cowork._cancel_events.pop(run_id, None)
                cowork._cancel_tokens.pop(run_id, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
