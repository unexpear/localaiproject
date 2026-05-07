"""Tests for the LinkedView Qt widget.

Headless Qt rendering with real local_memory storage backed by a temp
JSON file. dispatch_input is real (so tests cover the full plumbing),
but `handle_chat` is mocked so no Ollama call goes out.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

# Headless Qt for CI / sandbox
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from app.memory import local_memory  # noqa: E402
from app.memory.json_storage import JsonStorage  # noqa: E402
from app.ui.linked_view import LinkedView, _BuilderTab  # noqa: E402


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


class LinkedViewLayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        local_memory.add_builder("planner", "reviewer")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_view_constructs_with_planner_and_builder_tabs(self):
        view = LinkedView(initial_planner="planner")
        try:
            self.assertEqual(view.planner_combo.currentText(), "planner")
            # Two builder tabs
            tab_names = [view.builder_tabs.tabText(i) for i in range(view.builder_tabs.count())]
            self.assertEqual(set(tab_names), {"coder", "reviewer"})
            # Dispatch combo includes "(fan out to all)" + each @builder
            target_options = [
                view.dispatch_target.itemText(i) for i in range(view.dispatch_target.count())
            ]
            self.assertIn("(fan out to all)", target_options)
            self.assertIn("@coder", target_options)
            self.assertIn("@reviewer", target_options)
            # Summary label mentions both
            self.assertIn("coder", view.builders_summary.text())
            self.assertIn("reviewer", view.builders_summary.text())
        finally:
            view.deleteLater()

    def test_view_shows_placeholder_when_no_builders_linked(self):
        local_memory.clear_builders("planner")
        view = LinkedView(initial_planner="planner")
        try:
            tab_names = [view.builder_tabs.tabText(i) for i in range(view.builder_tabs.count())]
            self.assertEqual(tab_names, ["(none)"])
            self.assertIn("none", view.builders_summary.text())
        finally:
            view.deleteLater()

    def test_planner_history_renders_in_planner_pane(self):
        local_memory.append_message("user", "what should I do today?", session_id="planner")
        local_memory.append_message("assistant", "let's start by linking a builder", session_id="planner")
        view = LinkedView(initial_planner="planner")
        try:
            text = view.planner_pane.transcript.toPlainText()
            self.assertIn("what should I do today?", text)
            self.assertIn("let's start by linking a builder", text)
        finally:
            view.deleteLater()

    def test_task_queue_renders_with_filter(self):
        # One queued task per builder
        local_memory.dispatch_to_all("planner", "do the thing")
        view = LinkedView(initial_planner="planner")
        try:
            view.task_pane.filter_combo.setCurrentText("queued")
            view._refresh_tasks_only()
            # Two tasks listed
            self.assertEqual(view.task_pane.task_list.count(), 2)
        finally:
            view.deleteLater()

    def test_builder_tab_queue_label_reflects_inbox(self):
        local_memory.dispatch_to_all("planner", "hello")
        view = LinkedView(initial_planner="planner")
        try:
            tab = view._tab_for_builder("coder")
            self.assertIsNotNone(tab)
            self.assertIn("1 queued", tab.queue_label.text())
        finally:
            view.deleteLater()


class LinkedViewActionTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def _ok_chat(self, response, session="coder"):
        return {
            "ok": True,
            "session_id": session,
            "response": response,
            "fallback_used": False,
            "approval_required": False,
            "compaction_performed": False,
            "execution_id": "exe_test",
        }

    def test_dispatch_button_creates_task(self):
        view = LinkedView(initial_planner="planner")
        try:
            view.dispatch_input.setText("refactor the router")
            # Default dispatch target: bare form (fan-out / single builder)
            view._on_dispatch_clicked()
            inbox = local_memory.get_inbox("coder")
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["task"], "refactor the router")
            # Field cleared after dispatch
            self.assertEqual(view.dispatch_input.text(), "")
        finally:
            view.deleteLater()

    def test_dispatch_at_specific_builder(self):
        local_memory.add_builder("planner", "reviewer")
        view = LinkedView(initial_planner="planner")
        try:
            view.dispatch_input.setText("review the diff")
            view.dispatch_target.setCurrentText("@reviewer")
            view._on_dispatch_clicked()
            self.assertEqual(len(local_memory.get_inbox("reviewer")), 1)
            self.assertEqual(len(local_memory.get_inbox("coder")), 0)
        finally:
            view.deleteLater()

    def test_run_next_button_processes_task_via_handle_chat(self):
        local_memory.add_task("planner", "do thing")
        view = LinkedView(initial_planner="planner")
        try:
            with patch(
                "app.services.command_dispatcher.handle_chat",
                return_value=self._ok_chat("did the thing"),
            ):
                view._on_run_next("coder")
            done = local_memory.list_tasks("coder", status="done")
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0]["result"], "did the thing")
            # Queue label updated
            tab = view._tab_for_builder("coder")
            self.assertIn("0 queued", tab.queue_label.text())
        finally:
            view.deleteLater()

    def test_run_all_button_drains_inbox(self):
        for i in range(3):
            local_memory.add_task("planner", f"task {i}")
        view = LinkedView(initial_planner="planner")
        try:
            responses = [self._ok_chat(f"r{i}") for i in range(3)]
            with patch(
                "app.services.command_dispatcher.handle_chat",
                side_effect=responses,
            ):
                view._on_run_all("coder")
            self.assertEqual(len(local_memory.get_inbox("coder")), 0)
            done = local_memory.list_tasks("coder", status="done")
            self.assertEqual(len(done), 3)
        finally:
            view.deleteLater()

    def test_skip_button_marks_task_skipped(self):
        local_memory.add_task("planner", "throwaway")
        view = LinkedView(initial_planner="planner")
        try:
            view._on_skip("coder")
            self.assertEqual(len(local_memory.get_inbox("coder")), 0)
            self.assertEqual(len(local_memory.list_tasks("coder", status="skipped")), 1)
        finally:
            view.deleteLater()

    def test_planner_send_calls_dispatch_input_for_planner_session(self):
        view = LinkedView(initial_planner="planner")
        try:
            captured = {}

            def fake_dispatch(text, session_id="default"):
                captured["text"] = text
                captured["session_id"] = session_id
                return {"action": "chat", "session_id": session_id, "output": "ok"}

            with patch("app.ui.linked_view.dispatch_input", side_effect=fake_dispatch):
                view._on_planner_send("hello planner")
            self.assertEqual(captured["session_id"], "planner")
            self.assertEqual(captured["text"], "hello planner")
        finally:
            view.deleteLater()

    def test_builder_send_calls_dispatch_input_for_builder_session(self):
        view = LinkedView(initial_planner="planner")
        try:
            captured = {}

            def fake_dispatch(text, session_id="default"):
                captured["text"] = text
                captured["session_id"] = session_id
                return {"action": "chat", "session_id": session_id, "output": "ok"}

            with patch("app.ui.linked_view.dispatch_input", side_effect=fake_dispatch):
                view._on_builder_send("coder", "build the thing")
            self.assertEqual(captured["session_id"], "coder")
            self.assertEqual(captured["text"], "build the thing")
        finally:
            view.deleteLater()


class LinkedViewBuilderTabTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()

    def test_builder_tab_emits_signals_with_builder_id(self):
        tab = _BuilderTab("coder")
        captured = {"chat": None, "next": None, "all": None, "skip": None}
        tab.chat_submit.connect(lambda b, t: captured.update({"chat": (b, t)}))
        tab.run_next.connect(lambda b: captured.update({"next": b}))
        tab.run_all.connect(lambda b: captured.update({"all": b}))
        tab.skip.connect(lambda b: captured.update({"skip": b}))
        try:
            # Find the buttons by their visible label
            for btn in tab.findChildren(QPushButton):
                label = btn.text()
                if label == "Run next":
                    btn.click()
                elif label == "Run all":
                    btn.click()
                elif label == "Skip":
                    btn.click()
                elif label == "Send":
                    tab.chat_pane.input_field.setText("hi")
                    btn.click()
            self.assertEqual(captured["next"], "coder")
            self.assertEqual(captured["all"], "coder")
            self.assertEqual(captured["skip"], "coder")
            self.assertEqual(captured["chat"], ("coder", "hi"))
        finally:
            tab.deleteLater()


if __name__ == "__main__":
    unittest.main()
