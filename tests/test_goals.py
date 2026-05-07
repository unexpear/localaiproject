"""Tests for the high-level goal verbs in app.services.goals.

Goals are the second tier of the API — durable verbs that loop until
their semantic outcome is reached. These tests use real local_memory
storage and a mocked `_run_one_inbox_task` so they don't require Ollama.
"""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import goals
from app.services.command_dispatcher import dispatch_input
from app.services.gateway import KIND_TASK, default_bus, reset_default_bus_for_tests


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _runner_marks_done(builder_session):
    """Test runner — pops one task and marks it done with a canned response."""
    task = local_memory.next_task(builder_session)
    if not task:
        return {"task": None, "outcome": "empty", "error": None}
    updated = local_memory.mark_task(task["id"], status="done", result=f"did: {task['task']}")
    return {"task": task, "outcome": "done", "error": None}


def _runner_marks_failed(builder_session):
    task = local_memory.next_task(builder_session)
    if not task:
        return {"task": None, "outcome": "empty", "error": None}
    local_memory.mark_task(task["id"], status="failed", result="boom")
    return {"task": task, "outcome": "failed", "error": "boom"}


def _runner_pauses_on_first_then_done(state):
    """Closure factory: first call returns 'paused', subsequent calls 'done'."""
    def runner(builder_session):
        task = local_memory.next_task(builder_session)
        if not task:
            return {"task": None, "outcome": "empty", "error": None}
        if state["calls"] == 0:
            state["calls"] += 1
            local_memory.mark_task(task["id"], status="in_progress",
                                   result="(awaiting workspace approval)")
            return {"task": task, "outcome": "paused", "error": None}
        state["calls"] += 1
        local_memory.mark_task(task["id"], status="done", result=f"done {task['task']}")
        return {"task": task, "outcome": "done", "error": None}
    return runner


# ============================================================
# Pure helpers
# ============================================================

class GoalsHelpersTests(unittest.TestCase):
    def test_terminal_statuses_set(self):
        self.assertEqual(goals.TERMINAL_STATUSES, frozenset({"done", "failed", "skipped"}))

    def test_is_terminal_for_each_status(self):
        self.assertFalse(goals._is_terminal(None))
        self.assertFalse(goals._is_terminal({"status": "queued"}))
        self.assertFalse(goals._is_terminal({"status": "in_progress"}))
        self.assertTrue(goals._is_terminal({"status": "done"}))
        self.assertTrue(goals._is_terminal({"status": "failed"}))
        self.assertTrue(goals._is_terminal({"status": "skipped"}))
        # Approval-paused counts as terminal-for-now so wait verbs don't hang.
        self.assertTrue(goals._is_terminal({
            "status": "in_progress",
            "result": "(awaiting workspace approval)",
        }))

    def test_deadline_and_remaining(self):
        self.assertIsNone(goals._deadline(None))
        d = goals._deadline(0.5)
        self.assertGreater(goals._remaining(d), 0)
        time.sleep(0.05)
        self.assertLess(goals._remaining(d), 0.5)
        self.assertIsNone(goals._remaining(None))

    def test_sleep_with_cancel_returns_quickly_when_cancelled(self):
        ev = threading.Event()
        ev.set()
        start = time.monotonic()
        cancelled = goals._sleep_with_cancel(2.0, ev)
        elapsed = time.monotonic() - start
        self.assertTrue(cancelled)
        self.assertLess(elapsed, 0.5)


# ============================================================
# wait_for_task
# ============================================================

class WaitForTaskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_returns_immediately_when_task_already_terminal(self):
        task = local_memory.add_task("p", "x")
        local_memory.mark_task(task["id"], status="done", result="r")
        start = time.monotonic()
        final = goals.wait_for_task(task["id"], poll_interval=0.5)
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertEqual(final["status"], "done")

    def test_returns_when_task_becomes_terminal_during_wait(self):
        task = local_memory.add_task("p", "x")

        def flip_after_delay():
            time.sleep(0.05)
            local_memory.mark_task(task["id"], status="done", result="r")

        threading.Thread(target=flip_after_delay, daemon=True).start()
        final = goals.wait_for_task(task["id"], timeout=2.0, poll_interval=0.02)
        self.assertEqual(final["status"], "done")

    def test_returns_latest_on_timeout(self):
        task = local_memory.add_task("p", "x")
        final = goals.wait_for_task(task["id"], timeout=0.05, poll_interval=0.02)
        self.assertEqual(final["status"], "queued")

    def test_returns_none_for_unknown_task_id(self):
        result = goals.wait_for_task("task_nope", timeout=0.05, poll_interval=0.02)
        self.assertIsNone(result)

    def test_cancellation_returns_promptly(self):
        task = local_memory.add_task("p", "x")
        ev = threading.Event()

        def cancel_after_short_delay():
            time.sleep(0.05)
            ev.set()

        threading.Thread(target=cancel_after_short_delay, daemon=True).start()
        start = time.monotonic()
        result = goals.wait_for_task(task["id"], timeout=5.0, poll_interval=0.01, cancel_event=ev)
        self.assertLess(time.monotonic() - start, 1.0)
        # Returns the latest snapshot of the task (still queued).
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "queued")

    def test_empty_task_id_raises(self):
        with self.assertRaises(ValueError):
            goals.wait_for_task("")


# ============================================================
# wait_for_inbox_empty
# ============================================================

class WaitForInboxEmptyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_returns_true_immediately_when_already_empty(self):
        start = time.monotonic()
        ok = goals.wait_for_inbox_empty("b", poll_interval=0.5)
        self.assertTrue(ok)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_returns_true_when_drained_during_wait(self):
        local_memory.add_task("p", "x")

        def drain():
            time.sleep(0.05)
            t = local_memory.next_task("b")
            local_memory.mark_task(t["id"], status="done", result="r")

        threading.Thread(target=drain, daemon=True).start()
        ok = goals.wait_for_inbox_empty("b", timeout=2.0, poll_interval=0.02)
        self.assertTrue(ok)

    def test_returns_false_on_timeout(self):
        local_memory.add_task("p", "x")
        ok = goals.wait_for_inbox_empty("b", timeout=0.05, poll_interval=0.02)
        self.assertFalse(ok)

    def test_paused_task_counts_as_drained(self):
        # An approval-pending task means workers won't progress; treat as drained
        # so the wait doesn't hang forever.
        task = local_memory.add_task("p", "x")
        local_memory.next_task("b")  # mark in_progress
        local_memory.mark_task(task["id"], status="in_progress",
                               result="(awaiting workspace approval)")
        ok = goals.wait_for_inbox_empty("b", timeout=0.5, poll_interval=0.02)
        self.assertTrue(ok)


# ============================================================
# wait_for_state
# ============================================================

class WaitForStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_returns_true_on_predicate_match(self):
        local_memory.set_workspace(session_id="default", workspace_path="C:/proj")
        ok = goals.wait_for_state(
            "default",
            lambda ctx: ctx.get("workspace_path") == "C:/proj",
            timeout=0.5,
            poll_interval=0.02,
        )
        self.assertTrue(ok)

    def test_returns_false_on_timeout(self):
        ok = goals.wait_for_state(
            "default",
            lambda ctx: ctx.get("workspace_path") == "never",
            timeout=0.05,
            poll_interval=0.02,
        )
        self.assertFalse(ok)

    def test_predicate_exception_treated_as_not_yet(self):
        def angry(_ctx):
            raise RuntimeError("nope")

        ok = goals.wait_for_state("default", angry, timeout=0.05, poll_interval=0.02)
        self.assertFalse(ok)

    def test_non_callable_predicate_raises(self):
        with self.assertRaises(TypeError):
            goals.wait_for_state("default", "not callable")


# ============================================================
# drain_inbox
# ============================================================

class DrainInboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_drains_every_queued_task(self):
        for i in range(3):
            local_memory.add_task("p", f"task {i}")
        outcomes = goals.drain_inbox("b", runner=_runner_marks_done)
        self.assertEqual(len(outcomes), 3)
        self.assertEqual([o["outcome"] for o in outcomes], ["done", "done", "done"])
        self.assertEqual(len(local_memory.get_inbox("b")), 0)

    def test_stops_on_pause_when_stop_on_pause_true(self):
        for i in range(3):
            local_memory.add_task("p", f"task {i}")
        state = {"calls": 0}
        runner = _runner_pauses_on_first_then_done(state)
        outcomes = goals.drain_inbox("b", runner=runner, stop_on_pause=True)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["outcome"], "paused")
        # Two tasks still queued
        self.assertEqual(len(local_memory.get_inbox("b")), 2)

    def test_continues_past_pause_when_stop_on_pause_false(self):
        for i in range(2):
            local_memory.add_task("p", f"task {i}")
        state = {"calls": 0}
        runner = _runner_pauses_on_first_then_done(state)
        outcomes = goals.drain_inbox("b", runner=runner, stop_on_pause=False)
        # 3 calls total (2 tasks + 1 'empty' termination)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual([o["outcome"] for o in outcomes], ["paused", "done"])

    def test_max_iterations_caps_at_n(self):
        for i in range(20):
            local_memory.add_task("p", f"task {i}")
        outcomes = goals.drain_inbox("b", max_iterations=5, runner=_runner_marks_done)
        self.assertEqual(len(outcomes), 5)
        # 15 still queued
        self.assertEqual(len(local_memory.get_inbox("b")), 15)


# ============================================================
# dispatch_and_wait
# ============================================================

class DispatchAndWaitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_returns_done_task_after_drain(self):
        final = goals.dispatch_and_wait("p", "do the thing", runner=_runner_marks_done)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["result"], "did: do the thing")

    def test_returns_failed_task(self):
        final = goals.dispatch_and_wait("p", "boom", runner=_runner_marks_failed)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["result"], "boom")

    def test_returns_paused_task_when_runner_pauses(self):
        state = {"calls": 0}
        runner = _runner_pauses_on_first_then_done(state)
        final = goals.dispatch_and_wait("p", "needs approval", runner=runner)
        # The runner pauses immediately; dispatch_and_wait returns the paused task
        # rather than spinning forever.
        self.assertEqual(final["status"], "in_progress")
        self.assertEqual(final["result"], "(awaiting workspace approval)")

    def test_explicit_builder_argument(self):
        local_memory.add_builder("p", "b2")
        final = goals.dispatch_and_wait("p", "x", builder="b2", runner=_runner_marks_done)
        self.assertEqual(final["builder_session"], "b2")


# ============================================================
# fan_out_and_wait
# ============================================================

class FanOutAndWaitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        local_memory.add_builder("p", "b3")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_dispatches_one_task_per_builder_and_drains_each(self):
        finals = goals.fan_out_and_wait("p", "go", runner=_runner_marks_done)
        self.assertEqual(len(finals), 3)
        self.assertEqual({t["builder_session"] for t in finals}, {"b1", "b2", "b3"})
        for t in finals:
            self.assertEqual(t["status"], "done")
        # All three builders' inboxes are now empty
        self.assertEqual(len(local_memory.get_inbox("b1")), 0)
        self.assertEqual(len(local_memory.get_inbox("b2")), 0)
        self.assertEqual(len(local_memory.get_inbox("b3")), 0)


# ============================================================
# Bus events on task lifecycle
# ============================================================

class TaskBusEventsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        reset_default_bus_for_tests()
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        reset_default_bus_for_tests()
        self._tmp.cleanup()

    def test_done_task_publishes_event_on_planner_and_builder_sessions(self):
        local_memory.add_task("p", "x")

        ok_chat = {
            "ok": True, "session_id": "b", "response": "ok",
            "fallback_used": False, "approval_required": False,
            "compaction_performed": False, "execution_id": "exe_1",
        }

        events = []
        default_bus().subscribe(default_bus().ANY, events.append)
        with patch("app.services.command_dispatcher.handle_chat", return_value=ok_chat):
            dispatch_input("inbox next", session_id="b")

        task_events = [e for e in events if e.kind == KIND_TASK]
        self.assertGreaterEqual(len(task_events), 2)  # one for planner, one for builder
        sessions_seen = {e.session_id for e in task_events}
        self.assertIn("p", sessions_seen)
        self.assertIn("b", sessions_seen)
        # Each carries the right payload
        for e in task_events:
            self.assertEqual(e.payload["outcome"], "done")
            self.assertEqual(e.payload["builder_session"], "b")

    def test_paused_task_publishes_paused_outcome(self):
        local_memory.add_task("p", "approval needed")
        approval_event = {
            "ok": False, "approval_required": True,
            "pending_action": {"kind": "read", "origin": "chat",
                               "workspace_path": "C:/proj", "paths": ["a.py"]},
            "response": "[approval required]",
            "fallback_used": False, "compaction_performed": False,
        }
        events = []
        default_bus().subscribe(default_bus().ANY, events.append)
        with patch("app.services.command_dispatcher.handle_chat", return_value=approval_event):
            dispatch_input("inbox next", session_id="b")
        outcomes = [e.payload["outcome"] for e in events if e.kind == KIND_TASK]
        self.assertIn("paused", outcomes)


# ============================================================
# Dispatcher `do` and `wait` commands
# ============================================================

class DispatcherDoWaitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_do_with_one_builder_dispatches_and_waits(self):
        ok_chat = {
            "ok": True, "session_id": "b", "response": "did it",
            "fallback_used": False, "approval_required": False,
            "compaction_performed": False, "execution_id": "exe_1",
        }
        with patch("app.services.command_dispatcher.handle_chat", return_value=ok_chat):
            ev = dispatch_input("do refactor router", session_id="p")
        self.assertEqual(ev["action"], "do")
        # Fan-out form summary
        self.assertIn("1 task(s)", ev["output"])
        self.assertIn("done", ev["output"])

    def test_do_with_at_builder_targets_one(self):
        local_memory.add_builder("p", "b2")
        ok_chat = {
            "ok": True, "session_id": "b2", "response": "ok",
            "fallback_used": False, "approval_required": False,
            "compaction_performed": False, "execution_id": "exe_1",
        }
        with patch("app.services.command_dispatcher.handle_chat", return_value=ok_chat):
            ev = dispatch_input("do @b2 the thing", session_id="p")
        self.assertEqual(ev["action"], "do")
        self.assertIn("'b2'", ev["output"])
        # b1 inbox still empty (we targeted b2 only)
        self.assertEqual(len(local_memory.get_inbox("b")), 0)

    def test_do_without_text_returns_usage(self):
        ev = dispatch_input("do", session_id="p")
        self.assertIn("Usage:", ev["output"])

    def test_wait_inbox_returns_drained(self):
        ev = dispatch_input("wait inbox b", session_id="p")
        self.assertIn("drained", ev["output"])

    def test_wait_inbox_timeout_returns_message(self):
        local_memory.add_task("p", "X")
        ev = dispatch_input("wait inbox b 1", session_id="p")
        # 1-second timeout is enough for the test to not hang; queue still has a task
        self.assertIn("Timed out", ev["output"])

    def test_wait_task_unknown_id(self):
        ev = dispatch_input("wait task task_nope", session_id="p")
        self.assertIn("No task with id", ev["output"])

    def test_wait_task_returns_terminal(self):
        task = local_memory.add_task("p", "x")
        local_memory.mark_task(task["id"], status="done", result="r")
        ev = dispatch_input(f"wait task {task['id']}", session_id="p")
        self.assertIn("terminal", ev["output"])
        self.assertIn("done", ev["output"])

    def test_wait_no_subcommand_returns_usage(self):
        ev = dispatch_input("wait", session_id="p")
        self.assertIn("Usage:", ev["output"])


class IterateUntilCompleteTests(unittest.TestCase):
    """`goals.iterate_until_complete` — re-dispatch until builder emits
    `<task-complete>` or hits the iteration cap."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "builder1")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def _runner_factory(self, responses):
        """Returns a runner that emits `responses[i]` for each iteration."""
        state = {"i": 0}

        def runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty", "error": None}
            response_text = responses[state["i"]]
            state["i"] = min(state["i"] + 1, len(responses) - 1)
            local_memory.mark_task(task["id"], status="done", result=response_text)
            return {"task": task, "outcome": "done", "error": None}

        return runner

    def test_completes_when_marker_appears(self):
        runner = self._runner_factory([
            "first turn — still working",
            "second turn — still working",
            "all done\n<task-complete>",
        ])
        summary = goals.iterate_until_complete(
            "planner",
            "build a thing",
            max_iterations=10,
            runner=runner,
        )
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["iterations"], 3)
        self.assertEqual(summary["stopped_reason"], "marker")
        self.assertEqual(summary["last_status"], "done")

    def test_stops_at_max_iterations_when_no_marker(self):
        runner = self._runner_factory([
            "still working",
            "still working",
            "still working",
            "still working",
        ])
        summary = goals.iterate_until_complete(
            "planner",
            "build",
            max_iterations=2,
            runner=runner,
        )
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["iterations"], 2)
        self.assertEqual(summary["stopped_reason"], "cap")

    def test_hard_cap_clamps_max_iterations(self):
        # Even if user asks for 100 iterations, hard cap pins it at 20.
        runner = self._runner_factory(["still working"])
        summary = goals.iterate_until_complete(
            "planner",
            "build",
            max_iterations=999,
            runner=runner,
        )
        self.assertLessEqual(summary["iterations"], goals.ITERATE_HARD_CAP)

    def test_stops_on_failed_status(self):
        runner = _runner_marks_failed
        summary = goals.iterate_until_complete(
            "planner",
            "doomed task",
            max_iterations=10,
            runner=runner,
        )
        self.assertFalse(summary["completed"])
        self.assertEqual(summary["iterations"], 1)
        self.assertEqual(summary["stopped_reason"], "failed")
        self.assertEqual(summary["last_status"], "failed")

    def test_marker_must_be_on_own_line(self):
        # Embedded in a sentence shouldn't trigger completion.
        runner = self._runner_factory([
            "I'm not emitting <task-complete> in a sentence — keep going",
            "Now I'm done\n<task-complete>",
        ])
        summary = goals.iterate_until_complete(
            "planner",
            "build",
            max_iterations=10,
            runner=runner,
        )
        self.assertTrue(summary["completed"])
        self.assertEqual(summary["iterations"], 2)

    def test_no_link_raises_value_error(self):
        with self.assertRaises(ValueError):
            goals.iterate_until_complete(
                "lonely_planner",  # no linked builder
                "build",
                runner=lambda _b: {"task": None, "outcome": "empty"},
            )

    def test_records_all_task_ids(self):
        runner = self._runner_factory([
            "still working",
            "still working",
            "all done\n<task-complete>",
        ])
        summary = goals.iterate_until_complete(
            "planner",
            "build",
            runner=runner,
        )
        self.assertEqual(len(summary["task_ids"]), 3)
        for tid in summary["task_ids"]:
            self.assertTrue(tid.startswith("task_"))


class IterateDispatcherCommandTests(unittest.TestCase):
    """End-to-end `iterate` dispatcher command tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "builder1")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_iterate_with_no_args_emits_invalid_argument(self):
        ev = dispatch_input("iterate", session_id="planner")
        self.assertEqual(ev["reason"], "invalid_argument")

    def test_iterate_invalid_max_emits_invalid_argument(self):
        ev = dispatch_input("iterate build a thing --max NaN", session_id="planner")
        self.assertEqual(ev["reason"], "invalid_argument")

    def test_iterate_no_link_emits_no_link(self):
        ev = dispatch_input("iterate build", session_id="lonely_planner")
        self.assertEqual(ev["reason"], "no_link")

    def test_iterate_unknown_builder_emits_unknown_builder(self):
        ev = dispatch_input("iterate @ghost build", session_id="planner")
        self.assertEqual(ev["reason"], "unknown_builder")

    def test_iterate_completes_via_marker(self):
        # Patch the internal runner so the iterate command can run without
        # touching real chat.
        responses = iter([
            "still working",
            "all done\n<task-complete>",
        ])

        def fake_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty", "error": None}
            response = next(responses)
            local_memory.mark_task(task["id"], status="done", result=response)
            return {"task": task, "outcome": "done", "error": None}

        with patch("app.services.command_dispatcher._run_one_inbox_task", side_effect=fake_runner):
            ev = dispatch_input("iterate @builder1 build a thing --max 5", session_id="planner")

        self.assertNotIn("reason", ev)
        self.assertEqual(ev["result"]["completed"], True)
        self.assertEqual(ev["result"]["iterations"], 2)
        self.assertEqual(ev["result"]["stopped_reason"], "marker")


if __name__ == "__main__":
    unittest.main()
