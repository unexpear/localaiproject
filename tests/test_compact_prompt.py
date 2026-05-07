"""Tests for the compact-prompt opt-in (Phase 1).

See ``docs/design/compact-prompt.md``. The feature is purely additive:
when the per-session ``compact_format`` flag is True, slot producers
that support it strip their verbose framing in favour of a tight
``## Header\\n<body>`` form. The flag is off by default so existing
behaviour is preserved.

These tests cover:

* The ``compact_format`` flag round-trips through session context
  via ``is_compact_enabled`` / ``set_compact``.
* Each compact slot renderer preserves the underlying body content
  (no information loss — the framing is what's stripped).
* The flag threads through ``build_runtime_assembly`` so a session
  with ``compact_format=True`` produces shorter system messages
  than the same session with the default flag.
* The ``prompt compact [on|off]`` dispatcher subcommand parses
  arguments + persists the flag.
"""
import os
import tempfile
import unittest
from uuid import uuid4

from app.memory import local_memory
from app.services import prompt_assembly as pa
from app.services.chat_service import build_runtime_assembly
from app.services.command_dispatcher import dispatch_input


def _fresh_session():
    """Allocate a unique session id for each test so we don't leak
    flag state across cases that share the conftest tempdir."""
    return f"compact_{uuid4().hex[:10]}"


# ============================================================
# Flag round-trip
# ============================================================

class CompactFlagRoundTripTests(unittest.TestCase):
    def test_default_off(self):
        sid = _fresh_session()
        self.assertFalse(pa.is_compact_enabled(sid))

    def test_set_compact_returns_new_value(self):
        sid = _fresh_session()
        self.assertTrue(pa.set_compact(sid, True))
        self.assertTrue(pa.is_compact_enabled(sid))
        self.assertFalse(pa.set_compact(sid, False))
        self.assertFalse(pa.is_compact_enabled(sid))

    def test_flag_persists_across_get_calls(self):
        sid = _fresh_session()
        pa.set_compact(sid, True)
        # Two reads — same answer.
        self.assertTrue(pa.is_compact_enabled(sid))
        self.assertTrue(pa.is_compact_enabled(sid))

    def test_flag_isolated_per_session(self):
        sid_a = _fresh_session()
        sid_b = _fresh_session()
        pa.set_compact(sid_a, True)
        # B's flag is unchanged.
        self.assertTrue(pa.is_compact_enabled(sid_a))
        self.assertFalse(pa.is_compact_enabled(sid_b))

    def test_default_context_includes_compact_format_field(self):
        ctx = local_memory._default_context()
        self.assertIn("compact_format", ctx)
        self.assertFalse(ctx["compact_format"])

    def test_normalize_context_coerces_compact_format(self):
        normalized = local_memory._normalize_context({"compact_format": "yes"})
        # Truthy string → True.
        self.assertTrue(normalized["compact_format"])
        normalized = local_memory._normalize_context({"compact_format": 0})
        self.assertFalse(normalized["compact_format"])


# ============================================================
# Slot-level compact renderers
# ============================================================

class PersonaSlotCompactTests(unittest.TestCase):
    def test_default_renders_verbose_framing(self):
        slot = pa.persona_slot("Be concise.")
        self.assertIsNotNone(slot)
        self.assertIn("PERSONA.md", slot.content)
        self.assertIn("Be concise.", slot.content)

    def test_compact_strips_framing(self):
        slot = pa.persona_slot("Be concise.", compact=True)
        self.assertIsNotNone(slot)
        # Compact form: ## Persona header + body, no prose framing.
        self.assertIn("## Persona", slot.content)
        self.assertIn("Be concise.", slot.content)
        self.assertNotIn("PERSONA.md", slot.content)

    def test_compact_is_shorter_than_verbose(self):
        verbose = pa.persona_slot("Be concise.")
        compact = pa.persona_slot("Be concise.", compact=True)
        self.assertLess(len(compact.content), len(verbose.content))

    def test_compact_preserves_body_when_long(self):
        body = "Line one.\nLine two.\nLine three."
        slot = pa.persona_slot(body, compact=True)
        self.assertIn(body, slot.content)

    def test_compact_returns_none_for_blank_input(self):
        self.assertIsNone(pa.persona_slot("", compact=True))
        self.assertIsNone(pa.persona_slot("   ", compact=True))


class ProjectContextSlotCompactTests(unittest.TestCase):
    def test_default_renders_verbose_framing(self):
        slot = pa.project_context_slot("Always run tests.")
        self.assertIn("AGENTS.md", slot.content)

    def test_compact_strips_framing(self):
        slot = pa.project_context_slot("Always run tests.", compact=True)
        self.assertIn("## Project", slot.content)
        self.assertIn("Always run tests.", slot.content)
        self.assertNotIn("AGENTS.md", slot.content)

    def test_compact_is_shorter_than_verbose(self):
        verbose = pa.project_context_slot("Always run tests.")
        compact = pa.project_context_slot("Always run tests.", compact=True)
        self.assertLess(len(compact.content), len(verbose.content))


class MemoryBlockSlotsCompactTests(unittest.TestCase):
    def test_default_renders_full_header(self):
        blocks = {
            "current_goal": {
                "value": "ship phase 1",
                "priority": 2,
                "description": "what we're doing right now",
            },
        }
        slots = pa.memory_block_slots(blocks)
        self.assertEqual(len(slots), 1)
        self.assertIn("Memory block 'current_goal'", slots[0].content)
        self.assertIn("what we're doing right now", slots[0].content)
        self.assertIn("ship phase 1", slots[0].content)

    def test_compact_uses_short_header(self):
        blocks = {
            "current_goal": {
                "value": "ship phase 1",
                "priority": 2,
                "description": "what we're doing right now",
            },
        }
        slots = pa.memory_block_slots(blocks, compact=True)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].content, "## current_goal\nship phase 1")
        # The description gets dropped — that's framing, the label
        # already names the block.
        self.assertNotIn("what we're doing right now", slots[0].content)

    def test_compact_skips_blocks_with_blank_value(self):
        blocks = {
            "empty": {"value": "", "priority": 1},
            "kept": {"value": "real content", "priority": 1},
        }
        slots = pa.memory_block_slots(blocks, compact=True)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].name, "memory:kept")

    def test_compact_preserves_priority_and_name(self):
        blocks = {
            "persona": {"value": "be concise", "priority": 0},
            "working_memory": {"value": "scratch", "priority": 3},
        }
        slots = pa.memory_block_slots(blocks, compact=True)
        by_name = {s.name: s for s in slots}
        # Priority offset (+30) preserved → compact mode only changes
        # the rendered content, not the slot metadata.
        self.assertEqual(by_name["memory:persona"].priority, 30)
        self.assertEqual(by_name["memory:working_memory"].priority, 33)

    def test_compact_shorter_than_verbose_for_each_block(self):
        blocks = {
            "current_goal": {
                "value": "ship phase 1",
                "priority": 2,
                "description": "what we're doing right now",
            },
        }
        verbose = pa.memory_block_slots(blocks)
        compact = pa.memory_block_slots(blocks, compact=True)
        self.assertEqual(len(verbose), len(compact))
        self.assertLess(len(compact[0].content), len(verbose[0].content))


# ============================================================
# Mode slot (via _mode_system_message)
# ============================================================

class ModeSlotCompactTests(unittest.TestCase):
    def test_plan_mode_default_paragraph(self):
        from app.services.chat_service import _mode_system_message
        msg = _mode_system_message("plan")
        # The verbose form lists every refusal case.
        self.assertIn("plan", msg["content"].lower())
        self.assertIn("Read and analyse", msg["content"])

    def test_plan_mode_compact_one_liner(self):
        from app.services.chat_service import _mode_system_message
        msg = _mode_system_message("plan", compact=True)
        self.assertIn("plan", msg["content"].lower())
        # The full paragraph is gone.
        self.assertNotIn("Read and analyse only", msg["content"])
        # Compact is meaningfully shorter.
        verbose = _mode_system_message("plan")
        self.assertLess(len(msg["content"]), len(verbose["content"]))

    def test_build_mode_compact_one_liner(self):
        from app.services.chat_service import _mode_system_message
        msg = _mode_system_message("build", compact=True)
        self.assertIn("build", msg["content"].lower())
        self.assertIn("approval", msg["content"].lower())
        verbose = _mode_system_message("build")
        self.assertLess(len(msg["content"]), len(verbose["content"]))


# ============================================================
# End-to-end via build_runtime_assembly
# ============================================================

class CompactBuildRuntimeAssemblyTests(unittest.TestCase):
    """Pin a workspace with a real PERSONA.md + AGENTS.md, build
    the assembly with compact off then on, and assert the compact
    pass produces shorter system messages."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        with open(os.path.join(self.workspace, "PERSONA.md"), "w") as fh:
            fh.write("# Persona\nBe concise. Cite files.\n")
        with open(os.path.join(self.workspace, "AGENTS.md"), "w") as fh:
            fh.write("# Project Rules\n- Always run tests.\n- Keep diffs small.\n")
        self.sid = _fresh_session()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, *, compact):
        return {
            "workspace_path": self.workspace,
            "active_skills": [],
            "memory_blocks": {
                "current_goal": {
                    "value": "ship compact-prompt phase 1",
                    "priority": 2,
                    "description": "active task",
                },
            },
            "compact_format": bool(compact),
            "mode": "plan",
        }

    def test_compact_assembly_is_shorter_than_default(self):
        msgs_default, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=False),
            session_id=self.sid,
        )
        msgs_compact, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=True),
            session_id=self.sid,
        )
        # Sum bytes across system messages (the trailing user
        # message is identical in both).
        default_bytes = sum(
            len(m["content"]) for m in msgs_default if m["role"] == "system"
        )
        compact_bytes = sum(
            len(m["content"]) for m in msgs_compact if m["role"] == "system"
        )
        self.assertLess(compact_bytes, default_bytes)

    def test_compact_assembly_preserves_persona_body(self):
        msgs_compact, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=True),
            session_id=self.sid,
        )
        joined = "\n".join(m["content"] for m in msgs_compact)
        self.assertIn("Be concise.", joined)
        self.assertIn("Cite files.", joined)

    def test_compact_assembly_preserves_project_body(self):
        msgs_compact, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=True),
            session_id=self.sid,
        )
        joined = "\n".join(m["content"] for m in msgs_compact)
        self.assertIn("Always run tests.", joined)
        self.assertIn("Keep diffs small.", joined)

    def test_compact_assembly_preserves_memory_block_value(self):
        msgs_compact, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=True),
            session_id=self.sid,
        )
        joined = "\n".join(m["content"] for m in msgs_compact)
        self.assertIn("ship compact-prompt phase 1", joined)
        # The description is framing — dropped in compact mode.
        self.assertNotIn("active task", joined)

    def test_compact_assembly_strips_framing_strings(self):
        msgs_compact, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=True),
            session_id=self.sid,
        )
        joined = "\n".join(m["content"] for m in msgs_compact)
        # Verbose framing markers should NOT appear.
        self.assertNotIn(
            "Project persona (read from `PERSONA.md` at the workspace root)",
            joined,
        )
        self.assertNotIn(
            "Project guidance (read from `AGENTS.md` at the workspace root)",
            joined,
        )

    def test_default_assembly_keeps_framing_strings(self):
        msgs_default, _ = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=False),
            session_id=self.sid,
        )
        joined = "\n".join(m["content"] for m in msgs_default)
        # Default mode → framing IS present.
        self.assertIn("PERSONA.md", joined)
        self.assertIn("AGENTS.md", joined)

    def test_trace_slot_names_unchanged_in_compact_mode(self):
        # Compact mode only changes WHAT each slot renders, not WHICH
        # slots are present. The trace's slot names are identical.
        _, trace_default = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=False),
            session_id=self.sid,
        )
        _, trace_compact = build_runtime_assembly(
            {}, [], "explain the structure",
            session_context=self._ctx(compact=True),
            session_id=self.sid,
        )
        names_default = sorted(t["name"] for t in trace_default)
        names_compact = sorted(t["name"] for t in trace_compact)
        self.assertEqual(names_default, names_compact)


# ============================================================
# Dispatcher subcommand
# ============================================================

class PromptCompactDispatcherTests(unittest.TestCase):
    def test_no_arg_reports_current_state(self):
        sid = _fresh_session()
        result = dispatch_input("prompt compact", session_id=sid)
        self.assertEqual(result.get("action"), "prompt")
        self.assertIn("off", result.get("output", "").lower())
        self.assertEqual(
            result.get("result", {}).get("compact_format"),
            False,
        )

    def test_on_persists_flag(self):
        sid = _fresh_session()
        result = dispatch_input("prompt compact on", session_id=sid)
        self.assertEqual(result.get("action"), "prompt")
        self.assertEqual(
            result.get("result", {}).get("compact_format"),
            True,
        )
        self.assertTrue(pa.is_compact_enabled(sid))

    def test_off_persists_flag(self):
        sid = _fresh_session()
        pa.set_compact(sid, True)
        result = dispatch_input("prompt compact off", session_id=sid)
        self.assertEqual(
            result.get("result", {}).get("compact_format"),
            False,
        )
        self.assertFalse(pa.is_compact_enabled(sid))

    def test_unknown_value_returns_invalid_argument(self):
        sid = _fresh_session()
        result = dispatch_input("prompt compact maybe", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")
        # Flag unchanged.
        self.assertFalse(pa.is_compact_enabled(sid))

    def test_no_arg_reports_on_after_enabling(self):
        sid = _fresh_session()
        pa.set_compact(sid, True)
        result = dispatch_input("prompt compact", session_id=sid)
        self.assertIn("on", result.get("output", "").lower())
        self.assertEqual(
            result.get("result", {}).get("compact_format"),
            True,
        )

    def test_unknown_subcommand_message_lists_compact(self):
        # `prompt fooobar` falls through to the unknown-subcommand
        # path — its hint should mention `compact` so the help text
        # is discoverable.
        sid = _fresh_session()
        result = dispatch_input("prompt fooobar", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")
        self.assertIn("compact", result.get("output", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
