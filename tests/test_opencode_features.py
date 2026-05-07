"""Tests for OpenCode-inspired features:
  - Plan/Build mode toggle
  - @file mentions in chat
  - Custom slash commands
  - /undo and /redo on memory mutations
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services.command_dispatcher import (
    _RESERVED_CMD_NAMES,
    _try_resolve_custom_command,
    dispatch_input,
)
from app.services.mentions_service import (
    expand_mentions,
    extract_mentions,
    format_expansion_summary,
)


def _isolated_memory(tmp_dir):
    """Replace the local_memory storage backend with a tempfile-backed JSON store."""
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# ============================================================
# Plan / Build mode
# ============================================================

class PlanBuildModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_default_mode_is_plan(self):
        self.assertEqual(local_memory.get_mode(session_id="default"), "plan")

    def test_set_mode_round_trips(self):
        applied = local_memory.set_mode(session_id="default", mode="build")
        self.assertEqual(applied, "build")
        self.assertEqual(local_memory.get_mode(session_id="default"), "build")

    def test_set_mode_rejects_unknown(self):
        with self.assertRaises(ValueError):
            local_memory.set_mode(session_id="default", mode="ghost")

    def test_dispatch_mode_shows_current(self):
        event = dispatch_input("mode", session_id="default")
        self.assertEqual(event["action"], "mode")
        self.assertIn("plan", event["output"])

    def test_dispatch_mode_build_switches(self):
        event = dispatch_input("mode build", session_id="default")
        self.assertEqual(event["action"], "mode")
        self.assertIn("Mode set to build", event["output"])
        self.assertEqual(local_memory.get_mode(session_id="default"), "build")

    def test_dispatch_mode_garbage_returns_usage(self):
        event = dispatch_input("mode banana", session_id="default")
        self.assertIn("Usage:", event["output"])

    def test_chat_runtime_messages_contain_mode_hint_for_plan(self):
        from app.services import chat_service

        messages = chat_service._build_runtime_messages(
            None,
            [],
            "hi",
            session_context={"mode": "plan"},
        )
        # The mode message should appear before history.
        mode_msgs = [m for m in messages if m.get("role") == "system" and "Mode:" in m.get("content", "")]
        self.assertTrue(any("PLAN" in m["content"] for m in mode_msgs))

    def test_chat_runtime_messages_contain_mode_hint_for_build(self):
        from app.services import chat_service

        messages = chat_service._build_runtime_messages(
            None,
            [],
            "hi",
            session_context={"mode": "build"},
        )
        mode_msgs = [m for m in messages if m.get("role") == "system" and "Mode:" in m.get("content", "")]
        self.assertTrue(any("BUILD" in m["content"] for m in mode_msgs))


# ============================================================
# @file mentions
# ============================================================

class MentionsParserTests(unittest.TestCase):
    def test_extract_simple_mention(self):
        self.assertEqual(extract_mentions("explain @app/router.py please"), ["app/router.py"])

    def test_extract_multiple_unique_in_order(self):
        text = "compare @a.py with @b.py and @a.py again"
        self.assertEqual(extract_mentions(text), ["a.py", "b.py"])

    def test_email_addresses_are_not_mentions(self):
        self.assertEqual(extract_mentions("contact me at user@example.com"), [])

    def test_mention_strips_trailing_punctuation(self):
        self.assertEqual(extract_mentions("look at @router.py."), ["router.py"])
        self.assertEqual(extract_mentions("@a.py, @b.py?"), ["a.py", "b.py"])

    def test_empty_text_returns_empty(self):
        self.assertEqual(extract_mentions(""), [])
        self.assertEqual(extract_mentions(None), [])


class MentionsExpansionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Build a fake workspace with one file.
        self.workspace = self._tmp.name
        with open(os.path.join(self.workspace, "hello.py"), "w", encoding="utf-8") as fh:
            fh.write("def hi():\n    return 'hi'\n")
        os.makedirs(os.path.join(self.workspace, "sub"), exist_ok=True)
        with open(os.path.join(self.workspace, "sub", "deep.txt"), "w", encoding="utf-8") as fh:
            fh.write("deep content\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_expand_no_mentions_returns_unchanged(self):
        out = expand_mentions("nothing to expand", self.workspace)
        self.assertEqual(out["context_block"], "")
        self.assertEqual(out["stripped"], "nothing to expand")
        self.assertEqual(out["expanded"], [])

    def test_expand_inlines_file_content(self):
        out = expand_mentions("explain @hello.py", self.workspace)
        self.assertIn("def hi():", out["context_block"])
        self.assertEqual(out["expanded"][0]["ok"], True)
        self.assertEqual(out["stripped"], "explain hello.py")

    def test_expand_handles_subdirectories(self):
        out = expand_mentions("look @sub/deep.txt", self.workspace)
        self.assertIn("deep content", out["context_block"])

    def test_expand_skips_unknown_paths(self):
        out = expand_mentions("@nope.py", self.workspace)
        self.assertEqual(out["context_block"], "")
        self.assertEqual(out["expanded"][0]["ok"], False)
        self.assertEqual(out["expanded"][0]["reason"], "path_not_found")

    def test_expand_with_no_workspace_skips_all(self):
        out = expand_mentions("@whatever.py", workspace_path=None)
        self.assertEqual(out["context_block"], "")
        self.assertEqual(out["expanded"][0]["reason"], "no_workspace")
        self.assertFalse(out["had_workspace"])

    def test_expand_truncates_oversized_files(self):
        big = os.path.join(self.workspace, "big.txt")
        with open(big, "w", encoding="utf-8") as fh:
            fh.write("x" * 50000)
        out = expand_mentions("@big.txt", self.workspace, per_file_limit=200, total_limit=500)
        self.assertTrue(out["expanded"][0]["truncated"])
        self.assertIn("(truncated)", out["context_block"])

    def test_format_expansion_summary_renders_outcomes(self):
        expansion = {"expanded": [
            {"path": "hello.py", "ok": True, "truncated": False},
            {"path": "missing.py", "ok": False, "reason": "path_not_found"},
        ]}
        summary = format_expansion_summary(expansion)
        self.assertIn("hello.py loaded", summary)
        self.assertIn("missing.py skipped", summary)


# ============================================================
# Custom slash commands
# ============================================================

class CustomCommandsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_add_and_get_round_trip(self):
        local_memory.add_custom_command("standup", "Summarise yesterday's commits")
        entry = local_memory.get_custom_command("standup")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["prompt"], "Summarise yesterday's commits")

    def test_add_rejects_invalid_name(self):
        with self.assertRaises(ValueError):
            local_memory.add_custom_command("BAD NAME", "x")
        with self.assertRaises(ValueError):
            local_memory.add_custom_command("", "x")

    def test_add_rejects_empty_prompt(self):
        with self.assertRaises(ValueError):
            local_memory.add_custom_command("ok", "   ")

    def test_remove_returns_true_when_present(self):
        local_memory.add_custom_command("temp", "hello")
        self.assertTrue(local_memory.remove_custom_command("temp"))
        self.assertFalse(local_memory.remove_custom_command("temp"))

    def test_dispatch_cmd_add_then_show(self):
        event = dispatch_input("cmd add refactor Refactor the function I name", session_id="default")
        self.assertEqual(event["action"], "cmd")
        self.assertIn("Saved /refactor", event["output"])

        event = dispatch_input("cmd show refactor", session_id="default")
        self.assertIn("Refactor the function I name", event["output"])

    def test_dispatch_cmd_blocks_reserved_names(self):
        event = dispatch_input("cmd add help my prompt", session_id="default")
        self.assertIn("reserved", event["output"])

    def test_dispatch_cmd_list_empty_message(self):
        event = dispatch_input("cmd list", session_id="default")
        self.assertIn("No custom commands", event["output"])

    def test_try_resolve_custom_command_substitutes_prompt(self):
        local_memory.add_custom_command("greet", "Say hello to")
        result = _try_resolve_custom_command("/greet Alice")
        self.assertEqual(result, "Say hello to Alice")

    def test_try_resolve_custom_command_with_no_args(self):
        local_memory.add_custom_command("ping", "Send a status ping")
        self.assertEqual(_try_resolve_custom_command("/ping"), "Send a status ping")

    def test_try_resolve_returns_none_for_reserved(self):
        # Built-in `help` should never resolve as a custom command even if the user
        # somehow saved one (we block at add time, but defensively check at lookup too).
        self.assertIsNone(_try_resolve_custom_command("/help"))

    def test_try_resolve_returns_none_for_unknown(self):
        self.assertIsNone(_try_resolve_custom_command("/never-defined"))

    def test_reserved_set_includes_all_dispatcher_built_ins(self):
        for name in {"help", "status", "models", "doctor", "executions", "mode", "undo", "redo", "cmd", "exit"}:
            self.assertIn(name, _RESERVED_CMD_NAMES)


# ============================================================
# Undo / Redo
# ============================================================

class UndoRedoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_undo_restores_previous_state(self):
        local_memory.set_workspace(session_id="default", workspace_path="C:/first")
        local_memory.set_workspace(session_id="default", workspace_path="C:/second")
        self.assertEqual(local_memory.get_session_context()["workspace_path"], "C:/second")

        self.assertTrue(local_memory.undo())
        self.assertEqual(local_memory.get_session_context()["workspace_path"], "C:/first")

    def test_undo_then_redo_returns_to_post_state(self):
        local_memory.set_workspace(session_id="default", workspace_path="C:/first")
        local_memory.set_workspace(session_id="default", workspace_path="C:/second")
        local_memory.undo()
        self.assertTrue(local_memory.redo())
        self.assertEqual(local_memory.get_session_context()["workspace_path"], "C:/second")

    def test_undo_with_empty_stack_returns_false(self):
        self.assertFalse(local_memory.undo())

    def test_new_mutation_clears_redo_stack(self):
        local_memory.set_workspace(session_id="default", workspace_path="C:/a")
        local_memory.set_workspace(session_id="default", workspace_path="C:/b")
        local_memory.undo()
        self.assertTrue(local_memory.can_redo())
        # New mutation invalidates redo
        local_memory.set_workspace(session_id="default", workspace_path="C:/c")
        self.assertFalse(local_memory.can_redo())

    def test_undo_redo_round_trip_for_append_message(self):
        local_memory.append_message("user", "hello", session_id="default")
        self.assertEqual(len(local_memory.get_history()), 1)
        local_memory.undo()
        self.assertEqual(len(local_memory.get_history()), 0)
        local_memory.redo()
        self.assertEqual(len(local_memory.get_history()), 1)

    def test_undo_redo_dispatch_commands(self):
        local_memory.set_workspace(session_id="default", workspace_path="C:/x")
        ev = dispatch_input("undo", session_id="default")
        self.assertEqual(ev["action"], "undo")
        self.assertIn("Reverted", ev["output"])
        ev = dispatch_input("redo", session_id="default")
        self.assertEqual(ev["action"], "redo")
        self.assertIn("Re-applied", ev["output"])

    def test_undo_dispatch_with_empty_stack_says_nothing(self):
        ev = dispatch_input("undo", session_id="default")
        self.assertIn("Nothing to undo", ev["output"])

    def test_capacity_bound(self):
        # Push more than the capacity worth of mutations; the deque should drop oldest.
        for i in range(local_memory.UNDO_STACK_CAPACITY + 10):
            local_memory.set_workspace(session_id="default", workspace_path=f"C:/p{i}")
        self.assertEqual(len(local_memory._undo_stack), local_memory.UNDO_STACK_CAPACITY)


if __name__ == "__main__":
    unittest.main()
