"""Tests for the planner/builder coordination layer.

Two roles:
  - planner: holds task list, dispatches work
  - builder: receives queued tasks, runs them as chat turns, reports back

The test suite exercises the data layer (set_link / add_task / next_task /
mark_task) and the dispatcher commands (link / dispatch / inbox / tasks)
against a temp-backed JsonStorage. handle_chat is mocked everywhere so
no Ollama call is made.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services.command_dispatcher import (
    _handle_dispatch_command,
    _handle_inbox_command,
    _handle_link_command,
    _handle_tasks_command,
    _RESERVED_CMD_NAMES,
    dispatch_input,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# ============================================================
# Data layer
# ============================================================

class CoordinationStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_add_builder_round_trips_in_both_directions(self):
        local_memory.add_builder("planner", "builder")
        self.assertEqual(local_memory.get_builders("planner"), ["builder"])
        self.assertEqual(local_memory.get_planners("builder"), ["planner"])

    def test_add_builder_creates_both_sessions(self):
        local_memory.add_builder("p1", "b1")
        sessions = local_memory.list_sessions()
        self.assertIn("p1", sessions)
        self.assertIn("b1", sessions)

    def test_add_builder_appends_not_replaces(self):
        # NEW behaviour for fan-out: a planner can be linked to multiple builders.
        local_memory.add_builder("planner", "builder1")
        local_memory.add_builder("planner", "builder2")
        self.assertEqual(local_memory.get_builders("planner"), ["builder1", "builder2"])

    def test_add_builder_is_idempotent(self):
        local_memory.add_builder("p", "b")
        local_memory.add_builder("p", "b")  # second call does nothing
        self.assertEqual(local_memory.get_builders("p"), ["b"])

    def test_add_builder_rejects_self_link(self):
        with self.assertRaises(ValueError):
            local_memory.add_builder("solo", "solo")

    def test_add_builder_rejects_empty(self):
        with self.assertRaises(ValueError):
            local_memory.add_builder("", "builder")
        with self.assertRaises(ValueError):
            local_memory.add_builder("planner", "  ")

    def test_remove_builder_drops_one(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        self.assertTrue(local_memory.remove_builder("p", "b1"))
        self.assertEqual(local_memory.get_builders("p"), ["b2"])
        # Removing again is a no-op
        self.assertFalse(local_memory.remove_builder("p", "b1"))

    def test_clear_builders_drops_all(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        self.assertTrue(local_memory.clear_builders("p"))
        self.assertEqual(local_memory.get_builders("p"), [])
        self.assertFalse(local_memory.clear_builders("p"))

    def test_get_planners_returns_every_planner_for_a_builder(self):
        local_memory.add_builder("p1", "shared-builder")
        local_memory.add_builder("p2", "shared-builder")
        self.assertEqual(local_memory.get_planners("shared-builder"), ["p1", "p2"])

    def test_get_link_partners_combined_view(self):
        local_memory.add_builder("planner", "b1")
        local_memory.add_builder("planner", "b2")
        view = local_memory.get_link_partners("planner")
        self.assertEqual(view["as_planner"], ["b1", "b2"])
        self.assertEqual(view["as_builder"], [])
        view2 = local_memory.get_link_partners("b1")
        self.assertEqual(view2["as_builder"], ["planner"])

    def test_get_link_legacy_helper_returns_first(self):
        # Backward-compat helper used by older callers.
        local_memory.add_builder("planner", "b1")
        local_memory.add_builder("planner", "b2")
        self.assertEqual(local_memory.get_link("planner"), "b1")
        self.assertEqual(local_memory.get_link("b2"), "planner")
        self.assertIsNone(local_memory.get_link("nobody"))

    def test_set_link_alias_still_works(self):
        # Old API alias — adds (does not replace) for forward-compat.
        local_memory.set_link("p", "b1")
        local_memory.set_link("p", "b2")
        self.assertEqual(local_memory.get_builders("p"), ["b1", "b2"])

    def test_legacy_string_link_in_storage_is_normalized(self):
        # Simulate Phase-1 single-string link being read off disk.
        def fn(data):
            local_memory._ensure_coordination_bucket(data)
            data["coordination"]["links"]["legacy-planner"] = "old-builder"
        local_memory._mutate(fn)
        # On read, the legacy string should be promoted to a single-element list.
        self.assertEqual(local_memory.get_builders("legacy-planner"), ["old-builder"])


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "builder")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_add_task_creates_queued_entry(self):
        task = local_memory.add_task("planner", "Do the thing")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["task"], "Do the thing")
        self.assertEqual(task["builder_session"], "builder")
        self.assertEqual(task["planner_session"], "planner")
        self.assertTrue(task["id"].startswith("task_"))

    def test_add_task_rejects_unlinked_planner(self):
        with self.assertRaises(ValueError):
            local_memory.add_task("orphan", "anything")

    def test_add_task_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            local_memory.add_task("planner", "   ")

    def test_inbox_returns_only_queued_for_builder(self):
        local_memory.add_task("planner", "task A")
        local_memory.add_task("planner", "task B")
        inbox = local_memory.get_inbox("builder")
        self.assertEqual([t["task"] for t in inbox], ["task A", "task B"])
        # Builder=='planner' inbox is empty
        self.assertEqual(local_memory.get_inbox("planner"), [])

    def test_next_task_pops_in_fifo_order_and_marks_in_progress(self):
        local_memory.add_task("planner", "A")
        local_memory.add_task("planner", "B")
        first = local_memory.next_task("builder")
        self.assertEqual(first["task"], "A")
        self.assertEqual(first["status"], "in_progress")
        self.assertIsNotNone(first["started_at"])
        # The next pop returns B
        second = local_memory.next_task("builder")
        self.assertEqual(second["task"], "B")
        # And the inbox is now empty
        self.assertEqual(local_memory.next_task("builder"), None)

    def test_in_progress_task_for_returns_active_task(self):
        local_memory.add_task("planner", "X")
        self.assertIsNone(local_memory.in_progress_task_for("builder"))
        task = local_memory.next_task("builder")
        active = local_memory.in_progress_task_for("builder")
        self.assertEqual(active["id"], task["id"])

    def test_mark_task_done_sets_result_and_completed_at(self):
        local_memory.add_task("planner", "X")
        task = local_memory.next_task("builder")
        updated = local_memory.mark_task(task["id"], status="done", result="all good", execution_id="exe_1")
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["result"], "all good")
        self.assertEqual(updated["execution_id"], "exe_1")
        self.assertIsNotNone(updated["completed_at"])

    def test_mark_task_failed(self):
        local_memory.add_task("planner", "X")
        task = local_memory.next_task("builder")
        updated = local_memory.mark_task(task["id"], status="failed", result="oops")
        self.assertEqual(updated["status"], "failed")

    def test_mark_task_unknown_status_raises(self):
        with self.assertRaises(ValueError):
            local_memory.mark_task("task_x", status="exploded")

    def test_list_tasks_filters_by_session_and_status(self):
        local_memory.add_task("planner", "A")
        local_memory.add_task("planner", "B")
        t = local_memory.next_task("builder")
        local_memory.mark_task(t["id"], status="done", result="r")
        # Both planner and builder see both tasks
        self.assertEqual(len(local_memory.list_tasks("planner")), 2)
        self.assertEqual(len(local_memory.list_tasks("builder")), 2)
        # Status filter narrows
        done = local_memory.list_tasks("builder", status="done")
        self.assertEqual(len(done), 1)
        # Newest-first ordering
        all_tasks = local_memory.list_tasks("builder")
        self.assertEqual(all_tasks[0]["task"], "B")


# ============================================================
# Dispatcher commands
# ============================================================

class LinkDispatchInboxCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_link_command_creates_link(self):
        ev = dispatch_input("link builder1", session_id="planner1")
        self.assertEqual(ev["action"], "link")
        self.assertIn("planner1", ev["output"])
        self.assertIn("builder1", ev["output"])
        self.assertEqual(local_memory.get_link("planner1"), "builder1")

    def test_link_status_with_no_link(self):
        ev = dispatch_input("link", session_id="solo")
        # Multi-builder wording: any of these phrasings is acceptable.
        self.assertTrue(
            "No links" in ev["output"] or "No link" in ev["output"],
            f"unexpected: {ev['output']!r}",
        )

    def test_link_clear_drops_link(self):
        local_memory.add_builder("p", "b")
        ev = dispatch_input("link clear", session_id="p")
        self.assertIn("Cleared all builders", ev["output"])
        self.assertEqual(local_memory.get_builders("p"), [])

    def test_link_status_shows_partner_and_counts(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "X")
        ev = dispatch_input("link status", session_id="p")
        # New wording: "As planner: 'p' -> 1 builder(s): b"
        self.assertIn("'p' ->", ev["output"])
        self.assertIn("b", ev["output"])
        self.assertIn("queued=1", ev["output"])

    def test_dispatch_without_link_returns_error(self):
        ev = dispatch_input("dispatch hello", session_id="orphan")
        self.assertIn("not linked", ev["output"].lower())

    def test_dispatch_queues_a_task(self):
        local_memory.add_builder("p", "b")
        ev = dispatch_input("dispatch refactor the router", session_id="p")
        self.assertEqual(ev["action"], "dispatch")
        self.assertIn("Queued task_", ev["output"])
        self.assertIn("'b'", ev["output"])
        self.assertIn("refactor the router", ev["output"])
        self.assertEqual(len(local_memory.get_inbox("b")), 1)

    def test_dispatch_without_text_returns_usage(self):
        local_memory.add_builder("p", "b")
        ev = dispatch_input("dispatch", session_id="p")
        self.assertIn("Usage:", ev["output"])

    def test_inbox_lists_queued_tasks(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "first")
        local_memory.add_task("p", "second")
        ev = dispatch_input("inbox", session_id="b")
        self.assertIn("Queued (2)", ev["output"])
        self.assertIn("first", ev["output"])
        self.assertIn("second", ev["output"])

    def test_inbox_empty(self):
        ev = dispatch_input("inbox", session_id="someone")
        self.assertIn("Inbox is empty", ev["output"])

    def test_inbox_skip_drops_next_task(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "throw away")
        ev = dispatch_input("inbox skip", session_id="b")
        self.assertIn("Skipped", ev["output"])
        # The skipped task is gone from the queue
        self.assertEqual(local_memory.get_inbox("b"), [])
        # And its status is "skipped" in the full task list
        skipped = local_memory.list_tasks("b", status="skipped")
        self.assertEqual(len(skipped), 1)

    def test_inbox_next_runs_chat_and_marks_done(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "Explain workspace approval gating")
        fake_response = {
            "ok": True,
            "session_id": "b",
            "response": "Workspace reads need explicit approval.",
            "fallback_used": False,
            "approval_required": False,
            "compaction_performed": False,
            "execution_id": "exe_42",
        }
        with patch("app.services.command_dispatcher.handle_chat", return_value=fake_response):
            ev = dispatch_input("inbox next", session_id="b")
        self.assertIn("ran on 'b'", ev["output"])
        # Task is marked done with the response captured
        done = local_memory.list_tasks("b", status="done")
        self.assertEqual(len(done), 1)
        self.assertIn("Workspace reads", done[0]["result"])
        self.assertEqual(done[0]["execution_id"], "exe_42")
        # The planner sees a [task ... DONE] system note in its history
        history = local_memory.get_history(session_id="p")
        self.assertTrue(any(
            m["role"] == "system" and "DONE" in m["content"] for m in history
        ))

    def test_inbox_next_paused_on_workspace_approval(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "read app/services/router.py")
        pending = {"kind": "read", "origin": "chat", "workspace_path": "C:/proj", "paths": ["app/services/router.py"]}
        chat_event = {
            "ok": False,
            "approval_required": True,
            "pending_action": pending,
            "response": "[approval required]",
            "fallback_used": False,
            "compaction_performed": False,
        }
        with patch("app.services.command_dispatcher.handle_chat", return_value=chat_event):
            ev = dispatch_input("inbox next", session_id="b")
        self.assertIn("approval required", ev["output"].lower())
        # Task is still in_progress (awaiting approval)
        in_prog = local_memory.in_progress_task_for("b")
        self.assertIsNotNone(in_prog)
        self.assertEqual(in_prog["result"], "(awaiting workspace approval)")

    def test_tasks_command_lists_history(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "A")
        local_memory.add_task("p", "B")
        t = local_memory.next_task("b")
        local_memory.mark_task(t["id"], status="done", result="r")
        ev = dispatch_input("tasks", session_id="p")
        self.assertEqual(ev["action"], "tasks")
        self.assertIn("done", ev["output"])
        self.assertIn("queued", ev["output"])

    def test_tasks_command_status_filter(self):
        local_memory.add_builder("p", "b")
        local_memory.add_task("p", "A")
        ev = dispatch_input("tasks queued", session_id="p")
        self.assertIn("queued", ev["output"])

    def test_tasks_command_invalid_status(self):
        ev = dispatch_input("tasks exploded", session_id="p")
        self.assertIn("Unknown status", ev["output"])

    def test_reserved_set_blocks_link_dispatch_inbox_tasks(self):
        for name in ("link", "dispatch", "inbox", "tasks"):
            self.assertIn(name, _RESERVED_CMD_NAMES)


# ============================================================
# Multi-builder fan-out
# ============================================================

class MultiBuilderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    # ---- add_task with multiple builders ----

    def test_add_task_explicit_builder_routes_correctly(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        task = local_memory.add_task("p", "do thing", builder="b2")
        self.assertEqual(task["builder_session"], "b2")

    def test_add_task_unknown_explicit_builder_raises(self):
        local_memory.add_builder("p", "b1")
        with self.assertRaises(ValueError):
            local_memory.add_task("p", "do thing", builder="ghost")

    def test_add_task_no_builder_with_multiple_linked_raises(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        with self.assertRaises(ValueError) as cm:
            local_memory.add_task("p", "do thing")
        self.assertIn("dispatch_to_all", str(cm.exception))

    def test_add_task_no_builder_with_one_linked_uses_it(self):
        local_memory.add_builder("p", "only-builder")
        task = local_memory.add_task("p", "do thing")
        self.assertEqual(task["builder_session"], "only-builder")

    # ---- dispatch_to_all ----

    def test_dispatch_to_all_creates_one_task_per_builder(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        local_memory.add_builder("p", "b3")
        tasks = local_memory.dispatch_to_all("p", "fanned task")
        self.assertEqual(len(tasks), 3)
        self.assertEqual({t["builder_session"] for t in tasks}, {"b1", "b2", "b3"})
        self.assertTrue(all(t["task"] == "fanned task" for t in tasks))
        self.assertTrue(all(t["status"] == "queued" for t in tasks))

    def test_dispatch_to_all_with_no_builders_raises(self):
        with self.assertRaises(ValueError):
            local_memory.dispatch_to_all("solo", "anything")

    def test_dispatch_to_all_with_one_builder_creates_one_task(self):
        local_memory.add_builder("p", "b1")
        tasks = local_memory.dispatch_to_all("p", "x")
        self.assertEqual(len(tasks), 1)

    def test_each_builder_inbox_only_sees_its_own(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        local_memory.dispatch_to_all("p", "shared task")
        # b1 sees only its own copy
        b1_inbox = local_memory.get_inbox("b1")
        b2_inbox = local_memory.get_inbox("b2")
        self.assertEqual(len(b1_inbox), 1)
        self.assertEqual(len(b2_inbox), 1)
        # And they're distinct task IDs
        self.assertNotEqual(b1_inbox[0]["id"], b2_inbox[0]["id"])

    # ---- dispatcher integration ----

    def test_dispatch_bare_form_fans_out_to_all_linked_builders(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        ev = dispatch_input("dispatch refactor router", session_id="p")
        self.assertEqual(ev["action"], "dispatch")
        self.assertIn("Fanned out", ev["output"])
        self.assertIn("b1", ev["output"])
        self.assertIn("b2", ev["output"])
        # And both inboxes received a task
        self.assertEqual(len(local_memory.get_inbox("b1")), 1)
        self.assertEqual(len(local_memory.get_inbox("b2")), 1)

    def test_dispatch_at_builder_targets_specific_builder(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        ev = dispatch_input("dispatch @b2 do the thing", session_id="p")
        self.assertEqual(ev["action"], "dispatch")
        self.assertIn("for 'b2'", ev["output"])
        self.assertEqual(len(local_memory.get_inbox("b1")), 0)
        self.assertEqual(len(local_memory.get_inbox("b2")), 1)
        self.assertEqual(local_memory.get_inbox("b2")[0]["task"], "do the thing")

    def test_dispatch_at_unlinked_builder_returns_error(self):
        local_memory.add_builder("p", "b1")
        ev = dispatch_input("dispatch @ghost do something", session_id="p")
        self.assertIn("not linked", ev["output"].lower())

    def test_dispatch_at_with_no_task_text_returns_usage(self):
        local_memory.add_builder("p", "b1")
        ev = dispatch_input("dispatch @b1", session_id="p")
        self.assertIn("Usage:", ev["output"])

    def test_dispatch_with_one_builder_does_not_say_fanned_out(self):
        # When only one builder is linked, the bare form should look like a single dispatch.
        local_memory.add_builder("p", "only")
        ev = dispatch_input("dispatch hello", session_id="p")
        self.assertNotIn("Fanned out", ev["output"])
        self.assertIn("Queued", ev["output"])
        self.assertIn("'only'", ev["output"])

    # ---- link command for multi-builder ----

    def test_link_appends_builder_keeps_old_ones(self):
        dispatch_input("link b1", session_id="p")
        ev = dispatch_input("link b2", session_id="p")
        self.assertIn("2 builder(s)", ev["output"])
        self.assertIn("b1", ev["output"])
        self.assertIn("b2", ev["output"])
        self.assertEqual(local_memory.get_builders("p"), ["b1", "b2"])

    def test_link_clear_specific_builder(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        ev = dispatch_input("link clear b1", session_id="p")
        self.assertIn("Removed builder 'b1'", ev["output"])
        self.assertEqual(local_memory.get_builders("p"), ["b2"])

    def test_link_clear_all_drops_every_builder(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        ev = dispatch_input("link clear", session_id="p")
        self.assertIn("Cleared all builders", ev["output"])
        self.assertEqual(local_memory.get_builders("p"), [])

    def test_link_status_shows_all_builders_and_per_builder_counts(self):
        local_memory.add_builder("p", "b1")
        local_memory.add_builder("p", "b2")
        local_memory.dispatch_to_all("p", "task A")
        ev = dispatch_input("link status", session_id="p")
        self.assertIn("2 builder(s)", ev["output"])
        self.assertIn("-> b1:", ev["output"])
        self.assertIn("-> b2:", ev["output"])
        self.assertIn("queued=1", ev["output"])

    def test_link_status_for_session_acting_only_as_builder(self):
        local_memory.add_builder("planner-x", "builder-y")
        ev = dispatch_input("link status", session_id="builder-y")
        self.assertIn("As builder", ev["output"])
        self.assertIn("planner-x", ev["output"])

    def test_link_status_with_no_links(self):
        ev = dispatch_input("link status", session_id="lonely")
        self.assertIn("No links", ev["output"])


# ============================================================
# inbox auto — drain-until-empty
# ============================================================

class InboxAutoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("p", "b")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def _ok_chat(self, response):
        return {
            "ok": True,
            "session_id": "b",
            "response": response,
            "fallback_used": False,
            "approval_required": False,
            "compaction_performed": False,
            "execution_id": f"exe_{response.replace(' ', '_')[:6]}",
        }

    def test_auto_drains_every_queued_task(self):
        local_memory.add_task("p", "task A")
        local_memory.add_task("p", "task B")
        local_memory.add_task("p", "task C")

        responses = [self._ok_chat("did A"), self._ok_chat("did B"), self._ok_chat("did C")]
        with patch("app.services.command_dispatcher.handle_chat", side_effect=responses):
            ev = dispatch_input("inbox auto", session_id="b")

        self.assertEqual(ev["action"], "inbox")
        self.assertIn("3 task(s): 3 done, 0 failed, 0 paused", ev["output"])
        self.assertIn("inbox empty", ev["output"])
        # All tasks marked done
        self.assertEqual(len(local_memory.list_tasks("b", status="done")), 3)
        # And the planner's history has three DONE notes.
        history = local_memory.get_history(session_id="p")
        done_notes = [m for m in history if m["role"] == "system" and "DONE" in m["content"]]
        self.assertEqual(len(done_notes), 3)

    def test_auto_with_explicit_limit_caps_at_n(self):
        for i in range(5):
            local_memory.add_task("p", f"task {i}")

        # Five canned responses — but auto with limit=2 should only consume two.
        responses = [self._ok_chat(f"r{i}") for i in range(5)]
        with patch("app.services.command_dispatcher.handle_chat", side_effect=responses):
            ev = dispatch_input("inbox auto 2", session_id="b")

        self.assertIn("2 task(s): 2 done", ev["output"])
        self.assertIn("reached limit of 2 tasks", ev["output"])
        # Three should remain queued
        self.assertEqual(len(local_memory.get_inbox("b")), 3)

    def test_auto_stops_on_workspace_approval(self):
        local_memory.add_task("p", "first ok task")
        local_memory.add_task("p", "task that needs approval")
        local_memory.add_task("p", "third task that should not run")

        approval_event = {
            "ok": False,
            "approval_required": True,
            "pending_action": {
                "kind": "read",
                "origin": "chat",
                "workspace_path": "C:/proj",
                "paths": ["app/services/router.py"],
            },
            "response": "[approval required]",
            "fallback_used": False,
            "compaction_performed": False,
        }
        with patch(
            "app.services.command_dispatcher.handle_chat",
            side_effect=[self._ok_chat("first done"), approval_event, self._ok_chat("never")],
        ):
            ev = dispatch_input("inbox auto", session_id="b")

        # Two outcomes recorded: 1 done + 1 paused. Loop stopped — third task untouched.
        self.assertIn("2 task(s): 1 done, 0 failed, 1 paused", ev["output"])
        self.assertIn("workspace approval required", ev["output"])
        # The third task is still queued
        remaining = local_memory.get_inbox("b")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["task"], "third task that should not run")
        # And the second task is parked in_progress with the right marker
        in_prog = local_memory.in_progress_task_for("b")
        self.assertIsNotNone(in_prog)
        self.assertEqual(in_prog["result"], "(awaiting workspace approval)")

    def test_auto_continues_past_failures(self):
        local_memory.add_task("p", "a")
        local_memory.add_task("p", "b-fails")
        local_memory.add_task("p", "c")

        ok = self._ok_chat("ok")
        # A "soft" failure: chat returned ok=False with an error — task should be marked failed
        # and the loop should keep going.
        bad = {
            "ok": False,
            "approval_required": False,
            "response": "",
            "error": "model timeout",
            "fallback_used": False,
            "compaction_performed": False,
        }
        with patch(
            "app.services.command_dispatcher.handle_chat",
            side_effect=[ok, bad, self._ok_chat("c-done")],
        ):
            ev = dispatch_input("inbox auto", session_id="b")

        self.assertIn("3 task(s): 2 done, 1 failed", ev["output"])
        self.assertIn("inbox empty", ev["output"])
        # The failed task is marked failed with the error captured
        failed = local_memory.list_tasks("b", status="failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["result"], "model timeout")
        # The planner's history has FAILED note for the one failure
        history = local_memory.get_history(session_id="p")
        failed_notes = [m for m in history if m["role"] == "system" and "FAILED" in m["content"]]
        self.assertEqual(len(failed_notes), 1)

    def test_auto_handles_handle_chat_exception_as_failure(self):
        # If handle_chat raises (network blip, etc.), the task should be marked
        # failed and the loop should keep going.
        local_memory.add_task("p", "raises")
        local_memory.add_task("p", "ok")
        with patch(
            "app.services.command_dispatcher.handle_chat",
            side_effect=[RuntimeError("boom"), self._ok_chat("ok done")],
        ):
            ev = dispatch_input("inbox auto", session_id="b")
        self.assertIn("2 task(s): 1 done, 1 failed", ev["output"])
        failed = local_memory.list_tasks("b", status="failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("boom", failed[0]["result"])

    def test_auto_on_empty_inbox_prints_empty_message(self):
        ev = dispatch_input("inbox auto", session_id="b")
        self.assertIn("Inbox is empty", ev["output"])

    def test_auto_invalid_limit_returns_usage(self):
        local_memory.add_task("p", "x")
        ev = dispatch_input("inbox auto banana", session_id="b")
        self.assertIn("Usage:", ev["output"])
        # The task is still in the queue (we rejected the command before processing)
        self.assertEqual(len(local_memory.get_inbox("b")), 1)


if __name__ == "__main__":
    unittest.main()
