"""Tests for the custom-instructions feature (Phase 1 + minimal Phase 2).

See ``docs/design/custom-instructions.md``. Each instruction is a
typed, conditional, addressable behavioural rule the model sees on
every chat turn (filtered by its own ``applies_to``).

These tests cover:

* Storage round-trip via ``local_memory.add_instruction`` /
  ``update_instruction`` / ``remove_instruction`` /
  ``set_instruction_enabled`` / ``clear_instructions``.
* ``_normalize_instruction`` rejects unknown kinds, blank bodies,
  malformed ``applies_to``, and clamps numeric fields.
* ``_filter_applicable_instructions`` honours ``enabled``,
  ``mode``, ``workspace``, ``tags``, ``lanes`` predicates with the
  documented AND-across-keys / ANY-across-list semantics.
* ``custom_instructions_slot`` renders kind-ordered headers
  (verbose) and short prefixes (compact).
* ``build_runtime_assembly`` threads runtime context through the
  slot so the right entries land in the prompt.
* The ``instructions`` dispatcher subcommands (list / add / show /
  enable / disable / remove / clear).
"""
import unittest
from uuid import uuid4

from app.memory import local_memory
from app.services import prompt_assembly as pa
from app.services.chat_service import build_runtime_assembly
from app.services.command_dispatcher import dispatch_input


def _fresh_session():
    return f"ci_{uuid4().hex[:10]}"


# ============================================================
# Storage round-trip
# ============================================================

class InstructionsStorageTests(unittest.TestCase):
    def test_default_context_has_empty_instructions_list(self):
        ctx = local_memory._default_context()
        self.assertIn("instructions", ctx)
        self.assertEqual(ctx["instructions"], [])

    def test_get_instructions_empty_for_fresh_session(self):
        sid = _fresh_session()
        self.assertEqual(local_memory.get_instructions(session_id=sid), [])

    def test_add_returns_normalised_dict_with_minted_id(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="constraint",
            body="Never edit migrations.",
        )
        self.assertEqual(created["kind"], "constraint")
        self.assertEqual(created["body"], "Never edit migrations.")
        self.assertTrue(created["id"].startswith("i_"))
        self.assertTrue(created["enabled"])
        self.assertEqual(created["priority"], 50)

    def test_add_persists_to_session_context(self):
        sid = _fresh_session()
        local_memory.add_instruction(
            session_id=sid, kind="style", body="Reply in plain prose.",
        )
        entries = local_memory.get_instructions(session_id=sid)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["body"], "Reply in plain prose.")

    def test_add_uses_explicit_id_when_provided(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="persona",
            body="Be concise.", id="my-persona",
        )
        self.assertEqual(created["id"], "my-persona")

    def test_add_mints_fresh_id_when_supplied_id_collides(self):
        sid = _fresh_session()
        first = local_memory.add_instruction(
            session_id=sid, kind="style", body="A.", id="dup",
        )
        second = local_memory.add_instruction(
            session_id=sid, kind="style", body="B.", id="dup",
        )
        self.assertEqual(first["id"], "dup")
        self.assertNotEqual(second["id"], "dup")

    def test_update_preserves_id_and_applies_changes(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="Before.",
        )
        updated = local_memory.update_instruction(
            session_id=sid, instruction_id=created["id"],
            body="After.", priority=10,
        )
        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["body"], "After.")
        self.assertEqual(updated["priority"], 10)

    def test_update_returns_none_for_missing_id(self):
        sid = _fresh_session()
        self.assertIsNone(local_memory.update_instruction(
            session_id=sid, instruction_id="nope", body="x",
        ))

    def test_remove_returns_true_when_found(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
        )
        self.assertTrue(local_memory.remove_instruction(
            session_id=sid, instruction_id=created["id"],
        ))
        self.assertEqual(local_memory.get_instructions(session_id=sid), [])

    def test_remove_returns_false_when_missing(self):
        sid = _fresh_session()
        self.assertFalse(local_memory.remove_instruction(
            session_id=sid, instruction_id="nope",
        ))

    def test_set_instruction_enabled_flips(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
        )
        self.assertEqual(
            local_memory.set_instruction_enabled(
                session_id=sid, instruction_id=created["id"], enabled=False,
            ),
            False,
        )
        entry = local_memory.get_instruction(
            session_id=sid, instruction_id=created["id"],
        )
        self.assertFalse(entry["enabled"])

    def test_set_enabled_returns_none_for_missing(self):
        sid = _fresh_session()
        self.assertIsNone(local_memory.set_instruction_enabled(
            session_id=sid, instruction_id="nope", enabled=True,
        ))

    def test_clear_instructions_returns_count(self):
        sid = _fresh_session()
        local_memory.add_instruction(session_id=sid, kind="style", body="a.")
        local_memory.add_instruction(session_id=sid, kind="style", body="b.")
        self.assertEqual(local_memory.clear_instructions(session_id=sid), 2)
        self.assertEqual(local_memory.get_instructions(session_id=sid), [])


# ============================================================
# Normalisation
# ============================================================

class InstructionsNormalisationTests(unittest.TestCase):
    def test_unknown_kind_rejected(self):
        sid = _fresh_session()
        with self.assertRaises(ValueError):
            local_memory.add_instruction(
                session_id=sid, kind="bogus", body="x.",
            )

    def test_blank_body_rejected(self):
        sid = _fresh_session()
        with self.assertRaises(ValueError):
            local_memory.add_instruction(
                session_id=sid, kind="style", body="   ",
            )

    def test_oversize_body_truncated_to_char_limit(self):
        sid = _fresh_session()
        body = "a" * 5000
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body=body, char_limit=100,
        )
        self.assertEqual(len(created["body"]), 100)

    def test_unknown_applies_to_keys_dropped(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
            applies_to={"mode": "build", "made_up": "ignored"},
        )
        self.assertEqual(created["applies_to"], {"mode": "build"})

    def test_invalid_mode_in_applies_to_dropped(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
            applies_to={"mode": "shipping"},
        )
        self.assertEqual(created["applies_to"], {})

    def test_priority_clamped_above(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.", priority=999,
        )
        self.assertEqual(created["priority"], 100)

    def test_priority_clamped_below(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.", priority=-50,
        )
        self.assertEqual(created["priority"], 0)

    def test_default_label_is_first_40_chars_of_body(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style",
            body="A rather lengthy body that exceeds forty characters of text.",
        )
        self.assertEqual(len(created["label"]), 40)

    def test_normalise_existing_list_drops_malformed_entries(self):
        cleaned = local_memory._normalize_instructions([
            {"kind": "style", "body": "ok"},
            {"kind": "bogus", "body": "rejected"},
            "not a dict",
            {"kind": "constraint", "body": ""},
            {"kind": "knowledge", "body": "kept"},
        ])
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["body"], "ok")
        self.assertEqual(cleaned[1]["body"], "kept")


# ============================================================
# Filter semantics
# ============================================================

class InstructionsFilterTests(unittest.TestCase):
    def _entry(self, **fields):
        # Build a minimal valid entry then merge overrides on top.
        base = {
            "id": "i_test",
            "label": "test",
            "kind": "style",
            "body": "x.",
            "enabled": True,
            "applies_to": {},
            "priority": 50,
            "char_limit": 2048,
        }
        base.update(fields)
        return base

    def test_empty_applies_to_matches_everywhere(self):
        entries = [self._entry()]
        out = pa._filter_applicable_instructions(entries)
        self.assertEqual(len(out), 1)

    def test_disabled_entry_skipped(self):
        entries = [self._entry(enabled=False)]
        self.assertEqual(pa._filter_applicable_instructions(entries), [])

    def test_mode_predicate_match(self):
        entries = [self._entry(applies_to={"mode": "build"})]
        self.assertEqual(
            len(pa._filter_applicable_instructions(entries, mode="build")),
            1,
        )
        self.assertEqual(
            len(pa._filter_applicable_instructions(entries, mode="plan")),
            0,
        )

    def test_workspace_predicate_exact_match(self):
        entries = [self._entry(applies_to={"workspace": "C:\\repos\\X"})]
        self.assertEqual(
            len(pa._filter_applicable_instructions(
                entries, workspace_path="C:\\repos\\X",
            )),
            1,
        )
        self.assertEqual(
            len(pa._filter_applicable_instructions(
                entries, workspace_path="C:\\repos\\Y",
            )),
            0,
        )

    def test_tags_predicate_any_of(self):
        entries = [self._entry(applies_to={"tags": ["work", "urgent"]})]
        self.assertEqual(
            len(pa._filter_applicable_instructions(entries, tags=["work"])),
            1,
        )
        self.assertEqual(
            len(pa._filter_applicable_instructions(entries, tags=["personal"])),
            0,
        )

    def test_lanes_predicate_any_of(self):
        entries = [self._entry(applies_to={"lanes": ["code", "research"]})]
        self.assertEqual(
            len(pa._filter_applicable_instructions(entries, lane="code")),
            1,
        )
        self.assertEqual(
            len(pa._filter_applicable_instructions(entries, lane="vision")),
            0,
        )

    def test_and_across_keys(self):
        entries = [self._entry(applies_to={
            "mode": "build", "tags": ["work"],
        })]
        # Both match → keep
        self.assertEqual(
            len(pa._filter_applicable_instructions(
                entries, mode="build", tags=["work"],
            )),
            1,
        )
        # mode mismatch → drop
        self.assertEqual(
            len(pa._filter_applicable_instructions(
                entries, mode="plan", tags=["work"],
            )),
            0,
        )
        # tag mismatch → drop
        self.assertEqual(
            len(pa._filter_applicable_instructions(
                entries, mode="build", tags=["personal"],
            )),
            0,
        )

    def test_filter_returns_empty_for_none_input(self):
        self.assertEqual(pa._filter_applicable_instructions(None), [])
        self.assertEqual(pa._filter_applicable_instructions([]), [])


# ============================================================
# Slot rendering
# ============================================================

class InstructionsSlotRenderTests(unittest.TestCase):
    def _entry(self, kind="style", body="x.", **fields):
        return {
            "id": fields.get("id", "i_test"),
            "label": fields.get("label", "test"),
            "kind": kind,
            "body": body,
            "enabled": fields.get("enabled", True),
            "applies_to": fields.get("applies_to", {}),
            "priority": fields.get("priority", 50),
            "char_limit": fields.get("char_limit", 2048),
        }

    def test_empty_list_returns_no_slot(self):
        self.assertIsNone(pa.custom_instructions_slot([]))
        self.assertIsNone(pa.custom_instructions_slot(None))

    def test_no_applicable_returns_no_slot(self):
        # All applies_to filtered out.
        entries = [self._entry(applies_to={"mode": "build"})]
        self.assertIsNone(
            pa.custom_instructions_slot(entries, mode="plan"),
        )

    def test_verbose_render_uses_kind_capital_header(self):
        entries = [self._entry(kind="constraint", label="x", body="never Y.")]
        slot = pa.custom_instructions_slot(entries)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.name, "custom_instructions")
        self.assertEqual(slot.priority, 3)
        self.assertIn("Custom instructions:", slot.content)
        self.assertIn("### Constraint: x", slot.content)
        self.assertIn("never Y.", slot.content)

    def test_compact_render_uses_short_prefixes(self):
        entries = [self._entry(kind="style", label="tone", body="be formal.")]
        slot = pa.custom_instructions_slot(entries, compact=True)
        self.assertIn("S:tone", slot.content)
        # Verbose framing dropped in compact mode.
        self.assertNotIn("Custom instructions:", slot.content)
        self.assertIn("be formal.", slot.content)

    def test_kinds_render_in_documented_order(self):
        # override → constraint → persona → knowledge → style
        entries = [
            self._entry(kind="style", label="s", body="S body."),
            self._entry(kind="override", label="o", body="O body."),
            self._entry(kind="persona", label="p", body="P body."),
            self._entry(kind="constraint", label="c", body="C body."),
            self._entry(kind="knowledge", label="k", body="K body."),
        ]
        # Give every entry a unique id so the filter doesn't dedupe.
        for i, e in enumerate(entries):
            e["id"] = f"i_{i}"
        slot = pa.custom_instructions_slot(entries)
        self.assertIsNotNone(slot)
        # Earliest position wins → indexOf is monotone in expected order.
        positions = [
            slot.content.index("O body."),
            slot.content.index("C body."),
            slot.content.index("P body."),
            slot.content.index("K body."),
            slot.content.index("S body."),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_priority_breaks_within_kind(self):
        entries = [
            self._entry(kind="constraint", label="b", body="B.", priority=80, id="b"),
            self._entry(kind="constraint", label="a", body="A.", priority=10, id="a"),
        ]
        slot = pa.custom_instructions_slot(entries)
        self.assertLess(slot.content.index("A."), slot.content.index("B."))

    def test_disabled_entry_excluded_from_render(self):
        entries = [
            self._entry(kind="style", label="on", body="ON body.", id="x"),
            self._entry(kind="style", label="off", body="OFF body.",
                        enabled=False, id="y"),
        ]
        slot = pa.custom_instructions_slot(entries)
        self.assertIn("ON body.", slot.content)
        self.assertNotIn("OFF body.", slot.content)


# ============================================================
# End-to-end via build_runtime_assembly
# ============================================================

class InstructionsBuildRuntimeAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.sid = _fresh_session()

    def _ctx(self, *, mode="plan", instructions=None, tags=None, ws=None):
        return {
            "workspace_path": ws,
            "active_skills": [],
            "mode": mode,
            "tags": tags or [],
            "instructions": instructions or [],
        }

    def test_slot_absent_when_no_instructions(self):
        _, trace = build_runtime_assembly(
            {}, [], "hello", session_context=self._ctx(),
            session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("custom_instructions", names)

    def test_slot_present_when_instructions_present(self):
        local_memory.add_instruction(
            session_id=self.sid, kind="constraint", body="Never X.",
        )
        ctx = local_memory.get_session_context(session_id=self.sid)
        msgs, trace = build_runtime_assembly(
            {}, [], "hello", session_context=ctx,
            session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertIn("custom_instructions", names)
        joined = "\n".join(m["content"] for m in msgs)
        self.assertIn("Never X.", joined)

    def test_mode_filter_applied_via_runtime_context(self):
        # Build-only instruction; session is in plan → should not fire.
        local_memory.add_instruction(
            session_id=self.sid, kind="constraint",
            body="Run tests after a write.",
            applies_to={"mode": "build"},
        )
        ctx = local_memory.get_session_context(session_id=self.sid)
        ctx["mode"] = "plan"
        msgs, trace = build_runtime_assembly(
            {}, [], "hello", session_context=ctx,
            session_id=self.sid,
        )
        names = [t["name"] for t in trace]
        self.assertNotIn("custom_instructions", names)

    def test_lane_filter_respects_resolved_lane_argument(self):
        local_memory.add_instruction(
            session_id=self.sid, kind="constraint",
            body="Cite line numbers.",
            applies_to={"lanes": ["code"]},
        )
        ctx = local_memory.get_session_context(session_id=self.sid)
        # Call with code lane → fires
        _, trace_code = build_runtime_assembly(
            {}, [], "fix the bug", session_context=ctx,
            session_id=self.sid, resolved_lane="code",
        )
        # Call with vision lane → doesn't fire
        _, trace_vision = build_runtime_assembly(
            {}, [], "fix the bug", session_context=ctx,
            session_id=self.sid, resolved_lane="vision",
        )
        self.assertIn(
            "custom_instructions",
            [t["name"] for t in trace_code],
        )
        self.assertNotIn(
            "custom_instructions",
            [t["name"] for t in trace_vision],
        )

    def test_slot_priority_3_between_mode_and_persona(self):
        local_memory.add_instruction(
            session_id=self.sid, kind="constraint", body="X.",
        )
        ctx = local_memory.get_session_context(session_id=self.sid)
        _, trace = build_runtime_assembly(
            {}, [], "hello", session_context=ctx, session_id=self.sid,
        )
        priorities = {t["name"]: t["priority"] for t in trace}
        self.assertEqual(priorities["custom_instructions"], 3)
        # mode is 0, persona is 5 — custom_instructions sits between.
        self.assertGreater(priorities["custom_instructions"], priorities["mode"])

    def test_default_slot_budget_is_8192(self):
        self.assertEqual(pa.SLOT_BUDGET_DEFAULTS["custom_instructions"], 8192)


# ============================================================
# Dispatcher subcommands
# ============================================================

class InstructionsDispatcherTests(unittest.TestCase):
    def test_list_empty(self):
        sid = _fresh_session()
        result = dispatch_input("instructions list", session_id=sid)
        self.assertEqual(result.get("action"), "instructions")
        self.assertIn("No instructions", result.get("output", ""))
        self.assertEqual(result.get("result"), [])

    def test_default_subcommand_is_list(self):
        sid = _fresh_session()
        # Bare `instructions` → list (no error).
        result = dispatch_input("instructions", session_id=sid)
        self.assertEqual(result.get("action"), "instructions")
        self.assertNotIn("reason", result)

    def test_add_minimal(self):
        sid = _fresh_session()
        result = dispatch_input(
            "instructions add constraint Never edit migrations",
            session_id=sid,
        )
        self.assertEqual(result.get("action"), "instructions")
        self.assertIn("Added:", result["output"])
        self.assertEqual(
            result["result"]["body"], "Never edit migrations",
        )

    def test_add_with_flags(self):
        sid = _fresh_session()
        result = dispatch_input(
            "instructions add style --mode build --label tests "
            "--priority 10 Reply with checklist",
            session_id=sid,
        )
        self.assertEqual(result["result"]["applies_to"]["mode"], "build")
        self.assertEqual(result["result"]["label"], "tests")
        self.assertEqual(result["result"]["priority"], 10)

    def test_add_unknown_kind_returns_invalid_argument(self):
        sid = _fresh_session()
        result = dispatch_input(
            "instructions add fakery x.", session_id=sid,
        )
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_show_renders_full_view(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add knowledge The project uses postgres.",
            session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions show {added['id']}", session_id=sid,
        )
        self.assertIn(added["id"], result["output"])
        self.assertIn("postgres", result["output"])

    def test_show_unknown_id_returns_unknown_target(self):
        sid = _fresh_session()
        result = dispatch_input("instructions show nope", session_id=sid)
        self.assertEqual(result.get("reason"), "unknown_target")

    def test_disable_then_enable_round_trip(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style x.", session_id=sid,
        )["result"]
        d = dispatch_input(
            f"instructions disable {added['id']}", session_id=sid,
        )
        self.assertFalse(d["result"]["enabled"])
        e = dispatch_input(
            f"instructions enable {added['id']}", session_id=sid,
        )
        self.assertTrue(e["result"]["enabled"])

    def test_remove_drops_entry(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style x.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions remove {added['id']}", session_id=sid,
        )
        self.assertTrue(result["result"]["removed"])
        self.assertEqual(
            local_memory.get_instructions(session_id=sid), [],
        )

    def test_remove_unknown_id_returns_unknown_target(self):
        sid = _fresh_session()
        result = dispatch_input("instructions remove nope", session_id=sid)
        self.assertEqual(result.get("reason"), "unknown_target")

    def test_clear_drops_everything(self):
        sid = _fresh_session()
        dispatch_input("instructions add style a.", session_id=sid)
        dispatch_input("instructions add style b.", session_id=sid)
        result = dispatch_input("instructions clear", session_id=sid)
        self.assertEqual(result["result"]["removed"], 2)
        self.assertEqual(
            local_memory.get_instructions(session_id=sid), [],
        )

    def test_unknown_subcommand_returns_invalid_argument(self):
        sid = _fresh_session()
        result = dispatch_input("instructions fooobar", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_list_after_add_shows_table(self):
        sid = _fresh_session()
        dispatch_input(
            "instructions add constraint Never edit migrations",
            session_id=sid,
        )
        result = dispatch_input("instructions list", session_id=sid)
        self.assertIn("constraint", result["output"])
        self.assertIn("Never edit migrations", result["output"])


# ============================================================
# Phase 2.5 — hush + edit + import-persona
# ============================================================

class HushTests(unittest.TestCase):
    def test_default_hushed_remaining_is_zero(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
        )
        self.assertEqual(created["hushed_remaining"], 0)

    def test_hush_sets_remaining(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
        )
        self.assertEqual(
            local_memory.hush_instruction(
                session_id=sid, instruction_id=created["id"], turns=3,
            ),
            3,
        )
        entry = local_memory.get_instruction(
            session_id=sid, instruction_id=created["id"],
        )
        self.assertEqual(entry["hushed_remaining"], 3)
        # ``enabled`` itself is NOT flipped — hush is independent.
        self.assertTrue(entry["enabled"])

    def test_hush_zero_clears(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
        )
        local_memory.hush_instruction(
            session_id=sid, instruction_id=created["id"], turns=5,
        )
        self.assertEqual(
            local_memory.hush_instruction(
                session_id=sid, instruction_id=created["id"], turns=0,
            ),
            0,
        )

    def test_hush_returns_none_for_missing_id(self):
        sid = _fresh_session()
        self.assertIsNone(local_memory.hush_instruction(
            session_id=sid, instruction_id="nope", turns=2,
        ))

    def test_filter_treats_hushed_as_disabled(self):
        entry = {
            "id": "i_x",
            "label": "x",
            "kind": "style",
            "body": "x.",
            "enabled": True,
            "applies_to": {},
            "priority": 50,
            "char_limit": 2048,
            "hushed_remaining": 1,
        }
        self.assertEqual(
            pa._filter_applicable_instructions([entry]),
            [],
        )
        # Drop the hush → entry comes back.
        entry["hushed_remaining"] = 0
        self.assertEqual(
            len(pa._filter_applicable_instructions([entry])),
            1,
        )

    def test_tick_decrements_only_positive_values(self):
        sid = _fresh_session()
        e1 = local_memory.add_instruction(
            session_id=sid, kind="style", body="A.",
        )
        e2 = local_memory.add_instruction(
            session_id=sid, kind="style", body="B.",
        )
        local_memory.hush_instruction(
            session_id=sid, instruction_id=e1["id"], turns=3,
        )
        ticked = local_memory.tick_hushed_instructions(session_id=sid)
        self.assertEqual(ticked, 1)
        # e1 went 3 -> 2; e2 stayed at 0.
        ee1 = local_memory.get_instruction(session_id=sid, instruction_id=e1["id"])
        ee2 = local_memory.get_instruction(session_id=sid, instruction_id=e2["id"])
        self.assertEqual(ee1["hushed_remaining"], 2)
        self.assertEqual(ee2["hushed_remaining"], 0)

    def test_tick_no_op_when_no_hushed_entries(self):
        sid = _fresh_session()
        local_memory.add_instruction(session_id=sid, kind="style", body="A.")
        ticked = local_memory.tick_hushed_instructions(session_id=sid)
        self.assertEqual(ticked, 0)

    def test_tick_clamps_at_zero(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="style", body="x.",
        )
        local_memory.hush_instruction(
            session_id=sid, instruction_id=created["id"], turns=1,
        )
        # First tick: 1 -> 0
        local_memory.tick_hushed_instructions(session_id=sid)
        # Second tick: still 0 (no underflow)
        local_memory.tick_hushed_instructions(session_id=sid)
        entry = local_memory.get_instruction(
            session_id=sid, instruction_id=created["id"],
        )
        self.assertEqual(entry["hushed_remaining"], 0)

    def test_n_turn_hush_skips_n_then_returns(self):
        """End-to-end: hush --turns 3 → 3 hushed turns, 4th returns."""
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="constraint", body="Never X.",
        )
        local_memory.hush_instruction(
            session_id=sid, instruction_id=created["id"], turns=3,
        )
        # Turn 1: hushed_remaining=3, filter excludes, then tick -> 2
        for expected_remaining_at_filter in [3, 2, 1]:
            entry = local_memory.get_instruction(
                session_id=sid, instruction_id=created["id"],
            )
            self.assertEqual(
                entry["hushed_remaining"], expected_remaining_at_filter,
            )
            self.assertEqual(
                pa._filter_applicable_instructions([entry]),
                [],
            )
            local_memory.tick_hushed_instructions(session_id=sid)
        # Turn 4: hushed_remaining=0, entry comes back
        entry = local_memory.get_instruction(
            session_id=sid, instruction_id=created["id"],
        )
        self.assertEqual(entry["hushed_remaining"], 0)
        self.assertEqual(
            len(pa._filter_applicable_instructions([entry])),
            1,
        )


class EditDispatcherTests(unittest.TestCase):
    def test_edit_body(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Original body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions edit {added['id']} --body New body content",
            session_id=sid,
        )
        self.assertEqual(result["result"]["body"], "New body content")
        # ID preserved.
        self.assertEqual(result["result"]["id"], added["id"])

    def test_edit_kind(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions edit {added['id']} --kind constraint",
            session_id=sid,
        )
        self.assertEqual(result["result"]["kind"], "constraint")

    def test_edit_priority_int(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions edit {added['id']} --priority 10",
            session_id=sid,
        )
        self.assertEqual(result["result"]["priority"], 10)

    def test_edit_merges_applies_to_keys(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style --tags work,urgent Body.", session_id=sid,
        )["result"]
        # Add --mode without losing --tags
        result = dispatch_input(
            f"instructions edit {added['id']} --mode build",
            session_id=sid,
        )
        applies_to = result["result"]["applies_to"]
        self.assertEqual(applies_to.get("mode"), "build")
        self.assertEqual(applies_to.get("tags"), ["work", "urgent"])

    def test_edit_no_mode_clears_only_that_key(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style --mode build --tags work Body.",
            session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions edit {added['id']} --no-mode",
            session_id=sid,
        )
        applies_to = result["result"]["applies_to"]
        self.assertNotIn("mode", applies_to)
        self.assertEqual(applies_to.get("tags"), ["work"])

    def test_edit_unknown_id_returns_unknown_target(self):
        sid = _fresh_session()
        result = dispatch_input(
            "instructions edit nope --body x", session_id=sid,
        )
        self.assertEqual(result.get("reason"), "unknown_target")

    def test_edit_no_changes_returns_invalid_argument(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions edit {added['id']}", session_id=sid,
        )
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_edit_body_consumes_until_next_flag(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Old.", session_id=sid,
        )["result"]
        # Multi-word body, then a flag.
        result = dispatch_input(
            f"instructions edit {added['id']} --body Reply only in plain prose --priority 25",
            session_id=sid,
        )
        self.assertEqual(
            result["result"]["body"], "Reply only in plain prose",
        )
        self.assertEqual(result["result"]["priority"], 25)


class HushDispatcherTests(unittest.TestCase):
    def test_hush_default_one_turn(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions hush {added['id']}", session_id=sid,
        )
        self.assertEqual(result["result"]["hushed_remaining"], 1)

    def test_hush_explicit_turns(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions hush {added['id']} --turns 5", session_id=sid,
        )
        self.assertEqual(result["result"]["hushed_remaining"], 5)
        # Plural in output.
        self.assertIn("5 more turns", result["output"])

    def test_hush_zero_clears(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        dispatch_input(
            f"instructions hush {added['id']} --turns 5", session_id=sid,
        )
        result = dispatch_input(
            f"instructions hush {added['id']} --turns 0", session_id=sid,
        )
        self.assertEqual(result["result"]["hushed_remaining"], 0)
        self.assertIn("hush cleared", result["output"])

    def test_hush_unknown_id_returns_unknown_target(self):
        sid = _fresh_session()
        result = dispatch_input("instructions hush nope", session_id=sid)
        self.assertEqual(result.get("reason"), "unknown_target")

    def test_hush_negative_turns_rejected(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions hush {added['id']} --turns -3", session_id=sid,
        )
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_hush_non_integer_turns_rejected(self):
        sid = _fresh_session()
        added = dispatch_input(
            "instructions add style Body.", session_id=sid,
        )["result"]
        result = dispatch_input(
            f"instructions hush {added['id']} --turns abc", session_id=sid,
        )
        self.assertEqual(result.get("reason"), "invalid_argument")


class ImportPersonaTests(unittest.TestCase):
    def test_kind_from_heading_maps_known_words(self):
        from app.services.command_dispatcher import _kind_from_persona_heading
        self.assertEqual(_kind_from_persona_heading("Tone"), "style")
        self.assertEqual(_kind_from_persona_heading("Constraints"), "constraint")
        self.assertEqual(_kind_from_persona_heading("Background"), "knowledge")
        self.assertEqual(_kind_from_persona_heading("Persona"), "persona")
        self.assertEqual(_kind_from_persona_heading("Overrides"), "override")

    def test_kind_from_heading_falls_back_to_persona(self):
        from app.services.command_dispatcher import _kind_from_persona_heading
        self.assertEqual(
            _kind_from_persona_heading("Some Custom Section"),
            "persona",
        )

    def test_parse_persona_markdown_extracts_sections(self):
        from app.services.command_dispatcher import _parse_persona_markdown
        text = (
            "# Persona\nIntro text.\n\n"
            "## Tone\nBe concise.\n\n"
            "## Constraints\n- Never X.\n- Always Y.\n"
        )
        sections = _parse_persona_markdown(text)
        self.assertEqual(len(sections), 3)
        kinds = [s[0] for s in sections]
        self.assertEqual(kinds, ["persona", "style", "constraint"])
        self.assertIn("Be concise.", sections[1][2])

    def test_parse_persona_markdown_skips_empty_sections(self):
        from app.services.command_dispatcher import _parse_persona_markdown
        text = (
            "# Persona\n\n"  # Empty body
            "## Tone\nBe concise.\n"
        )
        sections = _parse_persona_markdown(text)
        # Empty-body section dropped.
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0], "style")

    def test_import_persona_no_workspace_returns_no_workspace(self):
        sid = _fresh_session()
        result = dispatch_input("instructions import-persona", session_id=sid)
        self.assertEqual(result.get("reason"), "no_workspace")

    def test_import_persona_creates_one_per_section(self):
        import os
        import tempfile
        sid = _fresh_session()
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "PERSONA.md"), "w") as fh:
                fh.write(
                    "# Persona\nA careful engineer.\n\n"
                    "# Tone\nBe concise.\n\n"
                    "# Constraints\nNever edit migrations.\n"
                )
            local_memory.set_workspace(session_id=sid, workspace_path=tmp)
            result = dispatch_input("instructions import-persona", session_id=sid)
            self.assertEqual(len(result["result"]), 3)
            kinds = [e["kind"] for e in result["result"]]
            self.assertIn("persona", kinds)
            self.assertIn("style", kinds)
            self.assertIn("constraint", kinds)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            local_memory.clear_instructions(session_id=sid)

    def test_import_persona_missing_file_returns_unknown_target(self):
        import tempfile
        sid = _fresh_session()
        tmp = tempfile.mkdtemp()
        try:
            # Workspace pinned but no PERSONA.md inside.
            local_memory.set_workspace(session_id=sid, workspace_path=tmp)
            result = dispatch_input("instructions import-persona", session_id=sid)
            self.assertEqual(result.get("reason"), "unknown_target")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class HushIntegrationTests(unittest.TestCase):
    """Hush + tick + slot filter as a single end-to-end flow."""

    def test_hushed_entry_excluded_from_runtime_assembly(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="constraint", body="Never X.",
        )
        local_memory.hush_instruction(
            session_id=sid, instruction_id=created["id"], turns=2,
        )
        ctx = local_memory.get_session_context(session_id=sid)
        _, trace = build_runtime_assembly(
            {}, [], "hello", session_context=ctx, session_id=sid,
        )
        names = [t["name"] for t in trace]
        # No applicable instructions → slot absent.
        self.assertNotIn("custom_instructions", names)

    def test_hushed_entry_returns_after_two_ticks_when_hush_was_2(self):
        sid = _fresh_session()
        created = local_memory.add_instruction(
            session_id=sid, kind="constraint", body="Never X.",
        )
        local_memory.hush_instruction(
            session_id=sid, instruction_id=created["id"], turns=2,
        )
        # Tick twice → hushed_remaining hits 0.
        local_memory.tick_hushed_instructions(session_id=sid)
        local_memory.tick_hushed_instructions(session_id=sid)
        ctx = local_memory.get_session_context(session_id=sid)
        _, trace = build_runtime_assembly(
            {}, [], "hello", session_context=ctx, session_id=sid,
        )
        names = [t["name"] for t in trace]
        self.assertIn("custom_instructions", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
