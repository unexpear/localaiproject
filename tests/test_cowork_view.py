"""Tests for the CoWorkView Qt widget (cowork-mode.md Phase 4).

Headless Qt rendering with real local_memory storage backed by a temp
JSON file. The cowork data model is populated directly via local_memory
helpers; we verify the widget renders the active run, the sub-task
table, the past-runs list, and reacts to KIND_COWORK bus events.
"""
import os
import tempfile
import unittest

# Headless Qt for CI / sandbox
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.memory import local_memory  # noqa: E402
from app.memory.json_storage import JsonStorage  # noqa: E402
from app.services.gateway.bus import (  # noqa: E402
    GatewayEvent,
    KIND_COWORK,
    default_bus,
    reset_default_bus_for_tests,
)
from app.ui.cowork_view import CoWorkView, _STATUS_GLYPH  # noqa: E402


def _qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _seed_run(session_id="planner", goal="ship a feature", status="approved"):
    """Insert a 3-sub-task cowork run into storage and return it."""
    plan = [
        {"id": "sub_1", "task": "Sketch the design", "builder": "writer",
         "depends_on": [], "acceptance": "1 page md"},
        {"id": "sub_2", "task": "Implement", "builder": "coder",
         "depends_on": ["sub_1"], "acceptance": "tests pass"},
        {"id": "sub_3", "task": "Review", "builder": "reviewer",
         "depends_on": ["sub_2"], "acceptance": "no blockers"},
    ]
    run = local_memory.add_cowork_run(session_id, goal, plan)
    if status != "planning":
        local_memory.update_cowork_run(run["id"], status=status)
    return local_memory.get_cowork_run(run["id"])


class CoWorkViewLayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        # Touch a couple of sessions so the combo isn't empty.
        local_memory.append_message("user", "hi", session_id="planner")
        local_memory.append_message("user", "hi", session_id="other")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    def test_view_constructs_with_session_combo_populated(self):
        view = CoWorkView(initial_session="planner")
        try:
            sessions = [
                view.session_combo.itemText(i)
                for i in range(view.session_combo.count())
            ]
            self.assertIn("planner", sessions)
            self.assertEqual(view.session_combo.currentText(), "planner")
            # No runs yet → empty table + disabled cancel
            self.assertEqual(view.subtask_table.rowCount(), 0)
            self.assertFalse(view.cancel_btn.isEnabled())
            self.assertIn("No active run", view.status_label.text())
        finally:
            view.close()

    def test_subtask_table_renders_each_sub_task(self):
        _seed_run("planner", goal="renderable goal", status="approved")
        view = CoWorkView(initial_session="planner")
        try:
            self.assertEqual(view.subtask_table.rowCount(), 3)
            self.assertEqual(view.subtask_table.item(0, 1).text(), "sub_1")
            self.assertEqual(view.subtask_table.item(1, 2).text(), "coder")
            # Header includes the seeded goal.
            self.assertIn("renderable goal", view.run_header.text())
            # All sub-tasks have status "(pending)" in the glyph column
            # since no subtask_results have been recorded.
            for row in range(3):
                glyph = view.subtask_table.item(row, 0).text()
                self.assertEqual(glyph, _STATUS_GLYPH["(pending)"])
        finally:
            view.close()

    def test_completed_subtasks_get_done_glyph(self):
        run = _seed_run("planner", status="executing")
        local_memory.record_cowork_subtask_result(
            run["id"], "sub_1", {"status": "done", "result": "wrote it"}
        )
        local_memory.record_cowork_subtask_result(
            run["id"], "sub_2", {"status": "failed", "result": "boom"}
        )
        view = CoWorkView(initial_session="planner")
        try:
            self.assertEqual(view.subtask_table.item(0, 0).text(), _STATUS_GLYPH["done"])
            self.assertEqual(view.subtask_table.item(1, 0).text(), _STATUS_GLYPH["failed"])
            self.assertEqual(view.subtask_table.item(2, 0).text(), _STATUS_GLYPH["(pending)"])
            # "1/3 done" + "1 failed" should appear in the status banner.
            self.assertIn("1/3 done", view.status_label.text())
            self.assertIn("failed", view.status_label.text())
        finally:
            view.close()

    def test_synthesis_text_renders_in_text_pane(self):
        run = _seed_run("planner", status="done")
        local_memory.update_cowork_run(
            run["id"], synthesis="All three sub-tasks completed.",
        )
        view = CoWorkView(initial_session="planner")
        try:
            self.assertIn(
                "All three sub-tasks completed",
                view.synthesis_view.toPlainText(),
            )
        finally:
            view.close()

    def test_cancel_button_enabled_for_active_run_only(self):
        # Active = status in {planning, approved, executing, synthesizing}.
        run = _seed_run("planner", status="executing")
        view = CoWorkView(initial_session="planner")
        try:
            self.assertTrue(view.cancel_btn.isEnabled())
            # Flip to a terminal state and refresh; button disables.
            local_memory.update_cowork_run(run["id"], status="done")
            view.refresh()
            self.assertFalse(view.cancel_btn.isEnabled())
        finally:
            view.close()

    def test_past_runs_list_populated(self):
        run1 = _seed_run("planner", goal="run one", status="done")
        run2 = _seed_run("planner", goal="run two", status="done")
        run3 = _seed_run("planner", goal="run three", status="approved")
        view = CoWorkView(initial_session="planner")
        try:
            count = view.past_runs_list.count()
            self.assertEqual(count, 3)
            # Newest first → run3 at the top.
            top = view.past_runs_list.item(0)
            self.assertEqual(top.data(Qt.UserRole), run3["id"])
        finally:
            view.close()

    def test_clicking_past_run_promotes_it_to_displayed_run(self):
        run1 = _seed_run("planner", goal="first goal", status="done")
        local_memory.update_cowork_run(
            run1["id"], synthesis="first synthesis",
        )
        run2 = _seed_run("planner", goal="second goal", status="approved")
        view = CoWorkView(initial_session="planner")
        try:
            # Default: shows the active run (run2 — status approved).
            self.assertIn("second goal", view.run_header.text())
            # Click the past run (run1).
            for i in range(view.past_runs_list.count()):
                item = view.past_runs_list.item(i)
                if item.data(Qt.UserRole) == run1["id"]:
                    view._on_past_run_clicked(item)
                    break
            # Now showing run1.
            self.assertIn("first goal", view.run_header.text())
            self.assertIn("first synthesis", view.synthesis_view.toPlainText())
        finally:
            view.close()


class CoWorkViewBusIntegrationTests(unittest.TestCase):
    """Verifies the widget refreshes on KIND_COWORK events."""

    def setUp(self):
        self.app = _qapp()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.append_message("user", "hi", session_id="planner")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        reset_default_bus_for_tests()

    def test_kind_cowork_event_triggers_refresh(self):
        run = _seed_run("planner", status="executing")
        view = CoWorkView(initial_session="planner")
        try:
            # Initially no sub-task results displayed.
            self.assertEqual(
                view.subtask_table.item(0, 0).text(),
                _STATUS_GLYPH["(pending)"],
            )

            # Update storage + publish a KIND_COWORK event. The widget's
            # _on_bus_event uses QTimer.singleShot(50, refresh); we
            # process events to drain the timer.
            local_memory.record_cowork_subtask_result(
                run["id"], "sub_1", {"status": "done", "result": "ok"},
            )
            default_bus().publish(GatewayEvent(
                kind=KIND_COWORK,
                session_id="planner",
                payload={"event": "subtask_done", "run_id": run["id"]},
            ))

            # Drain the Qt event loop so the QTimer fires + refresh runs.
            from PySide6.QtCore import QEventLoop
            loop = QEventLoop()
            from PySide6.QtCore import QTimer
            QTimer.singleShot(120, loop.quit)
            loop.exec()

            self.assertEqual(
                view.subtask_table.item(0, 0).text(),
                _STATUS_GLYPH["done"],
            )
        finally:
            view.close()

    def test_event_for_other_session_ignored(self):
        run = _seed_run("planner", status="executing")
        view = CoWorkView(initial_session="planner")
        try:
            initial_glyph = view.subtask_table.item(0, 0).text()
            # Publish a KIND_COWORK event for a different session.
            default_bus().publish(GatewayEvent(
                kind=KIND_COWORK,
                session_id="some-other-session",
                payload={"event": "subtask_done", "run_id": "other"},
            ))
            # No state change should occur; we'd see _on_bus_event
            # short-circuit before the QTimer fires.
            self.assertEqual(
                view.subtask_table.item(0, 0).text(),
                initial_glyph,
            )
        finally:
            view.close()

    def test_unrelated_event_kind_ignored(self):
        run = _seed_run("planner", status="executing")
        view = CoWorkView(initial_session="planner")
        try:
            initial_glyph = view.subtask_table.item(0, 0).text()
            # Publish a KIND_TASK event (not cowork) — should be ignored.
            from app.services.gateway.bus import KIND_TASK
            default_bus().publish(GatewayEvent(
                kind=KIND_TASK,
                session_id="planner",
                payload={"outcome": "done"},
            ))
            self.assertEqual(
                view.subtask_table.item(0, 0).text(),
                initial_glyph,
            )
        finally:
            view.close()

    def test_close_unsubscribes_from_bus(self):
        view = CoWorkView(initial_session="planner")
        sub_id = view._bus_subscription_ids[0]
        before = default_bus().subscriber_count()
        view.close()
        # The view's subscription is gone.
        after = default_bus().subscriber_count()
        self.assertLess(after, before)


if __name__ == "__main__":
    unittest.main()
