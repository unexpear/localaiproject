"""Tests for the proactive ``honest null`` trigger
(Phase 3 of `docs/design/patch-taxonomy.md`).

Adopted from the user's `personal app` self-modifying agent.

Covers:
* ``should_fire`` heuristic in isolation:
  - too_quiet (< MIN_RECENT_ACTIONS in window) → refuse
  - too_soon (< MIN_POLL_INTERVAL) → refuse
  - cooldown (< COOLDOWN after last_proposal) → refuse
  - null_streak (>= MAX_NULL_STREAK) → refuse
  - everything ok → fire
* ``parse_proactive_response`` accepts null + non-null + tolerates
  fenced JSON / surrounding prose; rejects malformed.
* ``note_user_action`` resets null_streak (fresh activity = fresh
  context the model hasn't seen).
* End-to-end ``tick`` with a mocked model_responder:
  - null response → null_streak increments, no proposal emitted
  - non-null response with valid command → dispatcher invoked,
    last_proposal_at set, null_streak reset
  - parse failure → counted as null
* Storage round-trip via ``_normalize_proactive_state`` (state
  survives session save / load).
* Dispatcher: ``proactive status / tick / reset / prompt``.
"""
import threading
import time
import unittest
from unittest.mock import patch as mock_patch

from app.memory import local_memory
from app.services import proactive_service as ps
from app.services.command_dispatcher import dispatch_input


# ============================================================
# should_fire heuristic
# ============================================================

class ShouldFireHeuristicTests(unittest.TestCase):
    def _state(self, **fields):
        return ps.ProactiveState(**fields)

    def test_too_quiet_when_no_actions(self):
        state = self._state()
        result = ps.should_fire(state, now=1000.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ps.REFUSE_TOO_QUIET)

    def test_fires_when_two_recent_actions(self):
        state = self._state(recent_action_times=[995.0, 999.0])
        result = ps.should_fire(state, now=1000.0)
        self.assertTrue(result.ok)
        self.assertIsNone(result.reason)

    def test_old_actions_outside_window_dont_count(self):
        # Two actions but both more than ACTIVITY_WINDOW_SECONDS old.
        state = self._state(recent_action_times=[100.0, 200.0])
        result = ps.should_fire(state, now=1000.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ps.REFUSE_TOO_QUIET)

    def test_too_soon_after_last_check(self):
        state = self._state(
            last_check_at=995.0,
            recent_action_times=[995.0, 999.0],
        )
        # Only 5s since last check, less than MIN_POLL_INTERVAL_SECONDS.
        result = ps.should_fire(state, now=1000.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ps.REFUSE_TOO_SOON)

    def test_cooldown_after_last_proposal(self):
        state = self._state(
            last_proposal_at=999.0,  # 1 second ago
            last_check_at=0.0,
            recent_action_times=[999.0, 999.5],
        )
        result = ps.should_fire(state, now=1000.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ps.REFUSE_COOLDOWN)

    def test_null_streak_at_max_blocks_fire(self):
        state = self._state(
            null_streak=ps.MAX_NULL_STREAK,
            recent_action_times=[995.0, 999.0],
        )
        result = ps.should_fire(state, now=1000.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ps.REFUSE_NULL_STREAK)

    def test_null_streak_below_max_still_allows_fire(self):
        state = self._state(
            null_streak=ps.MAX_NULL_STREAK - 1,
            recent_action_times=[995.0, 999.0],
        )
        result = ps.should_fire(state, now=1000.0)
        self.assertTrue(result.ok)


# ============================================================
# parse_proactive_response
# ============================================================

class ParseProactiveResponseTests(unittest.TestCase):
    def test_null_patch(self):
        out = ps.parse_proactive_response('{"patch": null, "reason": "all good"}')
        self.assertTrue(out.is_null)
        self.assertEqual(out.reason, "all good")
        self.assertIsNone(out.parse_error)

    def test_non_null_patch_with_command(self):
        out = ps.parse_proactive_response(
            '{"patch": {"command": "note set wm pattern"}, "reason": "saw repeat edits"}'
        )
        self.assertFalse(out.is_null)
        self.assertEqual(out.patch, {"command": "note set wm pattern"})
        self.assertEqual(out.reason, "saw repeat edits")

    def test_extracts_json_from_fenced_block(self):
        # Many models wrap output in ```json ... ``` fences.
        out = ps.parse_proactive_response(
            'Here is my answer:\n```json\n{"patch": null, "reason": "nothing"}\n```'
        )
        self.assertTrue(out.is_null)
        self.assertEqual(out.reason, "nothing")

    def test_extracts_json_from_loose_braces(self):
        out = ps.parse_proactive_response(
            'My response is {"patch": null, "reason": "ok"} thanks!'
        )
        self.assertTrue(out.is_null)

    def test_empty_response_marked_parse_error(self):
        out = ps.parse_proactive_response("")
        self.assertTrue(out.is_null)
        self.assertEqual(out.parse_error, "empty_response")

    def test_non_json_marked_parse_error(self):
        out = ps.parse_proactive_response("just plain prose, no json")
        self.assertTrue(out.is_null)
        self.assertEqual(out.parse_error, "not_json")

    def test_missing_patch_field_marked_parse_error(self):
        out = ps.parse_proactive_response('{"reason": "missing patch"}')
        self.assertTrue(out.is_null)
        self.assertEqual(out.parse_error, "missing_patch_field")

    def test_patch_not_object_marked_parse_error(self):
        out = ps.parse_proactive_response('{"patch": "string", "reason": "x"}')
        self.assertTrue(out.is_null)
        self.assertEqual(out.parse_error, "patch_not_object")


# ============================================================
# note_user_action
# ============================================================

class NoteUserActionTests(unittest.TestCase):
    def test_appends_timestamp_to_recent_actions(self):
        sid = "note_action_appends"
        ps.reset_state(sid)
        ps.note_user_action(sid, now=100.0)
        ps.note_user_action(sid, now=200.0)
        state = ps.load_state(sid)
        self.assertIn(100.0, state.recent_action_times)
        self.assertIn(200.0, state.recent_action_times)

    def test_resets_null_streak(self):
        sid = "note_action_resets_streak"
        ps.reset_state(sid)
        # Set streak to 3 manually
        s = ps.load_state(sid)
        s.null_streak = 3
        ps.save_state(sid, s)
        # New user action should reset
        ps.note_user_action(sid)
        self.assertEqual(ps.load_state(sid).null_streak, 0)

    def test_caps_history_at_max(self):
        sid = "note_action_caps"
        ps.reset_state(sid)
        for i in range(ps.MAX_RECENT_ACTION_HISTORY + 20):
            ps.note_user_action(sid, now=float(i))
        state = ps.load_state(sid)
        self.assertEqual(
            len(state.recent_action_times),
            ps.MAX_RECENT_ACTION_HISTORY,
        )
        # Most-recent timestamps are kept (the older ones get dropped).
        self.assertEqual(
            min(state.recent_action_times),
            float(20),  # first 20 dropped
        )


# ============================================================
# tick orchestrator
# ============================================================

class TickOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.sid = self.id().split(".")[-1]
        ps.reset_state(self.sid)

    def _seed_active_session(self):
        # Two recent actions so should_fire returns True.
        ps.note_user_action(self.sid, now=time.time() - 10)
        ps.note_user_action(self.sid, now=time.time() - 5)

    def test_tick_refused_when_no_activity(self):
        responder_called = {"n": 0}

        def fake_responder(_messages):
            responder_called["n"] += 1
            return ""

        result = ps.tick(self.sid, model_responder=fake_responder)
        self.assertFalse(result.fired)
        self.assertEqual(result.refused_reason, ps.REFUSE_TOO_QUIET)
        self.assertEqual(responder_called["n"], 0)

    def test_tick_null_response_increments_streak(self):
        self._seed_active_session()
        responder = lambda _msgs: '{"patch": null, "reason": "nothing useful"}'
        result = ps.tick(self.sid, model_responder=responder)
        self.assertTrue(result.fired)
        self.assertTrue(result.response.is_null)
        self.assertEqual(ps.load_state(self.sid).null_streak, 1)

    def test_tick_non_null_response_emits_dispatcher_command(self):
        self._seed_active_session()
        responder = lambda _msgs: (
            '{"patch": {"command": "mode plan"}, "reason": "switching"}'
        )
        result = ps.tick(self.sid, model_responder=responder)
        self.assertTrue(result.fired)
        self.assertFalse(result.response.is_null)
        self.assertTrue(result.proposal_emitted)
        self.assertEqual(local_memory.get_mode(self.sid), "plan")
        # Streak resets on a real proposal.
        self.assertEqual(ps.load_state(self.sid).null_streak, 0)

    def test_tick_non_null_sets_last_proposal_at(self):
        self._seed_active_session()
        responder = lambda _msgs: (
            '{"patch": {"command": "mode plan"}, "reason": "x"}'
        )
        before = ps.load_state(self.sid).last_proposal_at
        ps.tick(self.sid, model_responder=responder)
        after = ps.load_state(self.sid).last_proposal_at
        self.assertGreater(after, before)

    def test_tick_parse_error_counts_as_null_streak(self):
        self._seed_active_session()
        responder = lambda _msgs: "garbage non-JSON output"
        ps.tick(self.sid, model_responder=responder)
        self.assertEqual(ps.load_state(self.sid).null_streak, 1)

    def test_tick_three_null_responses_then_idle(self):
        self._seed_active_session()
        responder = lambda _msgs: '{"patch": null, "reason": "no"}'
        # First three tick calls fire (with manual time advance to
        # bypass MIN_POLL_INTERVAL between them).
        base = time.time()
        for i in range(ps.MAX_NULL_STREAK):
            now = base + i * (ps.MIN_POLL_INTERVAL_SECONDS + 1)
            r = ps.tick(self.sid, model_responder=responder, now=now)
            self.assertTrue(r.fired)
        # Fourth tick: streak == MAX_NULL_STREAK, should refuse.
        now = base + ps.MAX_NULL_STREAK * (ps.MIN_POLL_INTERVAL_SECONDS + 1)
        r = ps.tick(self.sid, model_responder=responder, now=now)
        self.assertFalse(r.fired)
        self.assertEqual(r.refused_reason, ps.REFUSE_NULL_STREAK)


# ============================================================
# Storage round-trip
# ============================================================

class StorageRoundTripTests(unittest.TestCase):
    def test_state_survives_save_load(self):
        sid = "storage_roundtrip"
        original = ps.ProactiveState(
            last_check_at=1000.0,
            last_proposal_at=900.0,
            null_streak=2,
            recent_action_times=[800.0, 900.0, 950.0],
            last_response={"is_null": True, "reason": "x", "raw_text": "{...}"},
        )
        ps.save_state(sid, original)
        loaded = ps.load_state(sid)
        self.assertEqual(loaded.last_check_at, 1000.0)
        self.assertEqual(loaded.last_proposal_at, 900.0)
        self.assertEqual(loaded.null_streak, 2)
        self.assertEqual(loaded.recent_action_times, [800.0, 900.0, 950.0])
        self.assertEqual(loaded.last_response["reason"], "x")

    def test_normaliser_drops_garbage_fields(self):
        sid = "storage_garbage"
        ps.save_state(sid, ps.ProactiveState())
        # Inject garbage directly via update_session_context.
        ctx = local_memory.get_session_context(sid)
        ctx["proactive"] = {
            "last_check_at": "not a number",
            "null_streak": -5,
            "recent_action_times": ["nope", 1.0, "also bad", 2.0],
            "last_response": "should be a dict",
        }
        local_memory.update_session_context(session_id=sid, context=ctx)
        loaded = ps.load_state(sid)
        self.assertEqual(loaded.last_check_at, 0.0)  # garbage → 0
        self.assertEqual(loaded.null_streak, 0)      # negative → 0
        self.assertEqual(loaded.recent_action_times, [1.0, 2.0])  # bad entries dropped
        self.assertIsNone(loaded.last_response)      # non-dict → None


# ============================================================
# Dispatcher commands
# ============================================================

class ProactiveDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.sid = self.id().split(".")[-1]
        ps.reset_state(self.sid)

    def test_status_subcommand(self):
        result = dispatch_input("proactive status", session_id=self.sid)
        self.assertEqual(result["action"], "proactive")
        self.assertIn("Proactive trigger", result["output"])
        self.assertIn("would_fire_now", result["output"])

    def test_status_is_default_subcommand(self):
        result = dispatch_input("proactive", session_id=self.sid)
        self.assertEqual(result["action"], "proactive")
        self.assertIn("Proactive trigger", result["output"])

    def test_reset_clears_state(self):
        # Seed some state
        s = ps.load_state(self.sid)
        s.null_streak = 3
        s.last_proposal_at = 999.0
        ps.save_state(self.sid, s)
        result = dispatch_input("proactive reset", session_id=self.sid)
        self.assertEqual(result["action"], "proactive")
        loaded = ps.load_state(self.sid)
        self.assertEqual(loaded.null_streak, 0)
        self.assertEqual(loaded.last_proposal_at, 0.0)

    def test_prompt_subcommand_renders_messages(self):
        result = dispatch_input("proactive prompt", session_id=self.sid)
        self.assertEqual(result["action"], "proactive")
        self.assertIn("--- system ---", result["output"])
        self.assertIn("--- user ---", result["output"])
        self.assertIn("proactive review agent", result["output"])

    def test_tick_refused_when_too_quiet(self):
        # Fresh session has no activity → tick refused.
        result = dispatch_input("proactive tick", session_id=self.sid)
        self.assertEqual(result["action"], "proactive")
        self.assertIn("refused", result["output"])
        self.assertEqual(result["result"]["refused_reason"], ps.REFUSE_TOO_QUIET)

    def test_unknown_subcommand_returns_error(self):
        result = dispatch_input("proactive bogus", session_id=self.sid)
        self.assertEqual(result.get("reason"), "invalid_argument")


# ============================================================
# Dispatcher hooks user actions
# ============================================================

class DispatcherHookTests(unittest.TestCase):
    def test_dispatch_input_records_user_action(self):
        sid = "dispatcher_hook_records"
        ps.reset_state(sid)
        before = len(ps.load_state(sid).recent_action_times)
        dispatch_input("mode plan", session_id=sid)
        after = len(ps.load_state(sid).recent_action_times)
        self.assertEqual(after - before, 1)

    def test_dispatch_input_does_not_record_proactive_self_calls(self):
        # Avoid recursion: a proactive command should not count as a
        # fresh user action that would re-arm the trigger.
        sid = "dispatcher_hook_no_recurse"
        ps.reset_state(sid)
        dispatch_input("proactive status", session_id=sid)
        self.assertEqual(len(ps.load_state(sid).recent_action_times), 0)


# ============================================================
# Phase 3b: auto-scheduler
# ============================================================

class AutoFireTests(unittest.TestCase):
    """``maybe_tick_async`` is gated on ``AUTOFIRE_ENABLED`` (off by
    default) and runs ``tick`` in a daemon thread for coalescing.
    Tests opt in by setting the flag and tear down by restoring it."""

    def setUp(self):
        self.sid = self.id().split(".")[-1]
        ps.reset_state(self.sid)
        # Each test gets a fresh autofire state.
        self._was_enabled = ps.AUTOFIRE_ENABLED
        ps.set_autofire(False)
        # Drop any leftover lock for this sid.
        with ps._TICK_LOCKS_GUARD:
            ps._TICK_LOCKS.pop(self.sid, None)

    def tearDown(self):
        ps.set_autofire(self._was_enabled)
        # Wait for any in-flight daemon thread to finish so it
        # doesn't leak across tests.
        with ps._TICK_LOCKS_GUARD:
            lock = ps._TICK_LOCKS.get(self.sid)
        if lock is not None:
            lock.acquire()  # blocks until the daemon releases
            lock.release()

    def _seed_active_session(self):
        ps.note_user_action(self.sid, now=time.time() - 10)
        ps.note_user_action(self.sid, now=time.time() - 5)

    def test_autofire_disabled_by_default(self):
        # Module default is False even after import.
        # (Other tests may flip it; we restore in tearDown.)
        self.assertFalse(ps.AUTOFIRE_ENABLED)

    def test_maybe_tick_async_returns_false_when_autofire_off(self):
        self._seed_active_session()
        called = {"n": 0}
        responder = lambda _: (called.update(n=called["n"] + 1) or "{}")
        result = ps.maybe_tick_async(self.sid, model_responder=responder)
        self.assertFalse(result)
        time.sleep(0.05)
        self.assertEqual(called["n"], 0)

    def test_maybe_tick_async_returns_false_when_heuristic_refuses(self):
        # No activity → too_quiet → returns False even with autofire on.
        ps.set_autofire(True)
        called = {"n": 0}
        responder = lambda _: (called.update(n=called["n"] + 1) or "{}")
        result = ps.maybe_tick_async(self.sid, model_responder=responder)
        self.assertFalse(result)
        time.sleep(0.05)
        self.assertEqual(called["n"], 0)

    def test_maybe_tick_async_spawns_thread_when_enabled(self):
        ps.set_autofire(True)
        self._seed_active_session()
        called = {"n": 0}

        def responder(messages):
            called["n"] += 1
            return '{"patch": null, "reason": "ok"}'

        result = ps.maybe_tick_async(self.sid, model_responder=responder)
        self.assertTrue(result)
        # Wait for the daemon thread to finish.
        time.sleep(0.2)
        self.assertEqual(called["n"], 1)

    def test_dispatch_input_does_not_block_on_slow_tick(self):
        """The user's command must return promptly even if the
        proactive model call would take a long time."""
        ps.set_autofire(True)
        self._seed_active_session()

        slow_done = threading.Event()

        def slow_responder(_messages):
            time.sleep(0.3)
            slow_done.set()
            return '{"patch": null, "reason": "ok"}'

        # Pass slow_responder explicitly to avoid a race with
        # monkey-patching _default_model_responder (the daemon thread
        # might read it after the patch is restored).
        start = time.monotonic()
        ok = ps.maybe_tick_async(self.sid, model_responder=slow_responder)
        elapsed = time.monotonic() - start

        # The async call returned in well under the 0.3s sleep.
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.15)
        # And the daemon thread will eventually finish.
        self.assertTrue(slow_done.wait(timeout=2.0))

    def test_coalescing_prevents_parallel_ticks(self):
        """Two rapid maybe_tick_async calls must not spawn two
        threads — the second sees the lock held and returns False."""
        ps.set_autofire(True)
        self._seed_active_session()

        responder_started = threading.Event()
        responder_can_proceed = threading.Event()
        responder_call_count = {"n": 0}

        def slow_responder(_messages):
            responder_call_count["n"] += 1
            responder_started.set()
            responder_can_proceed.wait(timeout=2.0)
            return '{"patch": null, "reason": "ok"}'

        first = ps.maybe_tick_async(self.sid, model_responder=slow_responder)
        self.assertTrue(first)
        self.assertTrue(responder_started.wait(timeout=1.0))

        # Now try to fire again while the first is still inside the
        # responder. Should be coalesced.
        second = ps.maybe_tick_async(self.sid, model_responder=slow_responder)
        self.assertFalse(second)

        # Let the first complete.
        responder_can_proceed.set()
        time.sleep(0.2)
        self.assertEqual(responder_call_count["n"], 1)

    def test_set_autofire_returns_new_value(self):
        ps.set_autofire(True)
        self.assertTrue(ps.AUTOFIRE_ENABLED)
        ps.set_autofire(False)
        self.assertFalse(ps.AUTOFIRE_ENABLED)
        self.assertTrue(ps.set_autofire("yes"))  # truthy
        self.assertFalse(ps.set_autofire(0))     # falsy


class AutoFireDispatcherTests(unittest.TestCase):
    """``proactive autofire`` subcommand round-trips."""

    def setUp(self):
        self._was_enabled = ps.AUTOFIRE_ENABLED
        ps.set_autofire(False)

    def tearDown(self):
        ps.set_autofire(self._was_enabled)

    def test_status_no_arg(self):
        ps.set_autofire(False)
        result = dispatch_input("proactive autofire", session_id="x")
        self.assertIn("OFF", result["output"])
        self.assertEqual(result["result"]["enabled"], False)

    def test_toggle_on(self):
        result = dispatch_input("proactive autofire on", session_id="x")
        self.assertNotIn("reason", result)
        self.assertTrue(ps.AUTOFIRE_ENABLED)
        self.assertEqual(result["result"]["enabled"], True)

    def test_toggle_off(self):
        ps.set_autofire(True)
        result = dispatch_input("proactive autofire off", session_id="x")
        self.assertNotIn("reason", result)
        self.assertFalse(ps.AUTOFIRE_ENABLED)

    def test_unknown_flag_returns_error(self):
        result = dispatch_input("proactive autofire maybe", session_id="x")
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_accepts_truthy_synonyms(self):
        for flag in ("on", "true", "1", "yes"):
            ps.set_autofire(False)
            dispatch_input(f"proactive autofire {flag}", session_id="x")
            self.assertTrue(ps.AUTOFIRE_ENABLED, f"flag {flag!r} should enable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
