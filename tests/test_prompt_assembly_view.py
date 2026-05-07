"""Tests for the PromptAssemblyView Qt panel + the
``get_prompt_assembly`` MCP tool (Phase 4 of
``docs/design/prompt-assembly.md``).
"""
import os
import unittest

# Headless Qt rendering for CI / testing
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.memory import local_memory  # noqa: E402
from app.mcp import tools as mcp_tools  # noqa: E402
from app.ui.prompt_assembly_view import PromptAssemblyView  # noqa: E402


def _qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ============================================================
# MCP tool: get_prompt_assembly
# ============================================================

class GetPromptAssemblyToolTests(unittest.TestCase):
    """The MCP tool wraps ``chat_service.build_runtime_assembly`` —
    these tests confirm the descriptor is registered correctly and
    the handler returns the expected shape."""

    def _tool(self):
        return next(t for t in mcp_tools.TOOLS if t.name == "get_prompt_assembly")

    def test_tool_is_registered(self):
        names = [t.name for t in mcp_tools.TOOLS]
        self.assertIn("get_prompt_assembly", names)

    def test_descriptor_advertises_optional_message(self):
        descriptor = self._tool().descriptor()
        self.assertEqual(descriptor["name"], "get_prompt_assembly")
        self.assertIn("inputSchema", descriptor)
        props = descriptor["inputSchema"]["properties"]
        self.assertIn("session_id", props)
        self.assertIn("message", props)

    def test_handler_returns_trace_and_diagnostics(self):
        result = self._tool().handler({"session_id": "mcp_get_prompt_test"})
        self.assertEqual(result["action"], "get_prompt_assembly")
        self.assertEqual(result["session_id"], "mcp_get_prompt_test")
        self.assertIn("trace", result["result"])
        self.assertIn("total_budget", result["result"])
        self.assertIn("rendered_chars", result["result"])
        self.assertIn("over_budget", result["result"])
        self.assertIn("footer", result["result"])
        # Every call should at least surface control + mode slots.
        names = [t["name"] for t in result["result"]["trace"]]
        self.assertIn("control", names)
        self.assertIn("mode", names)
        # The synthetic _diagnostics entry is stripped from the
        # visible trace; over_budget flag is exposed separately.
        self.assertNotIn("_diagnostics", names)

    def test_handler_marks_over_budget_when_pinned_alone_exceeds(self):
        sid = "mcp_get_prompt_over_budget"
        local_memory.set_prompt_total_budget(session_id=sid, total_budget=20)
        result = self._tool().handler({"session_id": sid})
        self.assertTrue(result["result"]["over_budget"])
        self.assertIn("OVER_BUDGET", result["output"])

    def test_handler_with_message_arg_does_not_dispatch_chat(self):
        # The tool is read-only — even with a message it must NOT
        # call dispatch_input (which would actually invoke the model).
        sid = "mcp_get_prompt_with_message"
        result = self._tool().handler({
            "session_id": sid,
            "message": "hello",
        })
        # No history was appended because we never dispatched.
        history = local_memory.get_history(session_id=sid, limit=10)
        self.assertEqual(history, [])
        # And the assembly trace still rendered.
        self.assertGreaterEqual(len(result["result"]["trace"]), 2)


# ============================================================
# Qt panel: PromptAssemblyView
# ============================================================

class PromptAssemblyViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qapp()

    def _build_view(self, session_id="default"):
        view = PromptAssemblyView(initial_session=session_id)
        return view

    def test_view_renders_slot_table_for_default_session(self):
        view = self._build_view()
        try:
            # At minimum, control + mode slots should populate the table.
            self.assertGreaterEqual(view.slot_table.rowCount(), 2)
            names = [
                view.slot_table.item(row, 0).text()
                for row in range(view.slot_table.rowCount())
            ]
            self.assertIn("control", names)
            self.assertIn("mode", names)
        finally:
            view.close()
            view.deleteLater()

    def test_view_total_label_shows_chars_and_budget(self):
        view = self._build_view()
        try:
            text = view.total_label.text()
            # Format: "Total: N / M chars  ·  K slots"
            self.assertIn("/", text)
            self.assertIn("chars", text)
            self.assertIn("slots", text)
        finally:
            view.close()
            view.deleteLater()

    def test_warning_banner_visible_when_over_budget(self):
        sid = "qt_over_budget_panel"
        local_memory.set_prompt_total_budget(session_id=sid, total_budget=20)
        view = self._build_view(session_id=sid)
        view.show()
        try:
            self.assertTrue(view.warning_label.isVisible())
            self.assertIn("prompt_budget_exceeded", view.warning_label.text())
        finally:
            view.close()
            view.deleteLater()

    def test_warning_banner_hidden_when_under_budget(self):
        sid = "qt_under_budget_panel"
        local_memory.set_prompt_total_budget(session_id=sid, total_budget=100_000)
        view = self._build_view(session_id=sid)
        view.show()
        try:
            self.assertFalse(view.warning_label.isVisible())
        finally:
            view.close()
            view.deleteLater()

    def test_refresh_button_rebuilds_table(self):
        view = self._build_view()
        try:
            initial = view.slot_table.rowCount()
            view.refresh()  # explicit refresh shouldn't crash
            self.assertEqual(view.slot_table.rowCount(), initial)
        finally:
            view.close()
            view.deleteLater()

    def test_close_unsubscribes_from_bus(self):
        view = self._build_view()
        # The view subscribes once on construction.
        self.assertEqual(len(view._bus_subscription_ids), 1)
        view.close()
        # After close, the list should be cleared so we don't leak
        # bus callbacks across tests.
        self.assertEqual(view._bus_subscription_ids, [])
        view.deleteLater()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
