"""Tests for the layered prompt assembly (Phase 1 + Phase 2 + Phase 3).

Phase 1 is a pure refactor of ``_build_runtime_messages`` into typed
slot producers + a per-slot trace. Phase 2 adds per-slot truncation +
total-budget eviction. Phase 3 adds Hermes-inspired persona /
project-AGENTS slot producers fed by ``project_context_service``.
These tests cover:

* The slot dataclass + assembly contracts in isolation.
* All slot producer functions and their priority assignments.
* A byte-identical regression check: feed the same inputs into
  ``build_runtime_assembly`` and confirm ``messages`` equals the
  output of the legacy code path.
* The trace shape returned by ``build_runtime_assembly`` and surfaced
  on the ``handle_chat`` result dict.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import prompt_assembly as pa
from app.services.chat_service import (
    CONTROL_SYSTEM_MESSAGE,
    _build_runtime_messages,
    build_runtime_assembly,
    handle_chat,
)


# ============================================================
# PromptSlot / PromptAssembly contracts
# ============================================================

class PromptSlotTests(unittest.TestCase):
    def test_default_priority_and_flags(self):
        slot = pa.PromptSlot(name="x", content="hi")
        self.assertEqual(slot.priority, pa.DEFAULT_PRIORITY)
        self.assertEqual(slot.char_budget, 0)
        self.assertFalse(slot.truncated)
        self.assertFalse(slot.evicted)
        self.assertEqual(slot.evicted_reason, "")

    def test_is_empty_treats_whitespace_as_empty(self):
        self.assertTrue(pa.PromptSlot(name="x", content="").is_empty())
        self.assertTrue(pa.PromptSlot(name="x", content="   \n\t").is_empty())
        self.assertFalse(pa.PromptSlot(name="x", content="hi").is_empty())

    def test_char_count_returns_len(self):
        self.assertEqual(pa.PromptSlot(name="x", content="abcd").char_count(), 4)
        self.assertEqual(pa.PromptSlot(name="x", content="").char_count(), 0)


class PromptAssemblyTests(unittest.TestCase):
    def test_assemble_emits_each_non_empty_slot_in_priority_order(self):
        assembly = pa.PromptAssembly()
        assembly.add(pa.PromptSlot(name="b", content="2", priority=20))
        assembly.add(pa.PromptSlot(name="a", content="1", priority=10))
        assembly.add(pa.PromptSlot(name="c", content="3", priority=30))
        messages, trace = assembly.assemble()
        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "1"},
                {"role": "system", "content": "2"},
                {"role": "system", "content": "3"},
            ],
        )
        self.assertEqual([t["name"] for t in trace], ["a", "b", "c"])

    def test_assemble_skips_empty_and_evicted_slots_but_traces_them(self):
        assembly = pa.PromptAssembly()
        assembly.add(pa.PromptSlot(name="empty", content="", priority=10))
        assembly.add(pa.PromptSlot(
            name="dropped", content="hi", priority=20, evicted=True,
            evicted_reason="over budget",
        ))
        assembly.add(pa.PromptSlot(name="kept", content="ok", priority=30))
        messages, trace = assembly.assemble()
        # Only the kept slot makes it to messages
        self.assertEqual(messages, [{"role": "system", "content": "ok"}])
        # All three appear in the trace
        names = [t["name"] for t in trace]
        self.assertEqual(names, ["empty", "dropped", "kept"])
        dropped = next(t for t in trace if t["name"] == "dropped")
        self.assertTrue(dropped["evicted"])
        self.assertEqual(dropped["evicted_reason"], "over budget")

    def test_assemble_breaks_priority_ties_by_insertion_order(self):
        assembly = pa.PromptAssembly()
        assembly.add(pa.PromptSlot(name="first", content="A", priority=10))
        assembly.add(pa.PromptSlot(name="second", content="B", priority=10))
        messages, _ = assembly.assemble()
        self.assertEqual([m["content"] for m in messages], ["A", "B"])

    def test_add_silently_ignores_none(self):
        assembly = pa.PromptAssembly()
        assembly.add(None)
        messages, trace = assembly.assemble()
        self.assertEqual(messages, [])
        self.assertEqual(trace, [])

    def test_add_all_handles_none_iterable(self):
        assembly = pa.PromptAssembly()
        assembly.add_all(None)  # should not raise
        assembly.add_all([])
        assembly.add_all([
            pa.PromptSlot(name="x", content="X", priority=1),
            None,  # interleaved None still ok
            pa.PromptSlot(name="y", content="Y", priority=2),
        ])
        messages, _ = assembly.assemble()
        self.assertEqual([m["content"] for m in messages], ["X", "Y"])

    def test_trace_entry_shape(self):
        assembly = pa.PromptAssembly()
        assembly.add(pa.PromptSlot(
            name="x", content="hello", priority=15, char_budget=100,
        ))
        _, trace = assembly.assemble()
        self.assertEqual(len(trace), 1)
        entry = trace[0]
        self.assertEqual(set(entry.keys()), {
            "name", "chars", "priority", "char_budget",
            "truncated", "evicted", "evicted_reason",
        })
        self.assertEqual(entry["name"], "x")
        self.assertEqual(entry["chars"], 5)
        self.assertEqual(entry["priority"], 15)
        self.assertEqual(entry["char_budget"], 100)
        self.assertFalse(entry["truncated"])
        self.assertFalse(entry["evicted"])


# ============================================================
# Slot producers
# ============================================================

class SlotProducerTests(unittest.TestCase):
    def test_control_slot_priority_zero(self):
        slot = pa.control_slot({"role": "system", "content": "be careful"})
        self.assertEqual(slot.name, "control")
        self.assertEqual(slot.priority, 0)
        self.assertEqual(slot.content, "be careful")

    def test_mode_slot_priority_zero(self):
        slot = pa.mode_slot({"role": "system", "content": "Plan mode"})
        self.assertEqual(slot.name, "mode")
        self.assertEqual(slot.priority, 0)

    def test_skills_slot_returns_none_for_falsy_input(self):
        self.assertIsNone(pa.skills_slot(None))
        self.assertIsNone(pa.skills_slot({}))

    def test_skills_slot_priority_10(self):
        slot = pa.skills_slot({"role": "system", "content": "skills..."})
        self.assertIsNotNone(slot)
        self.assertEqual(slot.name, "skills")
        self.assertEqual(slot.priority, 10)

    def test_workspace_slot_priority_15(self):
        slot = pa.workspace_slot({"role": "system", "content": "ws..."})
        self.assertEqual(slot.priority, 15)
        self.assertIsNone(pa.workspace_slot(None))

    def test_inspection_slot_priority_20(self):
        slot = pa.inspection_slot({"role": "system", "content": "read..."})
        self.assertEqual(slot.priority, 20)
        self.assertIsNone(pa.inspection_slot(None))

    def test_mentions_slot_priority_25(self):
        slot = pa.mentions_slot("file.py contents...")
        self.assertEqual(slot.priority, 25)
        self.assertEqual(slot.name, "mentions")
        self.assertEqual(slot.content, "file.py contents...")
        self.assertIsNone(pa.mentions_slot(None))
        self.assertIsNone(pa.mentions_slot(""))

    def test_summary_slot_priority_80(self):
        slot = pa.summary_slot({"role": "system", "content": "Facts: ..."})
        self.assertEqual(slot.priority, 80)
        self.assertIsNone(pa.summary_slot(None))


class MemoryBlockSlotsTests(unittest.TestCase):
    def test_empty_dict_returns_empty_list(self):
        self.assertEqual(pa.memory_block_slots({}), [])
        self.assertEqual(pa.memory_block_slots(None), [])

    def test_skips_blocks_with_blank_value(self):
        blocks = {
            "persona": {"value": "  ", "priority": 0, "description": "x"},
            "current_goal": {"value": "ship phase 1", "priority": 2},
        }
        slots = pa.memory_block_slots(blocks)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].name, "memory:current_goal")

    def test_priority_offset_by_30(self):
        blocks = {
            "persona": {"value": "be concise", "priority": 0},
            "working_memory": {"value": "scratch", "priority": 3},
        }
        slots = pa.memory_block_slots(blocks)
        # Sort by name → check priority assignment
        by_name = {s.name: s for s in slots}
        self.assertEqual(by_name["memory:persona"].priority, 30)
        self.assertEqual(by_name["memory:working_memory"].priority, 33)

    def test_emits_blocks_in_priority_then_label_order(self):
        blocks = {
            "z": {"value": "Z", "priority": 5},
            "a": {"value": "A", "priority": 5},
            "first": {"value": "F", "priority": 1},
        }
        slots = pa.memory_block_slots(blocks)
        self.assertEqual(
            [s.name for s in slots],
            ["memory:first", "memory:a", "memory:z"],
        )

    def test_header_includes_label_and_description(self):
        blocks = {
            "persona": {
                "value": "be concise",
                "priority": 0,
                "description": "How the assistant should present itself",
            },
        }
        slot = pa.memory_block_slots(blocks)[0]
        self.assertIn("Memory block 'persona'", slot.content)
        self.assertIn("How the assistant should present itself", slot.content)
        self.assertIn("be concise", slot.content)

    def test_header_omits_colon_when_no_description(self):
        blocks = {"x": {"value": "Y", "priority": 0}}
        slot = pa.memory_block_slots(blocks)[0]
        # No description → header is just the label
        first_line = slot.content.splitlines()[0]
        self.assertEqual(first_line, "Memory block 'x'")

    def test_char_budget_carried_from_char_limit(self):
        blocks = {"x": {"value": "Y", "priority": 0, "char_limit": 1234}}
        slot = pa.memory_block_slots(blocks)[0]
        self.assertEqual(slot.char_budget, 1234)


# ============================================================
# build_runtime_assembly: byte-identical regression
# ============================================================

class BuildRuntimeAssemblyTests(unittest.TestCase):
    """build_runtime_assembly must return the same `messages` list
    that the legacy ``_build_runtime_messages`` produces. The trace
    is a new addition; messages must not drift."""

    def _common_inputs(self):
        return {
            "summary": {
                "facts": ["Codename: Nebula Forge"],
                "preferences": [],
                "open_tasks": [],
            },
            "history": [
                {"role": "user", "content": "First turn"},
                {"role": "assistant", "content": "Got it."},
            ],
            "current": "What's next?",
        }

    def test_messages_match_legacy_for_minimal_context(self):
        i = self._common_inputs()
        legacy = _build_runtime_messages(i["summary"], i["history"], i["current"])
        new, trace = build_runtime_assembly(i["summary"], i["history"], i["current"])
        self.assertEqual(new, legacy)
        # control + mode + summary + 2 history + user = 6
        self.assertEqual(len(new), 6)
        self.assertGreaterEqual(len(trace), 3)  # control, mode, summary

    def test_messages_match_legacy_with_full_context(self):
        i = self._common_inputs()
        ctx = {
            "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
            "active_skills": ["builder"],
            "mode": "build",
            "memory_blocks": {
                "persona": {"value": "Concise.", "priority": 0, "description": "How to act"},
                "working_memory": {"value": "Open: ship phase 1", "priority": 3},
            },
        }
        legacy = _build_runtime_messages(
            i["summary"], i["history"], i["current"],
            session_context=ctx,
            mentions_context_block="mentions: file.py contents",
        )
        new, trace = build_runtime_assembly(
            i["summary"], i["history"], i["current"],
            session_context=ctx,
            mentions_context_block="mentions: file.py contents",
        )
        self.assertEqual(new, legacy)
        # Trace should mention every slot that emitted
        names = [t["name"] for t in trace]
        self.assertIn("control", names)
        self.assertIn("mode", names)
        self.assertIn("skills", names)
        self.assertIn("workspace", names)
        self.assertIn("mentions", names)
        self.assertIn("memory:persona", names)
        self.assertIn("memory:working_memory", names)
        self.assertIn("summary", names)

    def test_messages_match_legacy_with_inspection_result(self):
        i = self._common_inputs()
        approved = {
            "ok": True,
            "kind": "read",
            "files": [
                {"path": "x.py", "content": "print('hi')", "size": 11, "truncated": False},
            ],
        }
        legacy = _build_runtime_messages(
            i["summary"], i["history"], i["current"],
            session_context={"workspace_path": "/ws", "active_skills": []},
            approved_workspace_result=approved,
        )
        new, _ = build_runtime_assembly(
            i["summary"], i["history"], i["current"],
            session_context={"workspace_path": "/ws", "active_skills": []},
            approved_workspace_result=approved,
        )
        self.assertEqual(new, legacy)

    def test_trace_priorities_are_monotonic_in_message_order(self):
        i = self._common_inputs()
        ctx = {
            "active_skills": ["builder"],
            "memory_blocks": {
                "persona": {"value": "Concise.", "priority": 0},
            },
        }
        _, trace = build_runtime_assembly(
            i["summary"], i["history"], i["current"],
            session_context=ctx,
            mentions_context_block="m",
        )
        priorities = [t["priority"] for t in trace]
        self.assertEqual(priorities, sorted(priorities))


# ============================================================
# handle_chat: trace surfaced on the result
# ============================================================

class HandleChatPromptAssemblyTraceTests(unittest.TestCase):
    """The result dict that ``handle_chat`` returns includes a
    ``prompt_assembly`` list — one entry per registered slot. We
    don't run a real model; mock the inference and check the trace
    plumbed through."""

    def test_result_includes_prompt_assembly_trace(self):
        with patch("app.services.chat_service.chat_providers.dispatch_chat") as dispatch, \
             patch("app.services.chat_service.model_registry.resolve_model_for_role") as resolve, \
             patch("app.services.chat_service.router.route_message") as route, \
             patch("app.services.chat_service.compaction_service.maybe_compact_session") as compact:
            dispatch.return_value = {"ok": True, "content": "hi", "error": None}
            resolve.return_value = {
                "resolved_role": "reasoning",
                "model": "llama3.1:8b",
                "fallback_used": False,
            }
            route.return_value = {
                "lane": "reasoning",
                "confidence": 0.9,
                "scores": {},
                "primary_signal": None,
                "context_used": False,
                "context_reason": "",
            }
            compact.return_value = {"performed": False}

            result = handle_chat("hello there", session_id="prompt_assembly_test")
            self.assertTrue(result["ok"])
            trace = result.get("prompt_assembly")
            self.assertIsInstance(trace, list)
            self.assertGreaterEqual(len(trace), 2)
            names = [t["name"] for t in trace]
            self.assertIn("control", names)
            self.assertIn("mode", names)
            for entry in trace:
                self.assertIn("chars", entry)
                self.assertIn("priority", entry)


# ============================================================
# Phase 2: per-slot truncation + total-budget eviction
# ============================================================

class TruncationTests(unittest.TestCase):
    def test_slot_under_budget_is_unchanged(self):
        slot = pa.PromptSlot(name="x", content="hi there", char_budget=100)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        messages, trace = assembly.assemble()
        self.assertEqual(messages[0]["content"], "hi there")
        self.assertFalse(trace[0]["truncated"])
        # Original slot unchanged (we deep-copy in assemble).
        self.assertFalse(slot.truncated)

    def test_slot_over_budget_is_truncated_with_marker(self):
        content = "x" * 1000
        slot = pa.PromptSlot(name="x", content=content, char_budget=200)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        messages, trace = assembly.assemble()
        out = messages[0]["content"]
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("[truncated]"))
        self.assertTrue(trace[0]["truncated"])
        # Original slot must NOT be mutated.
        self.assertEqual(len(slot.content), 1000)
        self.assertFalse(slot.truncated)

    def test_zero_budget_means_unbounded(self):
        slot = pa.PromptSlot(name="x", content="x" * 5000, char_budget=0)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        messages, trace = assembly.assemble()
        self.assertEqual(len(messages[0]["content"]), 5000)
        self.assertFalse(trace[0]["truncated"])

    def test_tiny_budget_below_marker_length_still_truncates(self):
        # Budget smaller than the truncation marker — slot still
        # gets cut, content+marker can't exceed budget.
        slot = pa.PromptSlot(name="x", content="abcdefghij", char_budget=3)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        messages, trace = assembly.assemble()
        self.assertLessEqual(len(messages[0]["content"]), 3)
        self.assertTrue(trace[0]["truncated"])


class EvictionTests(unittest.TestCase):
    def test_no_eviction_when_total_zero_unbounded(self):
        a = pa.PromptSlot(name="a", content="x" * 1000, priority=10)
        b = pa.PromptSlot(name="b", content="y" * 1000, priority=80)
        assembly = pa.PromptAssembly(total_budget=0)
        assembly.add(a)
        assembly.add(b)
        messages, trace = assembly.assemble()
        self.assertEqual(len(messages), 2)
        for entry in trace:
            self.assertFalse(entry["evicted"])

    def test_no_eviction_when_total_under_budget(self):
        a = pa.PromptSlot(name="a", content="x" * 100, priority=10)
        b = pa.PromptSlot(name="b", content="y" * 100, priority=80)
        assembly = pa.PromptAssembly(total_budget=1000)
        assembly.add(a)
        assembly.add(b)
        messages, trace = assembly.assemble()
        self.assertEqual(len(messages), 2)
        for entry in trace:
            self.assertFalse(entry["evicted"])

    def test_eviction_drops_highest_priority_number_first(self):
        # Total content = 3000; budget = 1500. The priority-80 slot
        # should fall first; if still over, priority-50, etc.
        slots = [
            pa.PromptSlot(name="control", content="x" * 500, priority=0),
            pa.PromptSlot(name="mid", content="x" * 500, priority=50),
            pa.PromptSlot(name="late", content="x" * 1000, priority=80),
            pa.PromptSlot(name="latest", content="x" * 1000, priority=80),
        ]
        assembly = pa.PromptAssembly(total_budget=1500)
        for s in slots:
            assembly.add(s)
        _, trace = assembly.assemble()
        by_name = {t["name"]: t for t in trace}
        # Both priority-80 slots evicted (drops 2000 chars → fits 1000 in 1500).
        self.assertTrue(by_name["late"]["evicted"])
        self.assertTrue(by_name["latest"]["evicted"])
        self.assertFalse(by_name["mid"]["evicted"])
        self.assertFalse(by_name["control"]["evicted"])
        # Reason is recorded.
        self.assertEqual(by_name["late"]["evicted_reason"], pa.EVICT_REASON_OVER_BUDGET)

    def test_pinned_priority_zero_slot_never_evicted(self):
        # Pinned slot alone exceeds the budget — eviction can't fix
        # this; the pinned slot must remain.
        pinned = pa.PromptSlot(name="control", content="x" * 5000, priority=0)
        evictable = pa.PromptSlot(name="extra", content="x" * 100, priority=80)
        assembly = pa.PromptAssembly(total_budget=1000)
        assembly.add(pinned)
        assembly.add(evictable)
        messages, trace = assembly.assemble()
        names = [m["content"][:20] for m in messages]
        # Control survives; the evictable slot may or may not — both
        # being over budget, eviction will drop the priority-80 first
        # but pinned stays even when alone over budget.
        self.assertEqual(len(names), 1)
        by_name = {t["name"]: t for t in trace}
        self.assertFalse(by_name["control"]["evicted"])
        self.assertTrue(by_name["extra"]["evicted"])

    def test_truncation_runs_before_eviction(self):
        # Slot has its own char_budget that brings it under the
        # total_budget; eviction shouldn't fire after truncation.
        slot = pa.PromptSlot(
            name="big", content="x" * 5000, char_budget=500, priority=80,
        )
        assembly = pa.PromptAssembly(total_budget=1000)
        assembly.add(slot)
        _, trace = assembly.assemble()
        self.assertTrue(trace[0]["truncated"])
        self.assertFalse(trace[0]["evicted"])


class ApplyBudgetsTests(unittest.TestCase):
    def test_apply_budgets_overrides_zero_budget_slots(self):
        slot = pa.PromptSlot(name="skills", content="hi", char_budget=0)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        assembly.apply_budgets({"skills": 4000})
        # Internal slot mutated (apply_budgets predates the assemble copy).
        self.assertEqual(assembly._slots[0].char_budget, 4000)

    def test_apply_budgets_does_not_overwrite_existing_with_zero(self):
        # Memory blocks already carry their char_limit as char_budget;
        # passing a zero override mustn't weaken that.
        slot = pa.PromptSlot(name="memory:persona", content="hi", char_budget=1024)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        assembly.apply_budgets({"memory:persona": 0})
        self.assertEqual(assembly._slots[0].char_budget, 1024)

    def test_apply_budgets_positive_override_wins(self):
        slot = pa.PromptSlot(name="memory:persona", content="hi", char_budget=1024)
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        assembly.apply_budgets({"memory:persona": 500})
        self.assertEqual(assembly._slots[0].char_budget, 500)

    def test_apply_budgets_silently_ignores_unknown_slot_names(self):
        slot = pa.PromptSlot(name="skills", content="hi")
        assembly = pa.PromptAssembly()
        assembly.add(slot)
        # Should not raise — unknown name has no effect.
        assembly.apply_budgets({"nonexistent": 999})
        self.assertEqual(assembly._slots[0].char_budget, 0)


class OverBudgetWarningTests(unittest.TestCase):
    """When the pinned slots alone exceed total_budget, the trace
    grows a synthetic ``_diagnostics`` entry with
    ``over_budget=True``. The dispatcher footer + chat result both
    surface this as the ``prompt_budget_exceeded`` warning."""

    def test_no_diagnostics_when_under_budget(self):
        from app.memory import local_memory as lm
        sid = "no_diag_under_budget"
        lm.set_prompt_total_budget(session_id=sid, total_budget=10000)
        ctx = lm.get_session_context(session_id=sid)
        _, trace = build_runtime_assembly({}, [], "hi", session_context=ctx)
        names = [t["name"] for t in trace]
        self.assertNotIn("_diagnostics", names)

    def test_diagnostics_appended_when_pinned_exceeds_total(self):
        # Set a stupidly low total budget (50 chars) — control + mode
        # alone are 600+ chars and they're pinned, so eviction can't
        # save us. The diagnostics entry should appear.
        from app.memory import local_memory as lm
        sid = "diag_over_budget"
        lm.set_prompt_total_budget(session_id=sid, total_budget=50)
        ctx = lm.get_session_context(session_id=sid)
        _, trace = build_runtime_assembly({}, [], "hi", session_context=ctx)
        diag = [t for t in trace if t["name"] == "_diagnostics"]
        self.assertEqual(len(diag), 1)
        self.assertTrue(diag[0]["over_budget"])
        self.assertEqual(diag[0]["total_budget"], 50)
        self.assertTrue(pa.trace_over_budget(trace))

    def test_footer_includes_over_budget_marker(self):
        trace = [
            {"name": "control", "chars": 5000, "priority": 0,
             "char_budget": 0, "truncated": False, "evicted": False,
             "evicted_reason": ""},
            {"name": "_diagnostics", "chars": 5000, "priority": 999,
             "char_budget": 1000, "truncated": False, "evicted": False,
             "evicted_reason": "", "total_budget": 1000, "over_budget": True},
        ]
        footer = pa.render_trace_footer(trace)
        self.assertIn("OVER_BUDGET", footer)


class TraceFooterTests(unittest.TestCase):
    def test_no_footer_when_no_pressure(self):
        trace = [
            {"name": "control", "chars": 100, "priority": 0,
             "char_budget": 0, "truncated": False, "evicted": False,
             "evicted_reason": ""},
        ]
        self.assertFalse(pa.trace_has_pressure(trace))
        self.assertEqual(pa.render_trace_footer(trace), "")

    def test_footer_marks_truncated(self):
        trace = [
            {"name": "control", "chars": 100, "priority": 0,
             "char_budget": 0, "truncated": False, "evicted": False,
             "evicted_reason": ""},
            {"name": "mentions", "chars": 500, "priority": 25,
             "char_budget": 500, "truncated": True, "evicted": False,
             "evicted_reason": ""},
        ]
        self.assertTrue(pa.trace_has_pressure(trace))
        footer = pa.render_trace_footer(trace)
        self.assertIn("[prompt:", footer)
        self.assertIn("mentions/500~", footer)
        self.assertNotIn("control", footer)

    def test_footer_marks_evicted(self):
        trace = [
            {"name": "summary", "chars": 8000, "priority": 80,
             "char_budget": 0, "truncated": False, "evicted": True,
             "evicted_reason": "over_budget"},
        ]
        footer = pa.render_trace_footer(trace)
        self.assertIn("summary/EVICTED", footer)


# ============================================================
# Session-context overrides + storage roundtrip
# ============================================================

class StorageRoundtripTests(unittest.TestCase):
    def test_set_prompt_budget_persists(self):
        from app.memory import local_memory as lm
        sid = "prompt_budget_storage_test"
        lm.set_prompt_budget(session_id=sid, slot="summary", char_budget=1234)
        self.assertEqual(
            lm.get_prompt_budgets(session_id=sid),
            {"summary": 1234},
        )

    def test_set_prompt_budget_zero_clears_override(self):
        from app.memory import local_memory as lm
        sid = "prompt_budget_clear_test"
        lm.set_prompt_budget(session_id=sid, slot="summary", char_budget=1234)
        lm.set_prompt_budget(session_id=sid, slot="summary", char_budget=0)
        self.assertEqual(lm.get_prompt_budgets(session_id=sid), {})

    def test_set_prompt_total_budget_roundtrip(self):
        from app.memory import local_memory as lm
        sid = "prompt_total_storage_test"
        lm.set_prompt_total_budget(session_id=sid, total_budget=12345)
        self.assertEqual(lm.get_prompt_total_budget(session_id=sid), 12345)
        lm.set_prompt_total_budget(session_id=sid, total_budget=0)
        self.assertIsNone(lm.get_prompt_total_budget(session_id=sid))

    def test_invalid_slot_name_rejected(self):
        from app.memory import local_memory as lm
        with self.assertRaises(ValueError):
            lm.set_prompt_budget(session_id="x", slot="Bad NAME!", char_budget=100)

    def test_session_overrides_flow_through_to_assembly(self):
        from app.memory import local_memory as lm
        sid = "prompt_assembly_override_flow"
        lm.set_prompt_budget(session_id=sid, slot="summary", char_budget=10)
        lm.set_prompt_total_budget(session_id=sid, total_budget=2000)
        ctx = lm.get_session_context(session_id=sid)
        # Build with a long summary; override should truncate it.
        long_summary = {
            "facts": ["x" * 1000, "y" * 1000],
            "preferences": [],
            "open_tasks": [],
        }
        _, trace = build_runtime_assembly(
            long_summary, [], "hi", session_context=ctx,
        )
        by_name = {t["name"]: t for t in trace}
        self.assertIn("summary", by_name)
        self.assertTrue(by_name["summary"]["truncated"])
        self.assertEqual(by_name["summary"]["char_budget"], 10)


# ============================================================
# Dispatcher commands
# ============================================================

class PromptDispatcherTests(unittest.TestCase):
    def test_prompt_limits_lists_defaults_and_overrides(self):
        from app.services.command_dispatcher import dispatch_input
        from app.memory import local_memory as lm
        sid = "dispatch_prompt_limits"
        lm.set_prompt_budget(session_id=sid, slot="summary", char_budget=999)
        result = dispatch_input("prompt limits", session_id=sid)
        output = result.get("output", "")
        self.assertIn("Total budget:", output)
        self.assertIn("summary", output)
        self.assertIn("override=999", output)

    def test_prompt_set_budget_then_show(self):
        from app.services.command_dispatcher import dispatch_input
        sid = "dispatch_prompt_set"
        r = dispatch_input("prompt set-budget mentions 7000", session_id=sid)
        self.assertNotIn("reason", r)
        self.assertIn("mentions", r["output"])
        self.assertIn("7000", r["output"])

        r = dispatch_input("prompt show", session_id=sid)
        self.assertIn("Prompt assembly", r["output"])
        # Always at least the control + mode slots.
        self.assertIn("control", r["output"])

    def test_prompt_set_budget_invalid_slot_name_returns_error(self):
        from app.services.command_dispatcher import dispatch_input
        r = dispatch_input(
            "prompt set-budget Bad! 100",
            session_id="dispatch_prompt_invalid",
        )
        self.assertIn("reason", r)

    def test_prompt_clear_budgets_drops_overrides(self):
        from app.services.command_dispatcher import dispatch_input
        from app.memory import local_memory as lm
        sid = "dispatch_prompt_clear"
        lm.set_prompt_budget(session_id=sid, slot="summary", char_budget=1)
        r = dispatch_input("prompt clear-budgets", session_id=sid)
        self.assertNotIn("reason", r)
        self.assertEqual(lm.get_prompt_budgets(session_id=sid), {})

    def test_prompt_set_total_then_revert(self):
        from app.services.command_dispatcher import dispatch_input
        from app.memory import local_memory as lm
        sid = "dispatch_prompt_total"
        dispatch_input("prompt set-total 5000", session_id=sid)
        self.assertEqual(lm.get_prompt_total_budget(session_id=sid), 5000)
        dispatch_input("prompt set-total 0", session_id=sid)
        self.assertIsNone(lm.get_prompt_total_budget(session_id=sid))

    def test_prompt_unknown_subcommand(self):
        from app.services.command_dispatcher import dispatch_input
        r = dispatch_input("prompt nonsense", session_id="dispatch_prompt_unknown")
        self.assertIn("reason", r)


# ============================================================
# Phase 3: persona_slot + project_context_slot
# ============================================================

class PersonaSlotProducerTests(unittest.TestCase):
    def test_returns_none_for_falsy_content(self):
        self.assertIsNone(pa.persona_slot(None))
        self.assertIsNone(pa.persona_slot(""))
        self.assertIsNone(pa.persona_slot("   \n  \t  "))

    def test_priority_5(self):
        slot = pa.persona_slot("Be concise.")
        self.assertIsNotNone(slot)
        self.assertEqual(slot.name, "persona")
        self.assertEqual(slot.priority, 5)
        self.assertIn("Be concise", slot.content)
        self.assertIn("PERSONA.md", slot.content)


class ProjectContextSlotProducerTests(unittest.TestCase):
    def test_returns_none_for_falsy_content(self):
        self.assertIsNone(pa.project_context_slot(None))
        self.assertIsNone(pa.project_context_slot(""))

    def test_priority_7_after_persona(self):
        slot = pa.project_context_slot("Project: nebula")
        self.assertIsNotNone(slot)
        self.assertEqual(slot.name, "project_context")
        self.assertEqual(slot.priority, 7)
        # Sits AFTER persona (5) and BEFORE skills (10).
        self.assertGreater(slot.priority, pa.persona_slot("x").priority)
        self.assertLess(slot.priority, pa.skills_slot({"content": "x"}).priority)
        self.assertIn("nebula", slot.content)
        self.assertIn("AGENTS.md", slot.content)


class ProjectContextEndToEndTests(unittest.TestCase):
    """Pin a workspace with PERSONA.md / AGENTS.md and confirm the
    layered assembly picks both up and orders them correctly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        with open(os.path.join(self.workspace, "PERSONA.md"), "w", encoding="utf-8") as fh:
            fh.write("Voice: succinct. Quote code.")
        with open(os.path.join(self.workspace, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write("Project: NebulaForge.\nUse TOML over YAML.")

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_slots_present_in_trace(self):
        ctx = {"workspace_path": self.workspace, "active_skills": []}
        _, trace = build_runtime_assembly({}, [], "hello", session_context=ctx)
        names = [t["name"] for t in trace]
        self.assertIn("persona", names)
        self.assertIn("project_context", names)

    def test_slot_order_persona_before_project_before_skills(self):
        ctx = {
            "workspace_path": self.workspace,
            "active_skills": ["builder"],
        }
        messages, trace = build_runtime_assembly(
            {}, [], "hello", session_context=ctx,
        )
        # In the trace, persona (5) < project_context (7) < skills (10).
        priorities = {t["name"]: t["priority"] for t in trace}
        self.assertLess(priorities["persona"], priorities["project_context"])
        self.assertLess(priorities["project_context"], priorities["skills"])
        # And the messages reflect the same order.
        message_contents = "\n".join(m["content"] for m in messages if m["role"] == "system")
        persona_idx = message_contents.find("Voice: succinct")
        project_idx = message_contents.find("NebulaForge")
        skills_idx = message_contents.find("Active operating skills")
        self.assertGreater(project_idx, persona_idx)
        self.assertGreater(skills_idx, project_idx)

    def test_missing_files_contribute_nothing(self):
        # Empty workspace dir → no persona, no project_context.
        with tempfile.TemporaryDirectory() as empty:
            ctx = {"workspace_path": empty, "active_skills": []}
            _, trace = build_runtime_assembly(
                {}, [], "hi", session_context=ctx,
            )
            names = [t["name"] for t in trace]
            self.assertNotIn("persona", names)
            self.assertNotIn("project_context", names)

    def test_no_workspace_skips_both_slots(self):
        ctx = {"workspace_path": None, "active_skills": []}
        _, trace = build_runtime_assembly(
            {}, [], "hi", session_context=ctx,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("persona", names)
        self.assertNotIn("project_context", names)

    def test_persona_truncates_under_session_override(self):
        from app.memory import local_memory as lm
        sid = "phase3_persona_truncate"
        lm.set_workspace(session_id=sid, workspace_path=self.workspace)
        lm.set_prompt_budget(session_id=sid, slot="persona", char_budget=20)
        ctx = lm.get_session_context(session_id=sid)
        _, trace = build_runtime_assembly({}, [], "hi", session_context=ctx)
        persona_entries = [t for t in trace if t["name"] == "persona"]
        self.assertEqual(len(persona_entries), 1)
        self.assertTrue(persona_entries[0]["truncated"])

    def test_default_slot_budget_for_persona_is_8000(self):
        self.assertEqual(pa.SLOT_BUDGET_DEFAULTS["persona"], 8_000)
        self.assertEqual(pa.SLOT_BUDGET_DEFAULTS["project_context"], 12_000)


# ============================================================
# Phase 2b of recall-memory.md: retrieved_docs slot
# ============================================================

class RetrievedDocsSlotProducerTests(unittest.TestCase):
    """``retrieved_docs_slot`` is conservative — returns None when
    any of (workspace_path, session_id, query, cached index, hits)
    is missing. End-to-end coverage in
    ``RetrievedDocsEndToEndTests`` below."""

    def test_returns_none_for_missing_workspace(self):
        self.assertIsNone(pa.retrieved_docs_slot(
            "how does the auth flow work",
            session_id="x", workspace_path=None,
        ))

    def test_returns_none_for_missing_session_id(self):
        self.assertIsNone(pa.retrieved_docs_slot(
            "how does the auth flow work",
            session_id=None, workspace_path="/tmp/ws",
        ))

    def test_returns_none_for_short_query(self):
        # Below _RETRIEVED_DOCS_MIN_QUERY_CHARS (8).
        for tiny in ("", "  ", "yes", "ok", "?", "abc"):
            self.assertIsNone(
                pa.retrieved_docs_slot(
                    tiny, session_id="x", workspace_path="/tmp/ws",
                ),
                f"slot should be None for query {tiny!r}",
            )

    def test_returns_none_when_no_cached_index(self):
        # Even with all other inputs present, no index → None.
        from app.services import retrieval_index as ri
        ri.clear("rds_no_index")
        self.assertIsNone(pa.retrieved_docs_slot(
            "how does the auth flow work",
            session_id="rds_no_index",
            workspace_path="/tmp/ws_does_not_exist",
        ))


class RetrievedDocsEndToEndTests(unittest.TestCase):
    """Build a real BM25 index over a temp workspace, then assert
    the slot fires correctly through ``build_runtime_assembly``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        os.makedirs(os.path.join(self.workspace, "src"))
        with open(os.path.join(self.workspace, "src", "auth.py"), "w") as fh:
            fh.write("def login(user, password):\n    validate_token(password)")
        with open(os.path.join(self.workspace, "README.md"), "w") as fh:
            fh.write("# Auth Service\nLogin and token validation")
        # Unique session id so we don't share the cache with other tests.
        from uuid import uuid4
        self.sid = f"rds_e2e_{uuid4().hex[:8]}"

    def tearDown(self):
        self.tmp.cleanup()
        from app.services import retrieval_index as ri
        ri.clear(self.sid)

    def _build_index(self):
        from app.services import retrieval_index as ri
        ri.rebuild(self.sid, workspace_path=self.workspace)

    def test_slot_fires_with_citations_after_index_built(self):
        self._build_index()
        ctx = {"workspace_path": self.workspace, "active_skills": []}
        messages, trace = build_runtime_assembly(
            {}, [], "how does login token validation work",
            session_context=ctx,
            session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertIn("retrieved_docs", names)
        # The slot's content carries the citation marker.
        retrieval_msg = next(
            m for m in messages if "[file:" in (m.get("content") or "")
        )
        self.assertIn("BM25 matches", retrieval_msg["content"])
        self.assertIn("[file:README.md]", retrieval_msg["content"])

    def test_slot_skipped_when_no_cached_index(self):
        # Without ``rebuild``, no slot.
        ctx = {"workspace_path": self.workspace, "active_skills": []}
        _, trace = build_runtime_assembly(
            {}, [], "how does login token validation work",
            session_context=ctx,
            session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("retrieved_docs", names)

    def test_slot_skipped_for_short_message(self):
        self._build_index()
        ctx = {"workspace_path": self.workspace, "active_skills": []}
        _, trace = build_runtime_assembly(
            {}, [], "ok", session_context=ctx, session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("retrieved_docs", names)

    def test_slot_skipped_when_no_in_vocab_terms(self):
        self._build_index()
        ctx = {"workspace_path": self.workspace, "active_skills": []}
        # Query is long enough but contains no in-vocab tokens.
        _, trace = build_runtime_assembly(
            {}, [], "zzzzzzzz qqqqqqq xxxxxxxx",
            session_context=ctx, session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("retrieved_docs", names)

    def test_slot_priority_28_between_mentions_and_memory(self):
        self._build_index()
        ctx = {"workspace_path": self.workspace, "active_skills": []}
        _, trace = build_runtime_assembly(
            {}, [], "describe the login flow",
            session_context=ctx,
            session_id=self.sid,
            mentions_context_block="some mention context",
        )
        priorities = {t["name"]: t["priority"] for t in trace}
        self.assertIn("retrieved_docs", priorities)
        self.assertIn("mentions", priorities)
        self.assertGreater(priorities["retrieved_docs"], priorities["mentions"])

    def test_default_slot_budget_for_retrieved_docs_is_10000(self):
        self.assertEqual(pa.SLOT_BUDGET_DEFAULTS["retrieved_docs"], 10_000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
