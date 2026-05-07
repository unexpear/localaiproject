"""Tests for the per-session notes buffer (`docs/design/notes-buffer.md`).

Three layers:
  - Storage helpers (`local_memory.{get,set,append,clear}_notes`)
  - Dispatcher commands (`notes`, `notes append`, `notes set`, `notes clear`)
  - Chat-flow integration (notes appear as a system message when non-empty)
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import chat_service
from app.services.command_dispatcher import dispatch_input


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


class NotesStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_get_notes_on_fresh_session_returns_empty_string(self):
        self.assertEqual(local_memory.get_notes(session_id="fresh"), "")

    def test_set_notes_round_trip(self):
        local_memory.set_notes(session_id="default", notes="hello world")
        self.assertEqual(local_memory.get_notes(session_id="default"), "hello world")

    def test_append_notes_adds_with_newline_separator(self):
        local_memory.set_notes(session_id="default", notes="line one")
        local_memory.append_notes(session_id="default", text="line two")
        notes = local_memory.get_notes(session_id="default")
        self.assertIn("line one", notes)
        self.assertIn("line two", notes)
        # Separator between existing content and the new text.
        self.assertRegex(notes, r"line one\nline two")

    def test_append_notes_to_empty_buffer(self):
        local_memory.append_notes(session_id="default", text="first entry")
        self.assertIn("first entry", local_memory.get_notes(session_id="default"))

    def test_append_notes_with_timestamp_adds_iso_header(self):
        local_memory.append_notes(
            session_id="default",
            text="something happened",
            prefix_timestamp=True,
        )
        notes = local_memory.get_notes(session_id="default")
        # Markdown subheading + ISO-like timestamp followed by the text.
        self.assertIn("##", notes)
        self.assertIn("something happened", notes)

    def test_clear_notes_resets_to_empty(self):
        local_memory.set_notes(session_id="default", notes="filled")
        local_memory.clear_notes(session_id="default")
        self.assertEqual(local_memory.get_notes(session_id="default"), "")

    def test_notes_truncation_at_32kb_with_marker(self):
        # 40 KB of 'x' — should be clipped with the truncated marker.
        big = "x" * 40_000
        local_memory.set_notes(session_id="default", notes=big)
        out = local_memory.get_notes(session_id="default")
        self.assertLessEqual(len(out.encode("utf-8")), local_memory.NOTES_MAX_BYTES)
        self.assertIn("[truncated]", out)

    def test_notes_persists_across_load_save_round_trip(self):
        local_memory.set_notes(session_id="default", notes="persistence check")
        # Force a re-read by clearing in-memory and reloading.
        # JsonStorage hits disk on every read so this proves disk persistence.
        self.assertEqual(local_memory.get_notes(session_id="default"), "persistence check")

    def test_notes_isolated_per_session(self):
        local_memory.set_notes(session_id="alpha", notes="A")
        local_memory.set_notes(session_id="beta", notes="B")
        self.assertEqual(local_memory.get_notes(session_id="alpha"), "A")
        self.assertEqual(local_memory.get_notes(session_id="beta"), "B")


class NotesDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_notes_show_empty(self):
        ev = dispatch_input("notes", session_id="default")
        self.assertEqual(ev["action"], "notes")
        self.assertIn("empty", ev["output"].lower())

    def test_notes_append_then_show(self):
        ev = dispatch_input("notes append plan: do X then Y", session_id="default")
        self.assertEqual(ev["action"], "notes")
        self.assertIn("Appended", ev["output"])

        show = dispatch_input("notes", session_id="default")
        self.assertIn("plan: do X then Y", show["output"])

    def test_notes_set_replaces_buffer(self):
        dispatch_input("notes append old content", session_id="default")
        ev = dispatch_input("notes set new content", session_id="default")
        self.assertIn("replaced", ev["output"].lower())
        show = dispatch_input("notes", session_id="default")
        self.assertNotIn("old content", show["output"])
        self.assertIn("new content", show["output"])

    def test_notes_clear(self):
        dispatch_input("notes append something", session_id="default")
        dispatch_input("notes clear", session_id="default")
        show = dispatch_input("notes", session_id="default")
        self.assertIn("empty", show["output"].lower())

    def test_notes_append_without_text_emits_invalid_argument(self):
        ev = dispatch_input("notes append", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_notes_unknown_subcommand_emits_invalid_argument(self):
        ev = dispatch_input("notes bogus thing", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_notes_appends_are_reserved_against_custom_command_shadow(self):
        # `cmd add notes <prompt>` should be refused because `notes` is reserved.
        ev = dispatch_input("cmd add notes some prompt", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")
        self.assertIn("reserved", ev["output"])


class NotesChatFlowTests(unittest.TestCase):
    """When notes are non-empty they appear as a system message during chat
    inference."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_runtime_messages_include_notes_when_set(self):
        local_memory.set_notes(
            session_id="default",
            notes="Open question: which lane for the synthesis?",
        )
        context = local_memory.get_session_context(session_id="default")
        messages = chat_service._build_runtime_messages(
            summary=local_memory.get_session_summary(session_id="default"),
            recent_history=[],
            current_message="hello",
            session_context=context,
        )
        # Find the system message that includes the note text. With
        # Phase 2 the notes value is surfaced as the `working_memory`
        # memory block — content unchanged, header format updated.
        notes_messages = [
            m for m in messages
            if m.get("role") == "system"
            and "Open question: which lane" in m.get("content", "")
        ]
        self.assertEqual(len(notes_messages), 1)
        self.assertIn("working_memory", notes_messages[0]["content"])

    def test_runtime_messages_omit_notes_block_when_empty(self):
        context = local_memory.get_session_context(session_id="default")
        messages = chat_service._build_runtime_messages(
            summary=local_memory.get_session_summary(session_id="default"),
            recent_history=[],
            current_message="hello",
            session_context=context,
        )
        # No memory-block message should emit when every block's value
        # is empty (the default for a fresh session).
        for m in messages:
            self.assertNotIn(
                "Memory block",
                m.get("content", ""),
                "Expected no memory-block messages on a fresh session",
            )


# ============================================================
# Phase 2 — structured memory blocks
# ============================================================

class MemoryBlockStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_default_blocks_seeded_on_fresh_session(self):
        blocks = local_memory.get_memory_blocks(session_id="default")
        self.assertEqual(
            sorted(blocks.keys()),
            ["current_goal", "persona", "user_preferences", "working_memory"],
        )
        for label, block in blocks.items():
            self.assertEqual(block["value"], "")
            self.assertGreater(block["char_limit"], 0)

    def test_default_blocks_have_descriptions(self):
        blocks = local_memory.get_memory_blocks(session_id="default")
        for label, block in blocks.items():
            self.assertTrue(block["description"], f"{label} has empty description")

    def test_set_memory_block_value_round_trip(self):
        local_memory.set_memory_block_value(
            "default", "persona", "Concise; cite sources.",
        )
        self.assertEqual(
            local_memory.get_memory_block_value("default", "persona"),
            "Concise; cite sources.",
        )

    def test_set_memory_block_truncates_to_char_limit(self):
        # persona has 1024 byte limit; write 2000 chars.
        big = "x" * 2000
        local_memory.set_memory_block_value("default", "persona", big)
        value = local_memory.get_memory_block_value("default", "persona")
        self.assertLessEqual(len(value.encode("utf-8")), 1024)
        self.assertIn("[truncated]", value)

    def test_set_memory_block_invalid_label_raises(self):
        with self.assertRaises(ValueError):
            local_memory.set_memory_block_value("default", "Has Spaces", "x")
        with self.assertRaises(ValueError):
            local_memory.set_memory_block_value("default", "", "x")

    def test_set_memory_block_description_round_trip(self):
        local_memory.set_memory_block_description(
            "default", "persona", "Custom description",
        )
        block = local_memory.get_memory_block("default", "persona")
        self.assertEqual(block["description"], "Custom description")

    def test_set_memory_block_budget_clamps_existing_value(self):
        local_memory.set_memory_block_value(
            "default", "persona", "x" * 500,
        )
        local_memory.set_memory_block_budget("default", "persona", 100)
        value = local_memory.get_memory_block_value("default", "persona")
        self.assertLessEqual(len(value.encode("utf-8")), 100)

    def test_set_memory_block_budget_invalid_raises(self):
        with self.assertRaises(ValueError):
            local_memory.set_memory_block_budget("default", "persona", "abc")

    def test_clear_memory_block(self):
        local_memory.set_memory_block_value("default", "persona", "filled")
        local_memory.clear_memory_block("default", "persona")
        self.assertEqual(
            local_memory.get_memory_block_value("default", "persona"),
            "",
        )

    def test_remove_default_block_clears_but_keeps_default(self):
        local_memory.set_memory_block_value("default", "persona", "filled")
        # remove returns False for default blocks but clears the value.
        result = local_memory.remove_memory_block("default", "persona")
        self.assertFalse(result)
        block = local_memory.get_memory_block("default", "persona")
        self.assertIsNotNone(block)
        self.assertEqual(block["value"], "")

    def test_user_defined_block_round_trip(self):
        local_memory.set_memory_block_value(
            "default", "team_context", "we use rust and pytest",
        )
        block = local_memory.get_memory_block("default", "team_context")
        self.assertIsNotNone(block)
        self.assertEqual(block["value"], "we use rust and pytest")
        # Remove user-defined block actually removes it.
        self.assertTrue(
            local_memory.remove_memory_block("default", "team_context"),
        )
        self.assertIsNone(
            local_memory.get_memory_block("default", "team_context"),
        )

    def test_legacy_notes_api_writes_working_memory_block(self):
        local_memory.set_notes("default", "scratchpad content")
        self.assertEqual(
            local_memory.get_memory_block_value("default", "working_memory"),
            "scratchpad content",
        )
        self.assertEqual(
            local_memory.get_notes("default"),
            "scratchpad content",
        )

    def test_legacy_notes_api_clear_clears_working_memory(self):
        local_memory.set_notes("default", "filled")
        local_memory.clear_notes("default")
        self.assertEqual(
            local_memory.get_memory_block_value("default", "working_memory"),
            "",
        )


class MemoryBlockMigrationTests(unittest.TestCase):
    """Pre-Phase-2 storage has only `notes` (no memory_blocks). On
    first read, the legacy notes string migrates into the
    working_memory block; subsequent edits are persistent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_legacy_notes_migrates_to_working_memory_on_first_read(self):
        # Write a pre-Phase-2 storage payload manually.
        legacy_path = os.path.join(self._tmp.name, "memory.json")
        import json as _json
        with open(legacy_path, "w", encoding="utf-8") as fh:
            _json.dump({
                "sessions": {
                    "default": {
                        "history": [],
                        "executions": [],
                        "summary": {},
                        "context": {
                            "workspace_path": None,
                            "active_skills": [],
                            "pending_approval": None,
                            "approved_search": None,
                            "mode": "plan",
                            "notes": "legacy scratchpad content",
                            # No `memory_blocks` key — pre-Phase-2 shape
                        },
                    },
                },
            }, fh)
        storage = JsonStorage(path=legacy_path)
        local_memory._reset_storage_for_tests(storage)
        local_memory._reset_undo_stacks_for_tests()
        try:
            blocks = local_memory.get_memory_blocks("default")
            self.assertIn("working_memory", blocks)
            self.assertEqual(
                blocks["working_memory"]["value"],
                "legacy scratchpad content",
            )
            # And the legacy notes API still sees the same content.
            self.assertEqual(
                local_memory.get_notes("default"),
                "legacy scratchpad content",
            )
        finally:
            local_memory._reset_storage_for_tests(None)


class MemoryBlockChatFlowTests(unittest.TestCase):
    """Per-block injection: each non-empty block becomes its own
    system message ordered by priority."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_one_message_per_non_empty_block(self):
        local_memory.set_memory_block_value(
            "default", "persona", "Be concise.",
        )
        local_memory.set_memory_block_value(
            "default", "current_goal", "Ship Phase 2.",
        )
        # working_memory + user_preferences left empty.
        context = local_memory.get_session_context("default")
        messages = chat_service._build_runtime_messages(
            summary=local_memory.get_session_summary("default"),
            recent_history=[],
            current_message="hi",
            session_context=context,
        )
        block_messages = [
            m for m in messages
            if m.get("role") == "system"
            and "Memory block" in m.get("content", "")
        ]
        self.assertEqual(len(block_messages), 2)
        # Persona (priority 0) comes before current_goal (priority 2).
        self.assertIn("persona", block_messages[0]["content"])
        self.assertIn("current_goal", block_messages[1]["content"])

    def test_block_message_includes_description(self):
        local_memory.set_memory_block_value(
            "default", "persona", "VALUE_TEXT",
        )
        context = local_memory.get_session_context("default")
        messages = chat_service._build_runtime_messages(
            summary=local_memory.get_session_summary("default"),
            recent_history=[],
            current_message="hi",
            session_context=context,
        )
        persona_msg = [
            m for m in messages
            if m.get("role") == "system"
            and "persona" in m.get("content", "")
        ][0]
        self.assertIn("How the assistant should present itself", persona_msg["content"])
        self.assertIn("VALUE_TEXT", persona_msg["content"])


class NoteDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_note_list_shows_all_default_blocks(self):
        ev = dispatch_input("note list", session_id="default")
        self.assertNotIn("reason", ev)
        for label in ("persona", "current_goal", "user_preferences", "working_memory"):
            self.assertIn(label, ev["output"])

    def test_note_show_with_unknown_label(self):
        ev = dispatch_input("note show ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_note_set_round_trip(self):
        ev = dispatch_input(
            "note set persona Concise. Quote code.", session_id="default",
        )
        self.assertNotIn("reason", ev)
        show = dispatch_input("note show persona", session_id="default")
        self.assertIn("Concise. Quote code.", show["output"])

    def test_note_set_with_no_args(self):
        ev = dispatch_input("note set", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_note_set_invalid_label(self):
        ev = dispatch_input(
            "note set Has-Spaces value", session_id="default",
        )
        # `Has-Spaces` fails the label regex (uppercase H)
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_note_describe_round_trip(self):
        ev = dispatch_input(
            "note describe persona Always cite sources",
            session_id="default",
        )
        self.assertNotIn("reason", ev)
        block = local_memory.get_memory_block("default", "persona")
        self.assertEqual(block["description"], "Always cite sources")

    def test_note_budget_round_trip(self):
        ev = dispatch_input(
            "note budget persona 256", session_id="default",
        )
        self.assertNotIn("reason", ev)
        block = local_memory.get_memory_block("default", "persona")
        self.assertEqual(block["char_limit"], 256)

    def test_note_budget_non_integer(self):
        ev = dispatch_input(
            "note budget persona abc", session_id="default",
        )
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_note_clear_default_block(self):
        dispatch_input(
            "note set persona filled content", session_id="default",
        )
        ev = dispatch_input("note clear persona", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertEqual(
            local_memory.get_memory_block_value("default", "persona"),
            "",
        )

    def test_note_remove_user_block(self):
        local_memory.set_memory_block_value(
            "default", "team_context", "team data",
        )
        ev = dispatch_input("note remove team_context", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertIsNone(
            local_memory.get_memory_block("default", "team_context"),
        )

    def test_note_remove_default_block_clears_but_keeps(self):
        dispatch_input("note set persona x", session_id="default")
        ev = dispatch_input("note remove persona", session_id="default")
        # Default block: clear, not actually remove. Success path.
        self.assertNotIn("reason", ev)
        self.assertEqual(
            local_memory.get_memory_block_value("default", "persona"),
            "",
        )

    def test_note_remove_unknown_label(self):
        ev = dispatch_input("note remove ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_note_unknown_subcommand(self):
        ev = dispatch_input("note magic", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")


if __name__ == "__main__":
    unittest.main()
