"""Tests for the MCP server (`docs/design/mcp-server.md`).

Four layers:

  - Protocol framing (line-delimited JSON in/out, parse-error mapping)
  - Method dispatch (initialize / tools/list / tools/call /
    resources/list / resources/read; unknown methods)
  - Tool surface (each registered tool dispatches into the project's
    own `dispatch_input` and returns a structured result)
  - End-to-end via the ``serve`` loop with simulated stdio
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.mcp import protocol
from app.mcp import server as mcp_server
from app.mcp import tools as mcp_tools


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _drive(messages):
    """Send each request through the MCP serve loop and return the
    parsed responses (skipping notifications which produce no reply)."""
    reader = io.StringIO()
    for msg in messages:
        reader.write(json.dumps(msg) + "\n")
    reader.seek(0)
    writer = io.StringIO()
    mcp_server.serve(reader, writer)
    writer.seek(0)
    return [json.loads(line) for line in writer if line.strip()]


# ============================================================
# Protocol framing
# ============================================================

class ProtocolFramingTests(unittest.TestCase):
    def test_read_message_returns_none_on_eof(self):
        self.assertIsNone(protocol.read_message(io.StringIO()))

    def test_read_message_skips_blank_line(self):
        self.assertIsNone(protocol.read_message(io.StringIO("\n")))

    def test_read_message_parses_one_line(self):
        reader = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        msg = protocol.read_message(reader)
        self.assertEqual(msg["method"], "ping")

    def test_write_message_appends_newline_and_flushes(self):
        writer = io.StringIO()
        protocol.write_message(writer, {"a": 1})
        self.assertEqual(writer.getvalue(), '{"a": 1}\n')

    def test_serve_loop_handles_malformed_json(self):
        reader = io.StringIO("not json at all\n")
        writer = io.StringIO()
        protocol.serve_loop(reader, writer, {})
        out = writer.getvalue().strip()
        msg = json.loads(out)
        self.assertEqual(msg["error"]["code"], protocol.PARSE_ERROR)


# ============================================================
# Method dispatch
# ============================================================

class DispatchTests(unittest.TestCase):
    def test_unknown_method_returns_method_not_found(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "made/up"},
        ])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], protocol.METHOD_NOT_FOUND)

    def test_notifications_get_no_response(self):
        # Notifications (no `id` field) MUST NOT produce a response.
        responses = _drive([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ])
        self.assertEqual(responses, [])

    def test_unknown_notification_dropped_silently(self):
        responses = _drive([
            {"jsonrpc": "2.0", "method": "notifications/totally_unknown"},
        ])
        self.assertEqual(responses, [])

    def test_handler_exception_becomes_internal_error(self):
        # Inject a handler that raises to verify the loop's safety net.
        reader = io.StringIO(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "boom",
        }) + "\n")
        writer = io.StringIO()

        def boom(_params):
            raise RuntimeError("fire")

        protocol.serve_loop(reader, writer, {"boom": boom})
        msg = json.loads(writer.getvalue().strip())
        self.assertEqual(msg["error"]["code"], protocol.INTERNAL_ERROR)
        self.assertIn("fire", msg["error"]["message"])

    def test_handler_error_carries_data(self):
        def fails(_params):
            raise protocol.HandlerError(
                "go away", code=protocol.INVALID_PARAMS,
                data={"reason": "invalid_argument"},
            )
        reader = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "fail"}) + "\n")
        writer = io.StringIO()
        protocol.serve_loop(reader, writer, {"fail": fails})
        msg = json.loads(writer.getvalue().strip())
        self.assertEqual(msg["error"]["data"]["reason"], "invalid_argument")


# ============================================================
# Initialize handshake
# ============================================================

class InitializeHandshakeTests(unittest.TestCase):
    def test_initialize_returns_server_info_and_capabilities(self):
        responses = _drive([
            {
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
        ])
        self.assertEqual(len(responses), 1)
        result = responses[0]["result"]
        self.assertEqual(result["serverInfo"]["name"], "localaiproject")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("resources", result["capabilities"])

    def test_initialized_notification_no_response(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ])
        # Only initialize gets a reply.
        self.assertEqual(len(responses), 1)

    def test_ping(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ])
        self.assertEqual(responses[0]["result"], {})


# ============================================================
# tools/list + tools/call
# ============================================================

class ToolsSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_tools_list_returns_descriptors(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ])
        tools = responses[0]["result"]["tools"]
        self.assertGreater(len(tools), 5)
        names = {t["name"] for t in tools}
        for expected in {"status", "models", "doctor", "chat", "do", "approve"}:
            self.assertIn(expected, names)

    def test_each_tool_descriptor_has_input_schema(self):
        for tool in mcp_tools.TOOLS:
            d = tool.descriptor()
            self.assertEqual(d["name"], tool.name)
            self.assertIn("description", d)
            self.assertIsInstance(d["inputSchema"], dict)
            self.assertEqual(d["inputSchema"].get("type"), "object")

    def test_tools_call_unknown_tool_returns_method_not_found(self):
        responses = _drive([
            {
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"name": "ghost_tool", "arguments": {}},
            },
        ])
        self.assertEqual(responses[0]["error"]["code"], protocol.METHOD_NOT_FOUND)

    def test_tools_call_missing_name_returns_invalid_params(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"arguments": {}}},
        ])
        self.assertEqual(responses[0]["error"]["code"], protocol.INVALID_PARAMS)

    def test_tools_call_status_returns_text_block(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "status", "arguments": {}}},
        ])
        result = responses[0]["result"]
        self.assertIn("content", result)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertFalse(result.get("isError", False))

    def test_tools_call_link_then_dispatch_round_trip(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {
                 "name": "link",
                 "arguments": {"planner_session": "p", "builder_session": "b"},
             }},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {
                 "name": "dispatch",
                 "arguments": {"session_id": "p", "text": "do a thing"},
             }},
        ])
        # Link should succeed (no isError)
        self.assertFalse(responses[0]["result"].get("isError", False))
        # Dispatch should also succeed
        self.assertFalse(responses[1]["result"].get("isError", False))

    def test_tools_call_set_workspace_invalid_path_marks_iserror(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {
                 "name": "set_workspace",
                 "arguments": {"path": "/this/path/does/not/exist/anywhere"},
             }},
        ])
        result = responses[0]["result"]
        self.assertTrue(result.get("isError"))
        self.assertEqual(
            result["structuredContent"]["reason"],
            "path_not_found",
        )

    def test_tools_call_read_file_no_workspace_returns_no_workspace_reason(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {
                 "name": "read_file",
                 "arguments": {"path": "src/foo.py"},
             }},
        ])
        result = responses[0]["result"]
        self.assertTrue(result.get("isError"))
        self.assertEqual(
            result["structuredContent"]["reason"],
            "no_workspace",
        )

    def test_tools_call_set_mode_validates_value(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {
                 "name": "set_mode",
                 "arguments": {"mode": "build"},
             }},
        ])
        # Build mode applies cleanly.
        self.assertFalse(responses[0]["result"].get("isError"))

    def test_tools_call_handler_exception_becomes_isError(self):
        # Patch a tool to raise; the loop should produce isError=True
        # rather than a JSON-RPC error envelope.
        tool = mcp_tools.get_tool("status")
        original = tool.handler

        def boom(_args):
            raise RuntimeError("oh no")

        tool.handler = boom
        try:
            responses = _drive([
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "status", "arguments": {}}},
            ])
        finally:
            tool.handler = original

        self.assertTrue(responses[0]["result"]["isError"])
        self.assertEqual(
            responses[0]["result"]["structuredContent"]["reason"],
            "internal_error",
        )

    def test_event_to_call_result_marks_iserror_when_reason_set(self):
        result = mcp_tools.event_to_call_result({
            "action": "x", "output": "boom", "reason": "invalid_argument",
        })
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["reason"],
            "invalid_argument",
        )

    def test_event_to_call_result_clean_event_is_not_iserror(self):
        result = mcp_tools.event_to_call_result({
            "action": "ok", "output": "hello world",
        })
        self.assertFalse(result["isError"])


# ============================================================
# resources/list + resources/read
# ============================================================

class ResourcesSurfaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_resources_list_returns_sessions_uri(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
        ])
        resources = responses[0]["result"]["resources"]
        uris = {r["uri"] for r in resources}
        self.assertIn("sessions://list", uris)

    def test_resources_read_sessions_list(self):
        # Touch a session so list_sessions has a name to return.
        local_memory.append_message("user", "hi", session_id="s1")

        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
             "params": {"uri": "sessions://list"}},
        ])
        contents = responses[0]["result"]["contents"]
        body = json.loads(contents[0]["text"])
        self.assertIn("s1", body["sessions"])

    def test_resources_read_unknown_uri_returns_method_not_found(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "resources/read",
             "params": {"uri": "fake://nonsense"}},
        ])
        self.assertEqual(responses[0]["error"]["code"], protocol.METHOD_NOT_FOUND)


# ============================================================
# Dispatcher `mcp` command surface
# ============================================================

class McpBrowserToolsTests(unittest.TestCase):
    """The browser_* MCP tools wrap the dispatcher's `browser` command,
    so they share the per-origin approval flow and the FakeBrowserBackend."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        # Import here to keep the module-level test surface light.
        from app.services import browser_service as bs
        self._bs = bs
        self.fake = bs.FakeBrowserBackend()
        bs.set_backend(self.fake)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        self._bs.reset_for_tests()

    def test_browser_tools_appear_in_tools_list(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ])
        names = {t["name"] for t in responses[0]["result"]["tools"]}
        for expected in {
            "browser_open",
            "browser_navigate",
            "browser_screenshot",
            "browser_get_text",
            "browser_get_html",
            "browser_close",
        }:
            self.assertIn(expected, names)

    def test_browser_open_returns_approval_required_on_new_origin(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {
                 "name": "browser_open",
                 "arguments": {"url": "https://example.com/"},
             }},
        ])
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["reason"],
            "approval_required",
        )

    def test_browser_open_then_approve_then_get_text_round_trip(self):
        self.fake.register_page(
            "https://example.com/", title="Example", text="page body",
        )
        # 1. Open → approval pending
        # 2. Approve → navigate runs, origin whitelisted
        # 3. get_text → returns the canned body
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {
                 "name": "browser_open",
                 "arguments": {"url": "https://example.com/"},
             }},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "approve", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "browser_get_text", "arguments": {}}},
        ])
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertFalse(responses[1]["result"].get("isError", False))
        self.assertFalse(responses[2]["result"].get("isError", False))
        self.assertIn("page body", responses[2]["result"]["content"][0]["text"])

    def test_browser_open_with_no_url_returns_invalid_argument(self):
        responses = _drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "browser_open", "arguments": {}}},
        ])
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["reason"], "invalid_argument",
        )


class McpDispatcherCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_mcp_info(self):
        from app.services.command_dispatcher import dispatch_input
        ev = dispatch_input("mcp", session_id="default")
        self.assertIn("MCP server", ev["output"])
        self.assertIn("tools", ev["output"])

    def test_mcp_tools_lists_all_registered(self):
        from app.services.command_dispatcher import dispatch_input
        ev = dispatch_input("mcp tools", session_id="default")
        self.assertIn("status", ev["output"])
        self.assertIn("doctor", ev["output"])
        self.assertEqual(len(ev["result"]), len(mcp_tools.TOOLS))

    def test_mcp_config_emits_mcp_json_snippet(self):
        from app.services.command_dispatcher import dispatch_input
        ev = dispatch_input("mcp config", session_id="default")
        self.assertIn("mcpServers", ev["output"])
        self.assertIn("localaiproject", ev["output"])
        self.assertIn("python", ev["output"])

    def test_mcp_unknown_subcommand(self):
        from app.services.command_dispatcher import dispatch_input
        ev = dispatch_input("mcp bogus", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")


if __name__ == "__main__":
    unittest.main()
