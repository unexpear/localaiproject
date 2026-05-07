"""Tests for CoWork Phase 1 (`docs/design/cowork-mode.md`).

Four layers:
  - Plan parser (`cowork.parse_plan_response`)
  - Storage helpers (`local_memory.{add,get,update,active}_cowork_run`)
  - Plan flow (`cowork.plan_run` → approve → status)
  - Execution flow (`cowork.execute_run` walks plan, calls runner per
    sub-task, runs synthesis at the end)

All tests inject canned planner / runner callables — no Ollama.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import cowork
from app.services.command_dispatcher import dispatch_input
from app.services.gateway.bus import default_bus, reset_default_bus_for_tests


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _canned_planner(*responses):
    """Returns a planner_call substitute that yields each response in order."""
    state = {"i": 0}

    def call(prompt, session_id):
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return {"response": responses[idx], "ok": True}

    return call


@contextmanager
def _patch_default_planner(*responses):
    """Patch `cowork._default_planner_call` for the duration of the block."""
    with patch.object(
        cowork, "_default_planner_call", side_effect=_canned_planner(*responses)
    ):
        yield


def _runner_marks_done_with(canned_response):
    """Sub-task runner: pops one queued task and marks it done with the
    given response text."""
    def runner(builder_session):
        task = local_memory.next_task(builder_session)
        if not task:
            return {"task": None, "outcome": "empty", "error": None}
        local_memory.mark_task(task["id"], status="done", result=canned_response)
        return {"task": task, "outcome": "done", "error": None}
    return runner


def _runner_marks_failed():
    def runner(builder_session):
        task = local_memory.next_task(builder_session)
        if not task:
            return {"task": None, "outcome": "empty", "error": None}
        local_memory.mark_task(task["id"], status="failed", result="boom")
        return {"task": task, "outcome": "failed", "error": "boom"}
    return runner


def _runner_per_subtask(responses_by_task_text):
    """Runner that returns different canned text based on the task text.

    `responses_by_task_text` is a dict of substring → response text. The
    first matching key wins; a missing key marks the task done with
    `(no canned response)`.
    """
    def runner(builder_session):
        task = local_memory.next_task(builder_session)
        if not task:
            return {"task": None, "outcome": "empty", "error": None}
        body = task["task"]
        for key, response in responses_by_task_text.items():
            if key in body:
                local_memory.mark_task(task["id"], status="done", result=response)
                return {"task": task, "outcome": "done", "error": None}
        local_memory.mark_task(task["id"], status="done", result="(no canned response)")
        return {"task": task, "outcome": "done", "error": None}
    return runner


# ============================================================
# Plan parser
# ============================================================

class CoworkParserTests(unittest.TestCase):
    def test_valid_plan_parses(self):
        text = (
            "Here is the plan:\n\n"
            "```json\n"
            "{\n"
            '  "goal": "set up a python lib",\n'
            '  "sub_tasks": [\n'
            '    {"id": "sub_1", "task": "create package", "builder": "coder", '
            '"depends_on": [], "acceptance": "files exist"},\n'
            '    {"id": "sub_2", "task": "write README", "builder": "writer", '
            '"depends_on": ["sub_1"], "acceptance": "README has title"}\n'
            "  ]\n"
            "}\n"
            "```"
        )
        plan = cowork.parse_plan_response(text)
        self.assertEqual(plan["goal"], "set up a python lib")
        self.assertEqual(len(plan["sub_tasks"]), 2)
        self.assertEqual(plan["sub_tasks"][1]["depends_on"], ["sub_1"])

    def test_no_fence_raises_invalid_plan_json(self):
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response("just some text, no JSON here")
        self.assertEqual(ctx.exception.reason, "invalid_plan_json")

    def test_malformed_json_raises_invalid_plan_json(self):
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response("```json\n{not real JSON}\n```")
        self.assertEqual(ctx.exception.reason, "invalid_plan_json")

    def test_empty_subtasks_raises_empty_plan(self):
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response('```json\n{"goal":"g","sub_tasks":[]}\n```')
        self.assertEqual(ctx.exception.reason, "empty_plan")

    def test_too_many_subtasks_raises(self):
        subs = [
            {"id": f"s{i}", "task": "t", "builder": "b", "depends_on": [], "acceptance": ""}
            for i in range(15)
        ]
        import json as _json
        text = "```json\n" + _json.dumps({"goal": "g", "sub_tasks": subs}) + "\n```"
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response(text)
        self.assertEqual(ctx.exception.reason, "too_many_subtasks")

    def test_unknown_dependency_raises(self):
        text = (
            '```json\n{"goal":"g","sub_tasks":['
            '{"id":"s1","task":"t","builder":"b","depends_on":["does_not_exist"],"acceptance":""}'
            ']}\n```'
        )
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response(text)
        self.assertEqual(ctx.exception.reason, "unknown_dependency")

    def test_cycle_in_plan_raises(self):
        text = (
            '```json\n{"goal":"g","sub_tasks":['
            '{"id":"s1","task":"t","builder":"b","depends_on":["s2"],"acceptance":""},'
            '{"id":"s2","task":"t","builder":"b","depends_on":["s1"],"acceptance":""}'
            ']}\n```'
        )
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response(text)
        self.assertEqual(ctx.exception.reason, "cycle_in_plan")

    def test_topological_order_respects_dependencies(self):
        plan = [
            {"id": "c", "task": "t", "builder": "b", "depends_on": ["a", "b"], "acceptance": ""},
            {"id": "a", "task": "t", "builder": "b", "depends_on": [], "acceptance": ""},
            {"id": "b", "task": "t", "builder": "b", "depends_on": ["a"], "acceptance": ""},
        ]
        order = cowork.topological_order(plan)
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("c"))


# ============================================================
# Storage layer
# ============================================================

class CoworkStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_add_run_round_trips(self):
        plan = [
            {"id": "s1", "task": "t1", "builder": "b1", "depends_on": [], "acceptance": ""}
        ]
        run = local_memory.add_cowork_run("planner", "build", plan)
        self.assertTrue(run["id"].startswith("cw_"))
        self.assertEqual(run["status"], "planning")
        self.assertEqual(run["session_id"], "planner")
        # Round-trip via get
        fetched = local_memory.get_cowork_run(run["id"])
        self.assertEqual(fetched["goal"], "build")

    def test_add_run_too_many_subtasks_raises(self):
        plan = [
            {"id": f"s{i}", "task": "t", "builder": "b", "depends_on": [], "acceptance": ""}
            for i in range(local_memory.MAX_SUBTASKS + 1)
        ]
        with self.assertRaises(ValueError):
            local_memory.add_cowork_run("planner", "build", plan)

    def test_active_run_only_returns_active_statuses(self):
        plan = [{"id": "s1", "task": "t", "builder": "b", "depends_on": [], "acceptance": ""}]
        run = local_memory.add_cowork_run("planner", "build", plan)
        self.assertEqual(local_memory.active_cowork_run("planner")["id"], run["id"])
        local_memory.update_cowork_run(run["id"], status="done")
        self.assertIsNone(local_memory.active_cowork_run("planner"))

    def test_update_run_rejects_unknown_field(self):
        plan = [{"id": "s1", "task": "t", "builder": "b", "depends_on": [], "acceptance": ""}]
        run = local_memory.add_cowork_run("planner", "build", plan)
        with self.assertRaises(ValueError):
            local_memory.update_cowork_run(run["id"], goal="cant change goal")

    def test_update_run_rejects_invalid_status(self):
        plan = [{"id": "s1", "task": "t", "builder": "b", "depends_on": [], "acceptance": ""}]
        run = local_memory.add_cowork_run("planner", "build", plan)
        with self.assertRaises(ValueError):
            local_memory.update_cowork_run(run["id"], status="banana")


# ============================================================
# plan_run + approval flow
# ============================================================

class CoworkPlanRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        local_memory.add_builder("planner", "writer")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    @staticmethod
    def _good_plan_text(goal="build a thing"):
        return (
            "Plan below.\n```json\n"
            f'{{"goal": "{goal}", "sub_tasks": ['
            '{"id":"sub_1","task":"first step","builder":"coder","depends_on":[],"acceptance":"done"},'
            '{"id":"sub_2","task":"second step","builder":"writer","depends_on":["sub_1"],"acceptance":"ok"}'
            "]}\n```"
        )

    def test_plan_run_with_canned_planner_persists_run(self):
        result = cowork.plan_run(
            "planner",
            "build a thing",
            planner_call=_canned_planner(self._good_plan_text()),
        )
        self.assertTrue(result["ok"])
        run = result["run"]
        self.assertEqual(run["status"], "planning")
        self.assertEqual(len(run["plan"]), 2)
        # Pending approval queued.
        pending = local_memory.get_pending_approval(session_id="planner")
        self.assertEqual(pending["kind"], "cowork_plan")
        self.assertEqual(pending["run_id"], run["id"])

    def test_plan_run_retries_once_on_malformed_response(self):
        result = cowork.plan_run(
            "planner",
            "build",
            planner_call=_canned_planner(
                "no fenced block here, sorry",
                self._good_plan_text(),
            ),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["run"]["status"], "planning")

    def test_plan_run_fails_after_two_malformed_responses(self):
        result = cowork.plan_run(
            "planner",
            "build",
            planner_call=_canned_planner(
                "garbage one",
                "garbage two",
            ),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_plan_json")
        # No run persisted.
        self.assertEqual(local_memory.list_cowork_runs("planner"), [])

    def test_plan_run_with_no_link_emits_no_link(self):
        result = cowork.plan_run(
            "lonely_session",
            "build",
            planner_call=_canned_planner(self._good_plan_text()),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_link")

    def test_plan_run_refuses_when_active_run_exists(self):
        cowork.plan_run(
            "planner",
            "first goal",
            planner_call=_canned_planner(self._good_plan_text("first goal")),
        )
        # Try to plan a second goal while the first is still planning.
        result = cowork.plan_run(
            "planner",
            "second goal",
            planner_call=_canned_planner(self._good_plan_text("second goal")),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cowork_in_progress")

    def test_plan_run_rejects_plan_with_unlinked_builder(self):
        bad_plan = (
            '```json\n{"goal":"g","sub_tasks":[{'
            '"id":"sub_1","task":"t","builder":"ghost_builder",'
            '"depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        result = cowork.plan_run(
            "planner",
            "build",
            planner_call=_canned_planner(bad_plan),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_builder")

    def test_approve_flips_run_to_approved(self):
        cowork.plan_run(
            "planner",
            "build",
            planner_call=_canned_planner(self._good_plan_text()),
        )
        # Use the dispatcher's approve handler so we test the integration.
        approve_event = dispatch_input("approve", session_id="planner")
        self.assertNotIn("reason", approve_event)
        active = local_memory.active_cowork_run("planner")
        self.assertEqual(active["status"], "approved")

    def test_deny_marks_run_cancelled(self):
        cowork.plan_run(
            "planner",
            "build",
            planner_call=_canned_planner(self._good_plan_text()),
        )
        deny_event = dispatch_input("deny", session_id="planner")
        self.assertNotIn("reason", deny_event)
        runs = local_memory.list_cowork_runs("planner")
        self.assertEqual(runs[0]["status"], "cancelled")


# ============================================================
# execute_run + synthesis
# ============================================================

class CoworkExecuteRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        local_memory.add_builder("planner", "writer")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    def _approved_run(self, goal="build a thing"):
        plan_text = (
            '```json\n{"goal": "' + goal + '", "sub_tasks": ['
            '{"id":"sub_1","task":"first step (coder)","builder":"coder",'
            '"depends_on":[],"acceptance":"done"},'
            '{"id":"sub_2","task":"second step (writer)","builder":"writer",'
            '"depends_on":["sub_1"],"acceptance":"ok"}'
            "]}\n```"
        )
        cowork.plan_run("planner", goal, planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        return local_memory.active_cowork_run("planner")

    def test_execute_run_walks_plan_in_order(self):
        run = self._approved_run()
        captured_events = []
        bus = default_bus()
        bus.subscribe(bus.ANY, lambda e: captured_events.append(e))

        runner = _runner_per_subtask({
            "first step": "did first step",
            "second step": "did second step",
        })
        with _patch_default_planner("Synthesis: all good."):
            final = cowork.execute_run(run["id"], runner=runner)

        self.assertEqual(final["status"], "done")
        self.assertEqual(final["subtask_results"]["sub_1"]["status"], "done")
        self.assertEqual(final["subtask_results"]["sub_2"]["status"], "done")
        self.assertEqual(final["synthesis"], "Synthesis: all good.")

        # Both sub-tasks fired subtask_done events; one completed event.
        kinds_with_event = [
            (e.kind, e.payload.get("event"))
            for e in captured_events if e.kind == cowork.KIND_COWORK
        ]
        self.assertIn(("cowork", "subtask_done"), kinds_with_event)
        self.assertIn(("cowork", "completed"), kinds_with_event)

    def test_execute_run_failed_subtask_marks_run_failed(self):
        run = self._approved_run()

        # Coder fails on first step; second step depends on first → skipped.
        runner = _runner_marks_failed()
        with _patch_default_planner("Synthesis: first step failed."):
            final = cowork.execute_run(run["id"], runner=runner)

        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["subtask_results"]["sub_1"]["status"], "failed")
        self.assertEqual(final["subtask_results"]["sub_2"]["status"], "skipped")

    def test_execute_run_in_wrong_state_returns_invalid_state(self):
        plan = [{"id": "s1", "task": "t", "builder": "coder", "depends_on": [], "acceptance": ""}]
        run = local_memory.add_cowork_run("planner", "build", plan)
        # Status is "planning" (not "approved"); execute should refuse.
        result = cowork.execute_run(run["id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_state")

    def test_execute_run_unknown_id_returns_unknown_target(self):
        result = cowork.execute_run("cw_does_not_exist")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_target")

    def test_synthesis_failure_recorded_in_text(self):
        run = self._approved_run()

        def boom_planner(prompt, session_id):
            raise RuntimeError("planner offline")

        runner = _runner_per_subtask({
            "first step": "ok",
            "second step": "ok",
        })
        with patch.object(cowork, "_default_planner_call", side_effect=boom_planner):
            final = cowork.execute_run(run["id"], runner=runner)

        # Sub-tasks succeeded; synthesis raised. Run is still "done"
        # (overall failure flag tracks sub-task status, not synthesis).
        self.assertEqual(final["status"], "done")
        self.assertIn("synthesis failed", final["synthesis"].lower())


# ============================================================
# Phase 3 — parallel execution
# ============================================================

class CoworkParallelExecutionTests(unittest.TestCase):
    """Verifies the ThreadPoolExecutor-backed execute_run semantics:
    independent sub-tasks dispatch concurrently, same-builder sub-tasks
    serialise via per-builder locks, dep failures still skip downstream,
    and cancellation aborts new submissions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        # 3 builders so we can test cross-builder parallelism.
        local_memory.add_builder("planner", "coder")
        local_memory.add_builder("planner", "writer")
        local_memory.add_builder("planner", "reviewer")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    def _approved_three_independent_run(self):
        """Plan with 3 independent sub-tasks (no dependencies)."""
        plan_text = (
            '```json\n{"goal":"three independent","sub_tasks":['
            '{"id":"sub_1","task":"task A","builder":"coder",'
            '"depends_on":[],"acceptance":""},'
            '{"id":"sub_2","task":"task B","builder":"writer",'
            '"depends_on":[],"acceptance":""},'
            '{"id":"sub_3","task":"task C","builder":"reviewer",'
            '"depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        cowork.plan_run("planner", "three independent",
                        planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        return local_memory.active_cowork_run("planner")

    def _approved_two_same_builder_run(self):
        """Plan with 2 independent sub-tasks both on coder (must serialise)."""
        plan_text = (
            '```json\n{"goal":"two on coder","sub_tasks":['
            '{"id":"sub_1","task":"first job","builder":"coder",'
            '"depends_on":[],"acceptance":""},'
            '{"id":"sub_2","task":"second job","builder":"coder",'
            '"depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        cowork.plan_run("planner", "two on coder",
                        planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        return local_memory.active_cowork_run("planner")

    def test_independent_subtasks_run_concurrently(self):
        """Three sub-tasks on three different builders overlap at least in
        pairs when max_workers >= 2.

        We can't test wall-clock time reliably (the JSON storage lock
        serialises the bookkeeping calls that bracket each sleep, so wall
        time on Windows can hover near serial even when sleeps overlap).
        Instead, we capture each sub-task's [start, end] span and assert
        that at least one pair of spans overlaps — that's the meaningful
        signal that parallelism happened.
        """
        run = self._approved_three_independent_run()

        sleep_per_task = 0.10
        timestamps = []
        ts_lock = threading.Lock()

        def slow_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty", "error": None}
            t_start = time.monotonic()
            time.sleep(sleep_per_task)
            t_end = time.monotonic()
            with ts_lock:
                timestamps.append((task["id"], builder_session, t_start, t_end))
            local_memory.mark_task(task["id"], status="done", result=f"done by {builder_session}")
            return {"task": task, "outcome": "done", "error": None}

        with _patch_default_planner("synthesis"):
            final = cowork.execute_run(run["id"], runner=slow_runner, max_workers=3)

        # All three sub-tasks done.
        self.assertEqual(final["status"], "done")
        self.assertEqual(len(timestamps), 3)

        # Check pairwise overlap. Two spans [a_start, a_end] and
        # [b_start, b_end] overlap iff a_start < b_end AND b_start < a_end.
        spans = [(start, end) for _id, _b, start, end in timestamps]
        overlapped_pairs = 0
        for i, (a_start, a_end) in enumerate(spans):
            for b_start, b_end in spans[i + 1:]:
                if a_start < b_end and b_start < a_end:
                    overlapped_pairs += 1
        self.assertGreater(
            overlapped_pairs, 0,
            f"Expected at least one pair of sub-task spans to overlap; "
            f"got spans={spans} (looks fully serial)",
        )

    def test_same_builder_subtasks_serialise(self):
        """Two sub-tasks on the SAME builder must NOT overlap, even with
        max_workers > 1, because they share an inbox."""
        run = self._approved_two_same_builder_run()

        sleep_per_task = 0.10
        timestamps = []
        ts_lock = threading.Lock()

        def slow_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty", "error": None}
            t_start = time.monotonic()
            time.sleep(sleep_per_task)
            t_end = time.monotonic()
            with ts_lock:
                timestamps.append((task["id"], t_start, t_end))
            local_memory.mark_task(task["id"], status="done", result="ok")
            return {"task": task, "outcome": "done", "error": None}

        with _patch_default_planner("synthesis"):
            final = cowork.execute_run(run["id"], runner=slow_runner, max_workers=4)

        self.assertEqual(final["status"], "done")
        self.assertEqual(len(timestamps), 2)

        # Sort by start time. The second sub-task's start MUST be >= the
        # first's end — that's what serialisation means.
        timestamps.sort(key=lambda t: t[1])
        first_end = timestamps[0][2]
        second_start = timestamps[1][1]
        self.assertGreaterEqual(
            second_start, first_end - 0.005,  # tolerate 5ms scheduler slop
            "Same-builder sub-tasks ran in parallel — per-builder lock failed",
        )

    def test_dep_failure_skips_downstream_with_parallelism(self):
        """sub_1 fails → sub_2 (depends on sub_1) auto-skips, even with
        a thread pool. The other independent sub-task still runs."""
        plan_text = (
            '```json\n{"goal":"dep test","sub_tasks":['
            '{"id":"sub_1","task":"will fail","builder":"coder",'
            '"depends_on":[],"acceptance":""},'
            '{"id":"sub_2","task":"depends on 1","builder":"writer",'
            '"depends_on":["sub_1"],"acceptance":""},'
            '{"id":"sub_3","task":"independent","builder":"reviewer",'
            '"depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        cowork.plan_run("planner", "dep test",
                        planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        run = local_memory.active_cowork_run("planner")

        def per_builder_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty", "error": None}
            if builder_session == "coder":
                local_memory.mark_task(task["id"], status="failed", result="boom")
                return {"task": task, "outcome": "failed", "error": "boom"}
            local_memory.mark_task(task["id"], status="done", result="ok")
            return {"task": task, "outcome": "done", "error": None}

        with _patch_default_planner("synthesis"):
            final = cowork.execute_run(run["id"], runner=per_builder_runner, max_workers=3)

        self.assertEqual(final["status"], "failed")  # at least one sub-task failed
        results = final["subtask_results"]
        self.assertEqual(results["sub_1"]["status"], "failed")
        self.assertEqual(results["sub_2"]["status"], "skipped")
        self.assertEqual(results["sub_3"]["status"], "done")

    def test_max_workers_clamped_to_hard_cap(self):
        """Asking for 100 workers can't actually spawn 100 — capped at MAX_WORKERS_CAP."""
        run = self._approved_three_independent_run()

        def quick_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty"}
            local_memory.mark_task(task["id"], status="done", result="x")
            return {"task": task, "outcome": "done"}

        with _patch_default_planner("synthesis"):
            # Should NOT raise even with absurd worker count.
            final = cowork.execute_run(
                run["id"], runner=quick_runner, max_workers=100,
            )
        self.assertEqual(final["status"], "done")

    def test_max_workers_one_falls_back_to_serial(self):
        """max_workers=1 forces serial execution (Phase-1-equivalent)."""
        run = self._approved_three_independent_run()

        sleep_per_task = 0.05
        starts = []
        ends = []
        log_lock = threading.Lock()

        def serial_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty"}
            t0 = time.monotonic()
            time.sleep(sleep_per_task)
            t1 = time.monotonic()
            with log_lock:
                starts.append(t0)
                ends.append(t1)
            local_memory.mark_task(task["id"], status="done", result="x")
            return {"task": task, "outcome": "done"}

        with _patch_default_planner("synthesis"):
            final = cowork.execute_run(run["id"], runner=serial_runner, max_workers=1)
        self.assertEqual(final["status"], "done")
        # Each sub-task starts after the previous ends → wall time
        # should be >= 3 * sleep_per_task.
        self.assertEqual(len(starts), 3)
        # Sort and verify non-overlap.
        spans = sorted(zip(starts, ends), key=lambda s: s[0])
        for prev, curr in zip(spans, spans[1:]):
            self.assertGreaterEqual(curr[0], prev[1] - 0.005)

    def test_cancellation_mid_run_stops_remaining_subtasks(self):
        """Setting cancel_event mid-run prevents any further sub-tasks from
        starting; in-flight ones complete."""
        run = self._approved_three_independent_run()
        cancel_event = threading.Event()

        ran_count = {"n": 0}
        ran_lock = threading.Lock()

        def cancelling_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty"}
            with ran_lock:
                ran_count["n"] += 1
            # First sub-task to run signals cancel; later ones see the
            # event and skip.
            if ran_count["n"] == 1:
                cancel_event.set()
            local_memory.mark_task(task["id"], status="done", result="x")
            return {"task": task, "outcome": "done"}

        with _patch_default_planner("synthesis"):
            final = cowork.execute_run(
                run["id"],
                runner=cancelling_runner,
                cancel_event=cancel_event,
                max_workers=1,  # serial to make the assertion deterministic
            )

        # With max_workers=1 + cancel after first sub-task, the other 2
        # never get queued — they end up in the run as having no result.
        self.assertEqual(final["status"], "cancelled")

    def test_dispatcher_run_with_workers_flag(self):
        """`cowork run <id> --workers 2` parses + runs."""
        run = self._approved_three_independent_run()

        def fast_runner(builder_session):
            task = local_memory.next_task(builder_session)
            if not task:
                return {"task": None, "outcome": "empty"}
            local_memory.mark_task(task["id"], status="done", result="ok")
            return {"task": task, "outcome": "done"}

        with patch("app.services.command_dispatcher._run_one_inbox_task",
                   side_effect=fast_runner):
            with _patch_default_planner("synthesis"):
                ev = dispatch_input(
                    f"cowork run {run['id']} --workers 2", session_id="planner",
                )
        self.assertNotIn("reason", ev)
        self.assertEqual(ev["result"]["status"], "done")

    def test_dispatcher_run_with_invalid_workers_flag(self):
        run = self._approved_three_independent_run()
        ev = dispatch_input(
            f"cowork run {run['id']} --workers banana", session_id="planner",
        )
        self.assertEqual(ev.get("reason"), "invalid_argument")


# ============================================================
# Phase 5 — sub-task lifecycle hooks
# ============================================================

class CoworkLifecycleHookTests(unittest.TestCase):
    """`before` / `after` hooks per sub-task.

    `before` failures abort the sub-task with `precondition_failed`;
    `after` failures attach an `after_hook_warning` to the sub-task
    result but DON'T flip status (the work was already done).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        local_memory.add_builder("planner", "writer")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    def _seed_run_with_hook(self, sub_id_hook_map):
        """Build a 1-2 sub-task plan with the given before/after hooks
        and approve it. ``sub_id_hook_map`` is e.g.
        ``{"sub_1": {"before": "doctor"}, "sub_2": {"after": "tasks"}}``."""
        subs = []
        for sid, hooks in sub_id_hook_map.items():
            subs.append({
                "id": sid,
                "task": f"do work for {sid}",
                "builder": "coder" if sid == "sub_1" else "writer",
                "depends_on": [],
                "acceptance": "",
                **hooks,
            })
        plan_text = (
            '```json\n{"goal":"hooks","sub_tasks":'
            + json.dumps(subs)
            + "}\n```"
        )
        cowork.plan_run("planner", "hooks",
                        planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        return local_memory.active_cowork_run("planner")

    def test_before_hook_returning_reason_aborts_subtask(self):
        run = self._seed_run_with_hook({
            "sub_1": {"before": "this-command-does-not-exist 123"},
        })
        # The non-existent command falls through to chat → produces a
        # reason. We patch dispatch_input ITSELF for the hook only.
        original_dispatch = cowork.local_memory  # placeholder

        from app.services import command_dispatcher as cd

        real_dispatch = cd.dispatch_input

        def fake_dispatch(text, session_id=None):
            if "this-command-does-not-exist" in text:
                return {
                    "action": "noop", "session_id": session_id,
                    "output": "policy refusal",
                    "reason": "invalid_argument",
                }
            return real_dispatch(text, session_id=session_id)

        runner = _runner_marks_done_with("ok")

        with patch.object(cd, "dispatch_input", side_effect=fake_dispatch):
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(run["id"], runner=runner)

        self.assertEqual(
            final["subtask_results"]["sub_1"]["status"], "failed",
        )
        self.assertIn(
            "precondition_failed",
            final["subtask_results"]["sub_1"]["result"],
        )
        # No actual builder task was queued (since the before hook
        # aborted before dispatch).
        self.assertEqual(final["task_ids"], [])

    def test_after_hook_warning_attached_but_status_stays_done(self):
        run = self._seed_run_with_hook({
            "sub_1": {"after": "after-hook-cmd 999"},
        })
        from app.services import command_dispatcher as cd
        real_dispatch = cd.dispatch_input

        def fake_dispatch(text, session_id=None):
            if "after-hook-cmd" in text:
                return {
                    "action": "noop", "session_id": session_id,
                    "output": "linter complained: x",
                    "reason": "internal_error",
                }
            return real_dispatch(text, session_id=session_id)

        runner = _runner_marks_done_with("ok")

        with patch.object(cd, "dispatch_input", side_effect=fake_dispatch):
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(run["id"], runner=runner)

        # Sub-task itself succeeded; after hook warning attached.
        self.assertEqual(final["subtask_results"]["sub_1"]["status"], "done")
        self.assertIn(
            "linter complained",
            final["subtask_results"]["sub_1"].get("after_hook_warning", ""),
        )
        # Run overall: done (no failures).
        self.assertEqual(final["status"], "done")

    def test_no_hooks_means_no_dispatcher_calls_for_hooks(self):
        # Sanity: a plan with no before/after still works (regression
        # guard against the new code adding spurious calls).
        run = self._seed_run_with_hook({"sub_1": {}})
        runner = _runner_marks_done_with("ok")
        with _patch_default_planner("synthesis"):
            final = cowork.execute_run(run["id"], runner=runner)
        self.assertEqual(final["subtask_results"]["sub_1"]["status"], "done")

    def test_before_hook_failure_skips_dependents(self):
        # sub_1 has a failing before hook; sub_2 depends on sub_1 →
        # auto-skip with dependency_failed.
        plan_text = (
            '```json\n{"goal":"chain","sub_tasks":['
            '{"id":"sub_1","task":"x","builder":"coder","depends_on":[],'
            '"acceptance":"","before":"refuse-command 1"},'
            '{"id":"sub_2","task":"y","builder":"writer","depends_on":["sub_1"],'
            '"acceptance":""}'
            "]}\n```"
        )
        cowork.plan_run("planner", "chain",
                        planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        run = local_memory.active_cowork_run("planner")

        from app.services import command_dispatcher as cd
        real_dispatch = cd.dispatch_input

        def fake_dispatch(text, session_id=None):
            if "refuse-command" in text:
                return {"action": "noop", "session_id": session_id,
                        "output": "no", "reason": "invalid_argument"}
            return real_dispatch(text, session_id=session_id)

        with patch.object(cd, "dispatch_input", side_effect=fake_dispatch):
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(run["id"], runner=_runner_marks_done_with("ok"))

        self.assertEqual(final["subtask_results"]["sub_1"]["status"], "failed")
        self.assertEqual(final["subtask_results"]["sub_2"]["status"], "skipped")


# ============================================================
# Phase 5 — branch strategy
# ============================================================

class CoworkBranchStrategyParserTests(unittest.TestCase):
    def test_default_strategy_is_head(self):
        text = (
            '```json\n{"goal":"x","sub_tasks":['
            '{"id":"s1","task":"t","builder":"b","depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        plan = cowork.parse_plan_response(text)
        self.assertEqual(plan["branch_strategy"], "head")
        self.assertEqual(plan["branch_name"], "")

    def test_branch_strategy_branch_requires_branch_name(self):
        text = (
            '```json\n{"goal":"x","branch_strategy":"branch","sub_tasks":['
            '{"id":"s1","task":"t","builder":"b","depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        with self.assertRaises(cowork.CoworkPlanError) as ctx:
            cowork.parse_plan_response(text)
        self.assertEqual(ctx.exception.reason, "invalid_plan_json")

    def test_branch_strategy_unknown_value_raises(self):
        text = (
            '```json\n{"goal":"x","branch_strategy":"chaos","sub_tasks":['
            '{"id":"s1","task":"t","builder":"b","depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        with self.assertRaises(cowork.CoworkPlanError):
            cowork.parse_plan_response(text)

    def test_merge_to_head_does_not_require_branch_name(self):
        text = (
            '```json\n{"goal":"x","branch_strategy":"merge-to-head","sub_tasks":['
            '{"id":"s1","task":"t","builder":"b","depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        plan = cowork.parse_plan_response(text)
        self.assertEqual(plan["branch_strategy"], "merge-to-head")


class CoworkBranchStrategyExecutionTests(unittest.TestCase):
    """Execute a run with branch strategy, mocking cowork_git so no
    real git process spawns. Verifies the state machine + git_state
    bookkeeping end-to-end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        # Pretend we have a workspace pinned (any path works for the
        # mock — cowork_git's is_git_repo is patched).
        self.workspace = self._tmp.name
        local_memory.set_workspace(session_id="planner", workspace_path=self.workspace)
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    def _approved_run(self, branch_strategy="head", branch_name=""):
        plan_subs = [{
            "id": "sub_1", "task": "do work", "builder": "coder",
            "depends_on": [], "acceptance": "",
        }]
        plan_obj = {"goal": "g", "sub_tasks": plan_subs,
                    "branch_strategy": branch_strategy,
                    "branch_name": branch_name}
        plan_text = "```json\n" + json.dumps(plan_obj) + "\n```"
        cowork.plan_run("planner", "g", planner_call=_canned_planner(plan_text))
        dispatch_input("approve", session_id="planner")
        return local_memory.active_cowork_run("planner")

    def test_head_strategy_does_not_call_git(self):
        from app.services import cowork_git
        with patch.object(cowork_git, "is_git_repo", return_value=False) as m_repo:
            run = self._approved_run(branch_strategy="head")
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(
                    run["id"], runner=_runner_marks_done_with("ok"),
                )
            # `head` strategy never even checks if the workspace is
            # a git repo.
            self.assertEqual(m_repo.call_count, 0)
            self.assertEqual(final["status"], "done")

    def test_merge_to_head_creates_branch_and_merges_on_success(self):
        from app.services import cowork_git
        seq = []

        def fake_run_git(workspace, *args, timeout=30.0):
            seq.append(args)
            stdout = ""
            if args == ("rev-parse", "--is-inside-work-tree"):
                stdout = "true"
            elif args == ("rev-parse", "--abbrev-ref", "HEAD"):
                stdout = "main"
            elif args[:1] == ("status",):
                stdout = ""  # clean tree
            elif args[:2] == ("show-ref", "--verify"):
                # branch doesn't exist yet
                return cowork_git.GitResult(
                    ok=False, returncode=1, stdout="", stderr="",
                    args=("git", *args),
                )
            return cowork_git.GitResult(
                ok=True, returncode=0, stdout=stdout, stderr="",
                args=("git", *args),
            )

        with patch.object(cowork_git, "_run_git", side_effect=fake_run_git):
            run = self._approved_run(branch_strategy="merge-to-head")
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(
                    run["id"], runner=_runner_marks_done_with("ok"),
                )

        self.assertEqual(final["status"], "done")
        git_state = final.get("git_state") or {}
        self.assertEqual(git_state.get("strategy"), "merge-to-head")
        self.assertEqual(git_state.get("original_branch"), "main")
        self.assertTrue(
            (git_state.get("target_branch") or "").startswith("lai/planner/cw_"),
        )
        self.assertEqual(git_state.get("merge_status"), "merged-into main")
        # Some checkout/merge commands were issued.
        flat_calls = [a for tup in seq for a in tup]
        self.assertIn("merge", flat_calls)

    def test_merge_to_head_leaves_branch_when_subtasks_failed(self):
        from app.services import cowork_git

        def fake_run_git(workspace, *args, timeout=30.0):
            stdout = ""
            if args == ("rev-parse", "--is-inside-work-tree"):
                stdout = "true"
            elif args == ("rev-parse", "--abbrev-ref", "HEAD"):
                stdout = "main"
            elif args[:2] == ("show-ref", "--verify"):
                return cowork_git.GitResult(
                    ok=False, returncode=1, stdout="", stderr="",
                    args=("git", *args),
                )
            return cowork_git.GitResult(
                ok=True, returncode=0, stdout=stdout, stderr="",
                args=("git", *args),
            )

        with patch.object(cowork_git, "_run_git", side_effect=fake_run_git):
            run = self._approved_run(branch_strategy="merge-to-head")
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(
                    run["id"], runner=_runner_marks_failed(),
                )

        self.assertEqual(final["status"], "failed")
        git_state = final.get("git_state") or {}
        self.assertIn("left-on-branch", git_state.get("merge_status", ""))

    def test_branch_strategy_uses_named_branch(self):
        from app.services import cowork_git

        def fake_run_git(workspace, *args, timeout=30.0):
            stdout = ""
            if args == ("rev-parse", "--is-inside-work-tree"):
                stdout = "true"
            elif args == ("rev-parse", "--abbrev-ref", "HEAD"):
                stdout = "main"
            elif args[:2] == ("show-ref", "--verify"):
                return cowork_git.GitResult(
                    ok=False, returncode=1, stdout="", stderr="",
                    args=("git", *args),
                )
            return cowork_git.GitResult(
                ok=True, returncode=0, stdout=stdout, stderr="",
                args=("git", *args),
            )

        with patch.object(cowork_git, "_run_git", side_effect=fake_run_git):
            run = self._approved_run(
                branch_strategy="branch", branch_name="feat/cool-thing",
            )
            with _patch_default_planner("synthesis"):
                final = cowork.execute_run(
                    run["id"], runner=_runner_marks_done_with("ok"),
                )

        git_state = final.get("git_state") or {}
        self.assertEqual(git_state.get("target_branch"), "feat/cool-thing")
        # `branch` strategy never merges.
        self.assertEqual(git_state.get("merge_status"), "left-on-branch")

    def test_setup_fails_when_workspace_not_git_repo(self):
        from app.services import cowork_git
        with patch.object(cowork_git, "is_git_repo", return_value=False):
            run = self._approved_run(branch_strategy="merge-to-head")
            with _patch_default_planner("synthesis"):
                result = cowork.execute_run(
                    run["id"], runner=_runner_marks_done_with("ok"),
                )
        # execute_run returns an error dict on setup failure
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("ok"), False)
        self.assertIn("not a git repository", result.get("output", ""))


# ============================================================
# Dispatcher command surface
# ============================================================

class CoworkDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_cowork_status_with_no_runs(self):
        ev = dispatch_input("cowork status", session_id="planner")
        self.assertIn("No cowork runs", ev["output"])

    def test_cowork_with_no_args_falls_back_to_status(self):
        # Bare `cowork` is a friendly default — show what's happening.
        # Refusing would just send users to type `cowork status` anyway.
        ev = dispatch_input("cowork", session_id="planner")
        self.assertNotIn("reason", ev)
        self.assertIn("No cowork runs", ev["output"])

    def test_cowork_run_unknown_id_emits_unknown_target(self):
        ev = dispatch_input("cowork run cw_does_not_exist", session_id="planner")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_cowork_cancel_with_no_active_run_emits_invalid_argument(self):
        ev = dispatch_input("cowork cancel", session_id="planner")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_cowork_plan_command_routes_through_planner(self):
        good_plan = (
            '```json\n{"goal":"build","sub_tasks":[{'
            '"id":"sub_1","task":"do work","builder":"coder",'
            '"depends_on":[],"acceptance":""}'
            "]}\n```"
        )
        with patch.object(
            cowork, "_default_planner_call",
            side_effect=_canned_planner(good_plan),
        ):
            ev = dispatch_input("cowork plan \"build\"", session_id="planner")
        self.assertEqual(ev.get("reason"), "approval_required")
        self.assertIn("approval required", ev["output"].lower())


if __name__ == "__main__":
    unittest.main()
