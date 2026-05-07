import os
import unittest
from unittest.mock import patch

# Headless Qt rendering for CI / testing
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.qt_main_window import LocalAIQtWindow  # noqa: E402


_FAKE_STATUS_ONLINE = {
    "runtime_available": True,
    "version": "0.12.7",
    "installed": [{"role": "reasoning", "model": "llama3.1:8b", "required": True, "size_gb": 8}],
    "missing": [{"role": "coding", "model": "qwen2.5-coder:7b", "required": True, "size_gb": 7}],
    "optional": [],
    "detected": ["llama3.1:8b"],
}

_FAKE_STATUS_OFFLINE = {
    "runtime_available": False,
    "version": None,
    "installed": [],
    "missing": [{"role": "reasoning", "model": "llama3.1:8b", "required": True, "size_gb": 8}],
    "optional": [],
    "detected": [],
}


def _qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _build_window(status, context):
    with patch("app.services.model_service.get_status", return_value=status), \
         patch("app.ui.qt_main_window.get_status", return_value=status), \
         patch(
             "app.ui.qt_main_window.local_memory.get_session_context",
             return_value=context,
         ), \
         patch("app.ui.qt_main_window.local_memory.get_executions", return_value=[]):
        win = LocalAIQtWindow()
    return win


def _led_color(window):
    # The QSS sets `background: <fill>;` — pull the fill back out.
    style = window.led.styleSheet() or ""
    for token in style.split(";"):
        token = token.strip()
        if token.startswith("background:"):
            return token.split(":", 1)[1].strip()
    return ""


class ChassisBarTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()

    def test_led_is_online_green_when_ollama_connected(self):
        win = _build_window(
            _FAKE_STATUS_ONLINE,
            {"workspace_path": None, "active_skills": [], "pending_approval": None},
        )
        try:
            self.assertEqual(_led_color(win).upper(), "#93B88A")
        finally:
            win.deleteLater()

    def test_led_is_offline_red_when_ollama_unreachable(self):
        win = _build_window(
            _FAKE_STATUS_OFFLINE,
            {"workspace_path": None, "active_skills": [], "pending_approval": None},
        )
        try:
            self.assertEqual(_led_color(win).upper(), "#B43A3A")
        finally:
            win.deleteLater()

    def test_led_switches_to_active_when_install_in_flight(self):
        win = _build_window(
            _FAKE_STATUS_ONLINE,
            {"workspace_path": None, "active_skills": [], "pending_approval": None},
        )
        try:
            self.assertEqual(_led_color(win).upper(), "#93B88A")
            win._install_in_flight = True
            with patch("app.ui.qt_main_window.get_status", return_value=_FAKE_STATUS_ONLINE), \
                 patch(
                     "app.ui.qt_main_window.local_memory.get_session_context",
                     return_value={
                         "workspace_path": None,
                         "active_skills": [],
                         "pending_approval": None,
                     },
                 ):
                win._refresh_status_banner()
            self.assertEqual(_led_color(win).upper(), "#F56600")
        finally:
            win.deleteLater()


class BannerSegmentTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()

    def test_banner_segments_populate_with_runtime_lanes_and_skills(self):
        win = _build_window(
            _FAKE_STATUS_ONLINE,
            {
                "workspace_path": "C:/projects/localai",
                "active_skills": ["builder", "reviewer"],
                "pending_approval": None,
            },
        )
        try:
            self.assertIn("connected", win.banner_segments["ollama"].text())
            self.assertIn("0.12.7", win.banner_segments["ollama"].text())
            self.assertEqual(win.banner_segments["lanes"].text(), "reasoning")
            self.assertEqual(win.banner_segments["missing"].text(), "qwen2.5-coder:7b")
            self.assertIn("builder", win.banner_segments["skills"].text())
            self.assertEqual(win.banner_segments["approval"].text(), "none")
        finally:
            win.deleteLater()

    def test_pending_approval_text_is_orange(self):
        win = _build_window(
            _FAKE_STATUS_ONLINE,
            {
                "workspace_path": None,
                "active_skills": [],
                "pending_approval": {"kind": "read"},
            },
        )
        try:
            self.assertIn("queued", win.banner_segments["approval"].text())
            self.assertIn("#F56600", win.banner_segments["approval"].styleSheet())
        finally:
            win.deleteLater()

    def test_session_strip_includes_workspace_and_skills(self):
        win = _build_window(
            _FAKE_STATUS_ONLINE,
            {
                "workspace_path": "C:/Users/micha/Desktop/localaiproject",
                "active_skills": ["builder"],
                "pending_approval": None,
            },
        )
        try:
            text = win.session_strip.text()
            self.assertIn("session", text)
            self.assertIn("workspace", text)
            self.assertIn("localaiproject", text)
            self.assertIn("builder", text)
        finally:
            win.deleteLater()


# ============================================================
# Bus-driven live refresh
# ============================================================

import tempfile  # noqa: E402

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.memory import local_memory  # noqa: E402
from app.memory.json_storage import JsonStorage  # noqa: E402
from app.services.gateway import (  # noqa: E402
    GatewayEvent,
    KIND_DISPATCH,
    KIND_TASK,
    default_bus,
    reset_default_bus_for_tests,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _drain_qt_events_until(predicate, timeout=1.0):
    """Spin the Qt event loop briefly until `predicate()` returns True."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    QCoreApplication.processEvents()
    return predicate()


class BusRefreshTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        reset_default_bus_for_tests()
        self._tmp.cleanup()

    def _build(self):
        # Skip the missing-models prompt path with a complete-defaults status.
        status = {
            "runtime_available": True, "version": "0.12.7",
            "installed": [], "missing": [], "optional": [], "detected": [],
        }
        with patch("app.services.model_service.get_status", return_value=status), \
             patch("app.ui.qt_main_window.get_status", return_value=status):
            win = LocalAIQtWindow()
        return win

    def test_window_subscribes_on_construction(self):
        win = self._build()
        try:
            self.assertIsNotNone(win._bus_subscription_id)
            self.assertGreaterEqual(default_bus().subscriber_count(default_bus().ANY), 1)
        finally:
            win.close()
            win.deleteLater()

    def test_close_unsubscribes(self):
        win = self._build()
        before = default_bus().subscriber_count(default_bus().ANY)
        win.close()
        win.deleteLater()
        QCoreApplication.processEvents()
        after = default_bus().subscriber_count(default_bus().ANY)
        self.assertLess(after, before)

    def test_event_for_other_session_is_ignored(self):
        win = self._build()
        try:
            self.assertEqual(win._refresh_pending, False)
            default_bus().publish(GatewayEvent(kind=KIND_DISPATCH, session_id="not-mine"))
            QCoreApplication.processEvents()
            self.assertEqual(win._refresh_pending, False)
        finally:
            win.close()
            win.deleteLater()

    def test_event_for_current_session_schedules_refresh(self):
        win = self._build()
        try:
            self.assertEqual(win._refresh_pending, False)
            default_bus().publish(GatewayEvent(kind=KIND_DISPATCH, session_id=win.session_id))
            QCoreApplication.processEvents()
            # Marked pending immediately; the actual refresh fires 50ms later.
            self.assertTrue(win._refresh_pending)
            self.assertTrue(_drain_qt_events_until(lambda: not win._refresh_pending, timeout=1.5))
        finally:
            win.close()
            win.deleteLater()

    def test_burst_of_events_coalesces_to_one_refresh(self):
        win = self._build()
        try:
            calls = []

            def fake_refresh():
                # Mirror the real method's pending-flag reset so the test can wait on it.
                win._refresh_pending = False
                calls.append(1)

            with patch.object(win, "_do_bus_refresh", side_effect=fake_refresh):
                ev = GatewayEvent(kind=KIND_DISPATCH, session_id=win.session_id)
                for _ in range(5):
                    win._on_bus_event(ev)
                self.assertTrue(_drain_qt_events_until(lambda: len(calls) >= 1, timeout=1.5))
            self.assertEqual(len(calls), 1, f"expected exactly one coalesced refresh, got {len(calls)}")
        finally:
            win.close()
            win.deleteLater()

    def test_history_sync_appends_new_entries(self):
        win = self._build()
        try:
            # Window sees its initial state — no history entries yet.
            self.assertEqual(win._last_rendered_history_count, 0)

            # Simulate an external dispatch: write to history + publish a bus event.
            local_memory.append_message("user", "what's up?", session_id=win.session_id)
            local_memory.append_message("assistant", "all working", session_id=win.session_id,
                                        model="llama3.1:8b")
            default_bus().publish(GatewayEvent(kind=KIND_DISPATCH, session_id=win.session_id))
            QCoreApplication.processEvents()
            self.assertTrue(_drain_qt_events_until(
                lambda: win._last_rendered_history_count >= 2, timeout=1.5
            ))
            text = win.transcript.toPlainText()
            self.assertIn("what's up?", text)
            self.assertIn("all working", text)
            # Assistant turn shows the model on the speaker line
            self.assertIn("llama3.1:8b", text)
        finally:
            win.close()
            win.deleteLater()

    def test_history_sync_does_not_re_render_old_entries(self):
        win = self._build()
        try:
            local_memory.append_message("user", "first", session_id=win.session_id)
            default_bus().publish(GatewayEvent(kind=KIND_DISPATCH, session_id=win.session_id))
            self.assertTrue(_drain_qt_events_until(
                lambda: win._last_rendered_history_count >= 1, timeout=1.5
            ))
            first_text = win.transcript.toPlainText()
            self.assertEqual(first_text.count("first"), 1)

            # Second event with no new history → no duplicate
            default_bus().publish(GatewayEvent(kind=KIND_DISPATCH, session_id=win.session_id))
            self.assertTrue(_drain_qt_events_until(
                lambda: not win._refresh_pending, timeout=1.5
            ))
            second_text = win.transcript.toPlainText()
            self.assertEqual(second_text.count("first"), 1)
        finally:
            win.close()
            win.deleteLater()

    def test_switch_session_resets_history_counter_and_reloads(self):
        win = self._build()
        try:
            # Seed two sessions
            local_memory.append_message("user", "hi from default", session_id="default")
            local_memory.append_message("user", "hi from arch", session_id="arch")

            # Sync default's history first
            default_bus().publish(GatewayEvent(kind=KIND_DISPATCH, session_id="default"))
            self.assertTrue(_drain_qt_events_until(
                lambda: win._last_rendered_history_count >= 1, timeout=1.5
            ))

            # Switch to arch
            win._switch_session("arch")
            self.assertEqual(win.session_id, "arch")
            self.assertEqual(win._last_rendered_history_count, 1)
            text = win.transcript.toPlainText()
            self.assertIn("hi from arch", text)
            self.assertNotIn("hi from default", text)
        finally:
            win.close()
            win.deleteLater()

    def test_dispatch_text_chat_action_does_not_double_echo(self):
        win = self._build()
        try:
            # Pre-populate history with what handle_chat would write.
            chat_event = {
                "ok": True,
                "session_id": win.session_id,
                "response": "the assistant reply",
                "fallback_used": False,
                "approval_required": False,
                "compaction_performed": False,
                "execution_id": "exe_x",
                "action_hint": None,
            }
            # Patch dispatch_input to simulate a chat path: it appends history
            # entries and returns action="chat".
            def fake_dispatch(text, session_id="default"):
                local_memory.append_message("user", text, session_id=session_id)
                local_memory.append_message("assistant", "the assistant reply",
                                            session_id=session_id, model="llama3.1:8b")
                # Also publish a dispatch event so our bus handler fires.
                default_bus().publish(GatewayEvent(kind=KIND_DISPATCH,
                                                    session_id=session_id,
                                                    payload={"action": "chat"}))
                return {"action": "chat", "session_id": session_id, "output": ""}

            with patch("app.ui.qt_main_window.dispatch_input", side_effect=fake_dispatch):
                win._dispatch_text("ask the model something")
            self.assertTrue(_drain_qt_events_until(
                lambda: "the assistant reply" in win.transcript.toPlainText(),
                timeout=1.5,
            ))
            # The user message should appear exactly once (from history sync,
            # not echoed locally as well).
            text = win.transcript.toPlainText()
            self.assertEqual(text.count("ask the model something"), 1)
        finally:
            win.close()
            win.deleteLater()

    def test_dispatch_text_non_chat_still_echoes_locally(self):
        win = self._build()
        try:
            def fake_dispatch(text, session_id="default"):
                # Non-chat: no history side-effect; just publish event + return output.
                default_bus().publish(GatewayEvent(kind=KIND_DISPATCH,
                                                    session_id=session_id,
                                                    payload={"action": "status"}))
                return {"action": "status", "session_id": session_id,
                        "output": "Ollama: connected"}

            with patch("app.ui.qt_main_window.dispatch_input", side_effect=fake_dispatch):
                win._dispatch_text("status")
            QCoreApplication.processEvents()
            text = win.transcript.toPlainText()
            self.assertIn("status", text)             # user input echoed
            self.assertIn("Ollama: connected", text)  # control output echoed
        finally:
            win.close()
            win.deleteLater()


if __name__ == "__main__":
    unittest.main()
