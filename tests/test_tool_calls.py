"""Tests for the tool-calling parser + dispatcher (Phase 1 — see
`docs/design/tool-calling.md`).

Covers:
* ``parse_tool_calls`` parser:
  - Single + multiple ```dispatcher blocks
  - Case-insensitive info-line match
  - Empty body skipped
  - Non-dispatcher fenced blocks ignored
  - Coexists with ```python file=... blocks (parse_fenced_writes pattern)
  - Multi-line bodies preserved
  - 1-based indexing

* ``format_tool_calls`` rendering:
  - Empty list → friendly message
  - Single + multiple
  - Long-command truncation
  - Multi-line "+N more lines" marker

* ``parse_from_last_assistant``:
  - Reads from session.history
  - Walks back to most recent assistant turn
  - Returns empty list when no assistant turns

* Dispatcher subcommands: show / run / parse:
  - tool show (empty + populated)
  - tool run <n> recursively dispatches
  - tool run validates index range
  - tool parse <text> for debug
  - Unknown subcommand → invalid_argument
"""
import unittest
import uuid

from app.memory import local_memory
from app.services import tool_calls
from app.services.command_dispatcher import dispatch_input


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ============================================================
# Parser
# ============================================================

class ParseToolCallsTests(unittest.TestCase):
    def test_single_call(self):
        text = "Try this:\n\n```dispatcher\nmode plan\n```\n"
        calls = tool_calls.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].command, "mode plan")
        self.assertEqual(calls[0].index, 1)

    def test_multiple_calls_indexed(self):
        text = (
            "First:\n```dispatcher\nmode plan\n```\n"
            "Second:\n```dispatcher\nnote set wm hello\n```\n"
        )
        calls = tool_calls.parse_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].index, 1)
        self.assertEqual(calls[1].index, 2)
        self.assertEqual(calls[0].command, "mode plan")
        self.assertEqual(calls[1].command, "note set wm hello")

    def test_case_insensitive_info_line(self):
        for fence in ("dispatcher", "DISPATCHER", "Dispatcher", "  dispatcher  "):
            text = f"```{fence}\ncmd\n```"
            calls = tool_calls.parse_tool_calls(text)
            self.assertEqual(len(calls), 1, f"fence {fence!r} should match")

    def test_empty_body_skipped(self):
        text = "```dispatcher\n```\n```dispatcher\n   \n```"
        calls = tool_calls.parse_tool_calls(text)
        self.assertEqual(calls, [])

    def test_non_dispatcher_blocks_ignored(self):
        text = (
            "```python\nprint('hi')\n```\n"
            "```dispatcher\nmode plan\n```\n"
            "```bash\nls\n```\n"
        )
        calls = tool_calls.parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].command, "mode plan")

    def test_coexists_with_file_writes(self):
        # The two parsers should be able to scan the same response
        # without conflicting — file writes use `file=` info attr,
        # tool calls use `dispatcher` info line.
        text = (
            "Edit:\n```python file=foo.py\nprint('hi')\n```\n"
            "Then:\n```dispatcher\nmode plan\n```\n"
        )
        from app.services.workspace_service import parse_fenced_writes
        writes = parse_fenced_writes(text)
        calls = tool_calls.parse_tool_calls(text)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["path"], "foo.py")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].command, "mode plan")

    def test_multiline_body_preserved(self):
        text = (
            "```dispatcher\n"
            "do this\n"
            "and then this\n"
            "```\n"
        )
        calls = tool_calls.parse_tool_calls(text)
        self.assertEqual(calls[0].command, "do this\nand then this")

    def test_empty_input(self):
        self.assertEqual(tool_calls.parse_tool_calls(""), [])
        self.assertEqual(tool_calls.parse_tool_calls(None), [])

    def test_non_string_input(self):
        # Defensive — model output passed through json/dict accidentally.
        self.assertEqual(tool_calls.parse_tool_calls(123), [])
        self.assertEqual(tool_calls.parse_tool_calls({"x": 1}), [])

    def test_to_dict_round_trip(self):
        text = "```dispatcher\nmode plan\n```"
        call = tool_calls.parse_tool_calls(text)[0]
        d = call.to_dict()
        self.assertEqual(d["command"], "mode plan")
        self.assertEqual(d["index"], 1)
        self.assertIn("dispatcher", d["raw_block"])


# ============================================================
# Renderer
# ============================================================

class FormatToolCallsTests(unittest.TestCase):
    def test_empty_list(self):
        s = tool_calls.format_tool_calls([])
        self.assertIn("no tool calls", s)

    def test_single_call(self):
        text = "```dispatcher\nmode plan\n```"
        calls = tool_calls.parse_tool_calls(text)
        s = tool_calls.format_tool_calls(calls)
        self.assertIn("Tool calls (1)", s)
        self.assertIn("[1]", s)
        self.assertIn("mode plan", s)
        self.assertIn("tool run", s)

    def test_long_command_truncated(self):
        long_cmd = "x" * 500
        text = f"```dispatcher\n{long_cmd}\n```"
        calls = tool_calls.parse_tool_calls(text)
        s = tool_calls.format_tool_calls(calls)
        self.assertIn("...", s)
        self.assertLess(len(s), 600)

    def test_multiline_command_marker(self):
        text = (
            "```dispatcher\n"
            "line one\n"
            "line two\n"
            "line three\n"
            "```"
        )
        calls = tool_calls.parse_tool_calls(text)
        s = tool_calls.format_tool_calls(calls)
        self.assertIn("more line", s)


# ============================================================
# parse_from_last_assistant
# ============================================================

class ParseFromLastAssistantTests(unittest.TestCase):
    def test_empty_history_returns_empty(self):
        sid = _unique("tool_history_empty")
        self.assertEqual(tool_calls.parse_from_last_assistant(sid), [])

    def test_walks_back_to_most_recent_assistant(self):
        sid = _unique("tool_history_walk")
        local_memory.append_message("user", "hi", session_id=sid)
        local_memory.append_message(
            "assistant",
            "Try:\n```dispatcher\nmode plan\n```",
            session_id=sid,
        )
        # Append more user turns AFTER the assistant; parser still
        # walks back to the assistant turn.
        local_memory.append_message("user", "another", session_id=sid)
        calls = tool_calls.parse_from_last_assistant(sid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].command, "mode plan")

    def test_only_user_turns_returns_empty(self):
        sid = _unique("tool_history_only_user")
        local_memory.append_message("user", "hi", session_id=sid)
        local_memory.append_message("user", "again", session_id=sid)
        self.assertEqual(tool_calls.parse_from_last_assistant(sid), [])


# ============================================================
# Dispatcher
# ============================================================

class ToolDispatcherTests(unittest.TestCase):
    def test_show_with_no_history(self):
        sid = _unique("tool_disp_show_empty")
        result = dispatch_input("tool show", session_id=sid)
        self.assertEqual(result["action"], "tool")
        self.assertIn("no tool calls", result["output"])

    def test_show_default_subcommand_is_show(self):
        sid = _unique("tool_disp_default")
        result = dispatch_input("tool", session_id=sid)
        self.assertIn("no tool calls", result["output"])

    def test_show_after_assistant_turn(self):
        sid = _unique("tool_disp_after_turn")
        local_memory.append_message("user", "hi", session_id=sid)
        local_memory.append_message(
            "assistant",
            "```dispatcher\nmode plan\n```",
            session_id=sid,
        )
        result = dispatch_input("tool show", session_id=sid)
        self.assertEqual(result["action"], "tool")
        self.assertIn("[1]", result["output"])
        self.assertEqual(len(result["result"]["calls"]), 1)

    def test_run_recursively_dispatches(self):
        sid = _unique("tool_disp_run_recurse")
        local_memory.append_message("user", "hi", session_id=sid)
        local_memory.append_message(
            "assistant",
            "```dispatcher\nmode plan\n```",
            session_id=sid,
        )
        result = dispatch_input("tool run 1", session_id=sid)
        # The recursive dispatch should have called `mode plan`
        # and the result reflects that action.
        self.assertEqual(result["action"], "mode")
        self.assertEqual(result["origin"], "tool_run")
        self.assertEqual(result["tool_call_index"], 1)
        self.assertEqual(local_memory.get_mode(sid), "plan")

    def test_run_index_out_of_range(self):
        sid = _unique("tool_disp_run_oob")
        local_memory.append_message("user", "x", session_id=sid)
        local_memory.append_message(
            "assistant",
            "```dispatcher\nmode plan\n```",
            session_id=sid,
        )
        result = dispatch_input("tool run 5", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_run_with_no_calls(self):
        sid = _unique("tool_disp_run_no_calls")
        result = dispatch_input("tool run 1", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_run_non_integer_index(self):
        sid = _unique("tool_disp_run_bad_idx")
        result = dispatch_input("tool run abc", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_run_no_arg_returns_usage(self):
        sid = _unique("tool_disp_run_no_arg")
        result = dispatch_input("tool run", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")
        self.assertIn("Usage", result["output"])

    def test_parse_subcommand_arbitrary_text(self):
        sid = _unique("tool_disp_parse")
        text = "see this\n```dispatcher\nstatus\n```\n"
        result = dispatch_input(f"tool parse {text}", session_id=sid)
        self.assertEqual(len(result["result"]["calls"]), 1)
        self.assertEqual(result["result"]["calls"][0]["command"], "status")

    def test_parse_no_arg_returns_usage(self):
        sid = _unique("tool_disp_parse_empty")
        result = dispatch_input("tool parse", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_unknown_subcommand(self):
        sid = _unique("tool_disp_unknown")
        result = dispatch_input("tool bogus", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")


# ============================================================
# Phase 2: tool_calls_enabled flag + teaching slot
# ============================================================

class IsEnabledFlagTests(unittest.TestCase):
    def test_default_off(self):
        sid = _unique("tool_enabled_default")
        self.assertFalse(tool_calls.is_enabled(sid))

    def test_set_then_get(self):
        sid = _unique("tool_enabled_set_get")
        tool_calls.set_enabled(sid, True)
        self.assertTrue(tool_calls.is_enabled(sid))
        tool_calls.set_enabled(sid, False)
        self.assertFalse(tool_calls.is_enabled(sid))

    def test_storage_roundtrip(self):
        sid = _unique("tool_enabled_storage")
        tool_calls.set_enabled(sid, True)
        # Survives _normalize_context.
        ctx = local_memory.get_session_context(sid)
        self.assertTrue(ctx["tool_calls_enabled"])

    def test_set_returns_new_value(self):
        sid = _unique("tool_enabled_returns")
        self.assertTrue(tool_calls.set_enabled(sid, True))
        self.assertFalse(tool_calls.set_enabled(sid, False))


class ToolCallsSlotProducerTests(unittest.TestCase):
    def test_returns_none_when_disabled(self):
        from app.services import prompt_assembly as pa
        self.assertIsNone(pa.tool_calls_slot(False))
        self.assertIsNone(pa.tool_calls_slot(None))

    def test_returns_slot_when_enabled(self):
        from app.services import prompt_assembly as pa
        slot = pa.tool_calls_slot(True)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.name, "tool_calls")
        self.assertEqual(slot.priority, 8)
        # Content includes the format marker the model needs.
        self.assertIn("```dispatcher", slot.content)
        self.assertIn("tool run", slot.content)

    def test_slot_priority_between_mode_and_skills(self):
        from app.services import prompt_assembly as pa
        slot = pa.tool_calls_slot(True)
        self.assertGreater(slot.priority, 0)   # > mode (0)
        self.assertLess(slot.priority, 10)     # < skills (10)

    def test_default_budget_pinned(self):
        from app.services import prompt_assembly as pa
        self.assertEqual(pa.SLOT_BUDGET_DEFAULTS["tool_calls"], 2_000)


class ToolCallsSlotEndToEndTests(unittest.TestCase):
    """When the flag is set on session.context, build_runtime_assembly
    emits the slot. Off → slot absent."""

    def test_slot_in_trace_when_flag_set(self):
        from app.services import chat_service
        sid = _unique("tool_slot_e2e_on")
        tool_calls.set_enabled(sid, True)
        ctx = local_memory.get_session_context(sid)
        _, trace = chat_service.build_runtime_assembly(
            {}, [], "hello", session_context=ctx, session_id=sid,
        )
        names = [t["name"] for t in trace]
        self.assertIn("tool_calls", names)

    def test_slot_absent_when_flag_unset(self):
        from app.services import chat_service
        sid = _unique("tool_slot_e2e_off")
        # Default off — don't touch the flag.
        ctx = local_memory.get_session_context(sid)
        _, trace = chat_service.build_runtime_assembly(
            {}, [], "hello", session_context=ctx, session_id=sid,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("tool_calls", names)


class ToolEnableDispatcherTests(unittest.TestCase):
    def test_status_no_arg(self):
        sid = _unique("tool_enable_disp_status")
        result = dispatch_input("tool enable", session_id=sid)
        self.assertEqual(result["action"], "tool")
        self.assertIn("OFF", result["output"])
        self.assertEqual(result["result"]["enabled"], False)

    def test_toggle_on(self):
        sid = _unique("tool_enable_disp_on")
        result = dispatch_input("tool enable on", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertTrue(tool_calls.is_enabled(sid))

    def test_toggle_off(self):
        sid = _unique("tool_enable_disp_off")
        tool_calls.set_enabled(sid, True)
        result = dispatch_input("tool enable off", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertFalse(tool_calls.is_enabled(sid))

    def test_truthy_synonyms(self):
        for flag in ("on", "true", "1", "yes"):
            sid = _unique("tool_enable_disp_synonym")
            dispatch_input(f"tool enable {flag}", session_id=sid)
            self.assertTrue(tool_calls.is_enabled(sid),
                            f"flag {flag!r} should enable")

    def test_falsy_synonyms(self):
        for flag in ("off", "false", "0", "no"):
            sid = _unique("tool_enable_disp_synonym_off")
            tool_calls.set_enabled(sid, True)
            dispatch_input(f"tool enable {flag}", session_id=sid)
            self.assertFalse(tool_calls.is_enabled(sid),
                             f"flag {flag!r} should disable")

    def test_unknown_flag(self):
        sid = _unique("tool_enable_disp_unknown")
        result = dispatch_input("tool enable maybe", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")


# ============================================================
# Phase 3: tool_call kind in pending_approval + chat-flow detection
# ============================================================

class ToolCallNormalisationTests(unittest.TestCase):
    def test_minimal_tool_call_normalised(self):
        sid = _unique("tc_norm_minimal")
        action = {
            "kind": "tool_call",
            "origin": "chat",
            "command": "mode plan",
        }
        local_memory.set_pending_approval(session_id=sid, pending_approval=action)
        stored = local_memory.get_pending_approval(session_id=sid)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["kind"], "tool_call")
        self.assertEqual(stored["command"], "mode plan")
        self.assertEqual(stored["origin"], "chat")

    def test_empty_command_rejected(self):
        sid = _unique("tc_norm_empty")
        local_memory.set_pending_approval(session_id=sid, pending_approval={
            "kind": "tool_call", "origin": "chat", "command": "",
        })
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_whitespace_only_command_rejected(self):
        sid = _unique("tc_norm_whitespace")
        local_memory.set_pending_approval(session_id=sid, pending_approval={
            "kind": "tool_call", "origin": "chat", "command": "   \n  ",
        })
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_tier_field_preserved(self):
        sid = _unique("tc_norm_tier")
        local_memory.set_pending_approval(session_id=sid, pending_approval={
            "kind": "tool_call", "origin": "chat",
            "command": "mode plan", "tier": "L3",
        })
        stored = local_memory.get_pending_approval(session_id=sid)
        self.assertEqual(stored["tier"], "L3")

    def test_no_workspace_required(self):
        # tool_call should work without a workspace pinned (unlike
        # write/delete/bundle which require one).
        sid = _unique("tc_norm_no_ws")
        local_memory.set_pending_approval(session_id=sid, pending_approval={
            "kind": "tool_call", "origin": "chat", "command": "status",
        })
        self.assertIsNotNone(local_memory.get_pending_approval(session_id=sid))


class ChatFlowToolCallDetectionTests(unittest.TestCase):
    """``_maybe_chat_write_proposal`` is the chat-flow entry point;
    Phase 3 extended it to also detect ``dispatcher`` blocks when
    the user has opted into ``tool_calls_enabled``."""

    def test_no_detection_when_flag_off(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        # tool_calls_enabled is False → tool calls in the response are
        # ignored by the chat-flow detector (still discoverable via
        # tool show, but no auto-approval).
        sid = _unique("tc_chatflow_off")
        ctx = local_memory.get_session_context(sid)
        action = _maybe_chat_write_proposal(
            message="x",
            response_text="```dispatcher\nmode plan\n```",
            session_id=sid,
            session_context=ctx,
        )
        self.assertIsNone(action)

    def test_detection_when_flag_on(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        sid = _unique("tc_chatflow_on")
        tool_calls.set_enabled(sid, True)
        ctx = local_memory.get_session_context(sid)
        action = _maybe_chat_write_proposal(
            message="x",
            response_text="Try:\n```dispatcher\nmode plan\n```",
            session_id=sid,
            session_context=ctx,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "tool_call")
        self.assertEqual(action["command"], "mode plan")
        self.assertEqual(action["origin"], "chat")
        self.assertEqual(action["original_message"], "x")

    def test_multi_call_response_emits_bundle(self):
        # Phase 3b changed this: N>1 calls now emit a bundle, not
        # just the first call. (Phase 3a's "first call wins"
        # behaviour was a temporary shape; bundles are the real
        # model.)
        from app.services.chat_service import _maybe_chat_write_proposal
        sid = _unique("tc_chatflow_multi")
        tool_calls.set_enabled(sid, True)
        ctx = local_memory.get_session_context(sid)
        action = _maybe_chat_write_proposal(
            message="x",
            response_text=(
                "First:\n```dispatcher\nrecall auth\n```\n"
                "Then:\n```dispatcher\nmode plan\n```\n"
            ),
            session_id=sid,
            session_context=ctx,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "bundle")
        self.assertEqual(len(action["sub_actions"]), 2)
        self.assertEqual(action["sub_actions"][0]["command"], "recall auth")
        self.assertEqual(action["sub_actions"][1]["command"], "mode plan")

    def test_writes_win_when_both_present(self):
        # Phase 3a explicitly says writes win when a response has
        # both file= writes AND dispatcher blocks. The tool call
        # remains discoverable via `tool show` for manual run.
        import tempfile
        from app.services.chat_service import _maybe_chat_write_proposal
        sid = _unique("tc_chatflow_both")
        tool_calls.set_enabled(sid, True)
        with tempfile.TemporaryDirectory() as ws:
            local_memory.set_workspace(session_id=sid, workspace_path=ws)
            local_memory.set_mode(sid, "build")
            ctx = local_memory.get_session_context(sid)
            response = (
                "Edit:\n```python file=foo.py\nprint('hi')\n```\n"
                "Then:\n```dispatcher\nmode plan\n```\n"
            )
            action = _maybe_chat_write_proposal(
                message="x", response_text=response,
                session_id=sid, session_context=ctx,
            )
        self.assertEqual(action["kind"], "write")
        self.assertEqual(action["path"], "foo.py")

    def test_existing_pending_approval_blocks_detection(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        sid = _unique("tc_chatflow_blocked")
        tool_calls.set_enabled(sid, True)
        # Pre-stage another pending approval.
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "tool_call", "origin": "command",
                "command": "status",
            },
        )
        ctx = local_memory.get_session_context(sid)
        action = _maybe_chat_write_proposal(
            message="x",
            response_text="```dispatcher\nmode plan\n```",
            session_id=sid,
            session_context=ctx,
        )
        self.assertIsNone(action)


class ApproveToolCallTests(unittest.TestCase):
    """Approving a pending tool_call recursively dispatches the
    command. Provenance is recorded on the result dict."""

    def test_approve_dispatches_command(self):
        sid = _unique("tc_approve_run")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "tool_call", "origin": "chat",
                "command": "mode plan",
            },
        )
        result = dispatch_input("approve", session_id=sid)
        self.assertEqual(result["action"], "mode")
        self.assertEqual(result["origin"], "chat_tool_call")
        self.assertEqual(result["tool_call_command"], "mode plan")
        # Side effect: mode flipped to plan.
        self.assertEqual(local_memory.get_mode(sid), "plan")
        # Pending cleared.
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_deny_clears_pending_without_dispatching(self):
        sid = _unique("tc_approve_deny")
        starting_mode = local_memory.get_mode(sid)
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "tool_call", "origin": "chat",
                "command": "mode plan",
            },
        )
        result = dispatch_input("deny", session_id=sid)
        self.assertEqual(result["action"], "deny")
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))
        # Mode unchanged — deny did NOT execute the command.
        self.assertEqual(local_memory.get_mode(sid), starting_mode)


class ToolCallBannerRenderingTests(unittest.TestCase):
    def test_describe_pending_action_truncates_long_command(self):
        from app.services.workspace_service import describe_pending_action
        action = {"kind": "tool_call", "command": "x" * 300}
        desc = describe_pending_action(action)
        self.assertIn("tool call:", desc)
        self.assertIn("...", desc)
        self.assertLess(len(desc), 200)

    def test_impact_statement_mentions_safety_nets(self):
        from app.services.impact_statements import impact_statement_for
        impact = impact_statement_for({
            "kind": "tool_call", "command": "mode plan",
        })
        self.assertIn("dispatcher command", impact)
        self.assertIn("safety", impact)

    def test_default_tier_is_l2(self):
        from app.services.impact_statements import tier_for
        self.assertEqual(tier_for({"kind": "tool_call"}), "L2")

    def test_full_banner_format(self):
        from app.services.workspace_service import format_approval_request
        banner = format_approval_request({
            "kind": "tool_call", "origin": "chat",
            "command": "recall auth flow",
            "reason": "chat-flow ```dispatcher block",
        })
        self.assertIn("Tier: L2", banner)
        self.assertIn("tool call: recall auth flow", banner)
        self.assertIn("Impact:", banner)


# ============================================================
# Phase 3b: tool-call bundles (N>1 calls)
# ============================================================

class ToolCallBundleNormalisationTests(unittest.TestCase):
    """The bundle normaliser accepts ``tool_call`` sub-actions.
    Tool-call-only bundles can omit the bundle-level
    ``workspace_path`` (writes/deletes still require it)."""

    def test_tool_call_only_bundle_no_workspace_required(self):
        sid = _unique("bundle_tc_only")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "status"},
                    {"kind": "tool_call", "command": "mode plan"},
                ],
            },
        )
        stored = local_memory.get_pending_approval(session_id=sid)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["kind"], "bundle")
        self.assertEqual(len(stored["sub_actions"]), 2)
        self.assertEqual(stored["sub_actions"][0]["kind"], "tool_call")
        # Workspace path NOT required for tool-call-only bundles.
        self.assertNotIn("workspace_path", stored)

    def test_mixed_bundle_requires_workspace(self):
        # write + tool_call → workspace_path required (for the
        # write half).
        sid = _unique("bundle_mixed_no_ws")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "write", "path": "x.py", "content": "x"},
                    {"kind": "tool_call", "command": "status"},
                ],
                # workspace_path omitted
            },
        )
        # Refused by normaliser.
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_unknown_sub_kind_rejected(self):
        sid = _unique("bundle_unknown_sub")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "status"},
                    {"kind": "bogus_kind"},
                ],
            },
        )
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_empty_command_in_sub_rejects_bundle(self):
        sid = _unique("bundle_empty_cmd")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "status"},
                    {"kind": "tool_call", "command": ""},
                ],
            },
        )
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))


class BundleComposerToolCallTests(unittest.TestCase):
    def test_n_tool_calls_compose_into_bundle(self):
        from app.services import patches
        actions = [
            {"kind": "tool_call", "command": "status"},
            {"kind": "tool_call", "command": "models"},
        ]
        bundle = patches.bundle_from_action_dicts(
            actions, rationale="test", origin="chat",
        )
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle["kind"], "bundle")
        self.assertEqual(len(bundle["sub_actions"]), 2)
        # Tool-call-only bundle has NO workspace_path.
        self.assertNotIn("workspace_path", bundle)

    def test_unknown_kind_in_composer_returns_none(self):
        from app.services import patches
        actions = [
            {"kind": "tool_call", "command": "status"},
            {"kind": "what_even", "stuff": True},
        ]
        self.assertIsNone(patches.bundle_from_action_dicts(actions))

    def test_describe_summary_renders_tool_call_section(self):
        from app.services.patches import describe_bundle_summary
        action = {
            "kind": "bundle",
            "sub_actions": [
                {"kind": "tool_call", "command": "status"},
                {"kind": "tool_call", "command": "mode plan"},
            ],
        }
        summary = describe_bundle_summary(action)
        self.assertIn("2 tool calls", summary)
        self.assertIn("status", summary)
        self.assertIn("mode plan", summary)

    def test_describe_summary_handles_mixed_bundle(self):
        from app.services.patches import describe_bundle_summary
        action = {
            "kind": "bundle",
            "sub_actions": [
                {"kind": "write", "path": "foo.py"},
                {"kind": "tool_call", "command": "status"},
            ],
        }
        summary = describe_bundle_summary(action)
        # Both file and tool-call sections appear.
        self.assertIn("file change", summary)
        self.assertIn("tool call", summary)


class ChatFlowMultiToolCallTests(unittest.TestCase):
    """When the model emits N>1 ``dispatcher`` blocks AND
    ``tool_calls_enabled`` is on, the chat-flow detector emits a
    bundle (Phase 3b). Single calls still emit a bare tool_call
    (Phase 3a)."""

    def test_two_calls_emit_bundle(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        sid = _unique("multi_tc_bundle")
        tool_calls.set_enabled(sid, True)
        ctx = local_memory.get_session_context(sid)
        action = _maybe_chat_write_proposal(
            message="x",
            response_text=(
                "First:\n```dispatcher\nstatus\n```\n"
                "Second:\n```dispatcher\nmode plan\n```\n"
            ),
            session_id=sid,
            session_context=ctx,
        )
        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "bundle")
        self.assertEqual(len(action["sub_actions"]), 2)
        for sub in action["sub_actions"]:
            self.assertEqual(sub["kind"], "tool_call")

    def test_single_call_still_bare_tool_call(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        sid = _unique("single_tc_bare")
        tool_calls.set_enabled(sid, True)
        ctx = local_memory.get_session_context(sid)
        action = _maybe_chat_write_proposal(
            message="x",
            response_text="```dispatcher\nstatus\n```",
            session_id=sid,
            session_context=ctx,
        )
        self.assertIsNotNone(action)
        # Single call → not bundled.
        self.assertEqual(action["kind"], "tool_call")
        self.assertEqual(action["command"], "status")


class ApproveToolCallBundleTests(unittest.TestCase):
    def test_approve_dispatches_each_in_order(self):
        sid = _unique("tc_bundle_approve_all")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "mode plan"},
                    {"kind": "tool_call", "command": "mode build"},
                ],
            },
        )
        result = dispatch_input("approve", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertEqual(result["result"]["tool_calls_applied"], 2)
        # Final mode is "build" because second call ran last.
        self.assertEqual(local_memory.get_mode(sid), "build")
        # Pending cleared.
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_tool_call_only_bundle_does_not_require_build_mode(self):
        sid = _unique("tc_bundle_no_build")
        local_memory.set_mode(sid, "plan")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "status"},
                    {"kind": "tool_call", "command": "models"},
                ],
            },
        )
        result = dispatch_input("approve", session_id=sid)
        # Must NOT refuse with mode_required (Plan mode is fine
        # for read-only tool calls).
        self.assertNotEqual(result.get("reason"), "mode_required")

    def test_failure_in_tool_call_stops_remaining(self):
        sid = _unique("tc_bundle_fail")
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "mode plan"},
                    {"kind": "tool_call",
                     "command": "tool run nonexistent_subcommand_xyz"},
                    # This third call should NEVER run.
                    {"kind": "tool_call", "command": "mode build"},
                ],
            },
        )
        result = dispatch_input("approve", session_id=sid)
        self.assertIn("reason", result)
        # First call ran (mode → plan); third never ran (mode
        # stayed plan, didn't flip back to build).
        self.assertEqual(local_memory.get_mode(sid), "plan")
        # The result records what got through.
        self.assertEqual(result["result"]["applied_tool_calls"], 1)
        self.assertEqual(result["result"]["failed_at_index"], 1)

    def test_deny_clears_bundle_without_dispatching(self):
        sid = _unique("tc_bundle_deny")
        starting_mode = local_memory.get_mode(sid)
        local_memory.set_pending_approval(
            session_id=sid,
            pending_approval={
                "kind": "bundle",
                "origin": "chat",
                "sub_actions": [
                    {"kind": "tool_call", "command": "mode plan"},
                    {"kind": "tool_call", "command": "mode build"},
                ],
            },
        )
        result = dispatch_input("deny", session_id=sid)
        self.assertEqual(result["action"], "deny")
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))
        # Mode unchanged — no dispatch happened.
        self.assertEqual(local_memory.get_mode(sid), starting_mode)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
