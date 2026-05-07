"""Tests for agent-rules Phase 1 (`docs/design/agent-rules.md`).

Four layers:
  - Parser (TOML + JSON → Rule, with shape validation)
  - Predicate evaluator (`event_kind` / `action` / `session_id` /
    `session_tag` / `text_match` / `text_regex` / `path_glob`)
  - Pre-dispatch refusal flow (a refuse rule short-circuits a matching
    dispatch_input call)
  - Dispatcher commands (`rules list / show / reload / disable / enable`,
    `tags add / remove / clear`)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import rule_service as rs
from app.services.command_dispatcher import dispatch_input
from app.services.gateway.bus import (
    KIND_RULE,
    default_bus,
    reset_default_bus_for_tests,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# ============================================================
# Parser
# ============================================================

class RuleParserTests(unittest.TestCase):
    def test_parse_minimal_refuse_rule(self):
        data = {
            "id": "block-deletes",
            "description": "Refuse all deletes",
            "when": {"event_kind": "dispatch", "action": "delete"},
            "then": {"refuse": {"reason": "invalid_argument", "output": "policy"}},
        }
        rule = rs.parse_rule(data)
        self.assertEqual(rule.id, "block-deletes")
        self.assertEqual(rule.when["action"], "delete")
        self.assertIn("refuse", rule.then)

    def test_parse_minimal_forbid_provider_rule(self):
        data = {
            "id": "no-cloud-on-prod",
            "when": {"session_tag": "production"},
            "then": {"forbid_provider": {"not_in": ["ollama"], "reason": "production_local_only"}},
        }
        rule = rs.parse_rule(data)
        self.assertIn("forbid_provider", rule.then)

    def test_invalid_id_raises(self):
        with self.assertRaises(rs.RuleParseError) as ctx:
            rs.parse_rule({
                "id": "Has Spaces",
                "when": {"action": "x"},
                "then": {"refuse": {"reason": "x"}},
            })
        self.assertEqual(ctx.exception.reason, "invalid_argument")

    def test_empty_when_raises(self):
        with self.assertRaises(rs.RuleParseError):
            rs.parse_rule({
                "id": "always-fires",
                "when": {},
                "then": {"refuse": {"reason": "x"}},
            })

    def test_unknown_when_key_raises(self):
        with self.assertRaises(rs.RuleParseError) as ctx:
            rs.parse_rule({
                "id": "weird-pred",
                "when": {"unknown_key": "x"},
                "then": {"refuse": {"reason": "x"}},
            })
        self.assertIn("unknown when-key", str(ctx.exception))

    def test_then_without_action_raises(self):
        with self.assertRaises(rs.RuleParseError):
            rs.parse_rule({
                "id": "no-action",
                "when": {"action": "x"},
                "then": {},
            })

    def test_unknown_then_key_raises(self):
        with self.assertRaises(rs.RuleParseError):
            rs.parse_rule({
                "id": "weird-action",
                "when": {"action": "x"},
                "then": {"summon_demon": {"reason": "x"}},
            })

    def test_refuse_without_reason_raises(self):
        with self.assertRaises(rs.RuleParseError):
            rs.parse_rule({
                "id": "no-reason",
                "when": {"action": "x"},
                "then": {"refuse": {"output": "no reason"}},
            })

    def test_invalid_text_regex_raises(self):
        with self.assertRaises(rs.RuleParseError) as ctx:
            rs.parse_rule({
                "id": "bad-regex",
                "when": {"text_regex": "(unclosed"},
                "then": {"refuse": {"reason": "x"}},
            })
        self.assertEqual(ctx.exception.reason, "invalid_argument")


class RuleFileLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        rs._reset_registry_for_tests()

    def tearDown(self):
        self._tmp.cleanup()
        rs._reset_registry_for_tests()

    def test_load_toml(self):
        path = Path(self._tmp.name) / "x.toml"
        path.write_text(
            'id = "block"\n'
            '[when]\n'
            'action = "delete"\n'
            '[then.refuse]\n'
            'reason = "invalid_argument"\n'
            'output = "no"\n',
            encoding="utf-8",
        )
        rule = rs.load_rule_file(path)
        self.assertEqual(rule.id, "block")

    def test_load_json(self):
        path = Path(self._tmp.name) / "x.json"
        path.write_text(
            json.dumps({
                "id": "block-json",
                "when": {"action": "delete"},
                "then": {"refuse": {"reason": "invalid_argument", "output": "no"}},
            }),
            encoding="utf-8",
        )
        rule = rs.load_rule_file(path)
        self.assertEqual(rule.id, "block-json")

    def test_unsupported_extension_raises(self):
        path = Path(self._tmp.name) / "x.yaml"
        path.write_text("id: y", encoding="utf-8")
        with self.assertRaises(rs.RuleParseError):
            rs.load_rule_file(path)

    def test_reload_skips_bad_rules(self):
        good = Path(self._tmp.name) / "good.toml"
        good.write_text(
            'id = "good"\n[when]\naction = "x"\n[then.refuse]\nreason = "invalid_argument"\n',
            encoding="utf-8",
        )
        bad = Path(self._tmp.name) / "bad.toml"
        bad.write_text("id = ''\n", encoding="utf-8")  # invalid id
        registry = rs.reload_rules(rules_dir=Path(self._tmp.name))
        self.assertIn("good", registry)
        self.assertEqual(len(registry), 1)


# ============================================================
# Predicate evaluator
# ============================================================

class PredicateEvaluatorTests(unittest.TestCase):
    def _rule(self, when, then=None):
        return rs.parse_rule({
            "id": f"r-{abs(hash(repr(when)))}"[:30].replace("-", "x"),
            "when": when,
            "then": then or {"refuse": {"reason": "invalid_argument"}},
        })

    def test_event_kind_match(self):
        rule = self._rule({"event_kind": "dispatch"})
        event = {"event_kind": "dispatch", "action": "x"}
        self.assertTrue(rs._matches_predicate(rule, event, []))

    def test_event_kind_mismatch(self):
        rule = self._rule({"event_kind": "task"})
        event = {"event_kind": "dispatch"}
        self.assertFalse(rs._matches_predicate(rule, event, []))

    def test_action_match(self):
        rule = self._rule({"action": "delete"})
        event = {"event_kind": "dispatch", "action": "delete"}
        self.assertTrue(rs._matches_predicate(rule, event, []))

    def test_session_id_match(self):
        rule = self._rule({"session_id": "prod"})
        self.assertTrue(rs._matches_predicate(rule, {"session_id": "prod"}, []))
        self.assertFalse(rs._matches_predicate(rule, {"session_id": "dev"}, []))

    def test_session_tag_match(self):
        rule = self._rule({"session_tag": "production"})
        event = {"session_id": "x"}
        self.assertTrue(rs._matches_predicate(rule, event, ["production"]))
        self.assertFalse(rs._matches_predicate(rule, event, ["staging"]))

    def test_text_match_substring(self):
        rule = self._rule({"text_match": "rm -rf"})
        self.assertTrue(rs._matches_predicate(rule, {"text": "do rm -rf /"}, []))
        self.assertFalse(rs._matches_predicate(rule, {"text": "do safe thing"}, []))

    def test_text_regex_match(self):
        rule = self._rule({"text_regex": r"\bsudo\s+rm\b"})
        self.assertTrue(rs._matches_predicate(rule, {"text": "sudo rm bad"}, []))
        self.assertFalse(rs._matches_predicate(rule, {"text": "rm without sudo"}, []))

    def test_path_glob_match_via_text(self):
        rule = self._rule({"path_glob": "src/**", "action": "write"})
        # `_matches_predicate` extracts the path from a `write <path> ...` text.
        event = {
            "event_kind": "dispatch",
            "action": "write",
            "text": "write src/foo/bar.py <<EOF\nx\nEOF",
        }
        self.assertTrue(rs._matches_predicate(rule, event, []))

    def test_multiple_keys_anded(self):
        rule = self._rule({"action": "delete", "session_tag": "production"})
        event = {"action": "delete"}
        self.assertFalse(rs._matches_predicate(rule, event, []))  # tag missing
        self.assertTrue(rs._matches_predicate(rule, event, ["production"]))


# ============================================================
# Pre-dispatch refusal flow
# ============================================================

class PreDispatchRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        rs._reset_registry_for_tests()
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        rs._reset_registry_for_tests()
        reset_default_bus_for_tests()

    def _register(self, **kwargs):
        rs.register_rule_for_tests(rs.parse_rule(kwargs))

    def test_refuse_rule_short_circuits_dispatch(self):
        self._register(
            id="block-deletes",
            description="No deletes",
            when={"action": "delete"},
            then={"refuse": {"reason": "invalid_argument", "output": "deletes blocked"}},
        )
        ev = dispatch_input("delete some-file.txt", session_id="default")
        self.assertEqual(ev["reason"], "invalid_argument")
        self.assertEqual(ev["rule_id"], "block-deletes")
        self.assertIn("deletes blocked", ev["output"])
        self.assertIn("block-deletes", ev["output"])

    def test_non_matching_rule_lets_dispatch_through(self):
        self._register(
            id="only-on-prod",
            when={"session_tag": "production"},
            then={"refuse": {"reason": "invalid_argument", "output": "prod only"}},
        )
        # No `production` tag on this session → rule doesn't fire → dispatch
        # falls through to the regular handler. `inbox` is harmless.
        ev = dispatch_input("inbox", session_id="default")
        self.assertNotEqual(ev.get("rule_id"), "only-on-prod")

    def test_session_tag_predicate_matches_when_tag_present(self):
        local_memory.add_tag(session_id="default", tag="production")
        self._register(
            id="prod-no-installs",
            when={"session_tag": "production", "action": "install"},
            then={"refuse": {"reason": "invalid_argument", "output": "install blocked on prod"}},
        )
        ev = dispatch_input("install vision", session_id="default")
        self.assertEqual(ev.get("rule_id"), "prod-no-installs")

    def test_disabled_rule_does_not_fire(self):
        self._register(
            id="block-deletes",
            when={"action": "delete"},
            then={"refuse": {"reason": "invalid_argument", "output": "no"}},
        )
        rs.disable_rule("block-deletes")
        # delete falls through; without rule it produces its OWN reason
        # (no_workspace) but NOT rule_id.
        ev = dispatch_input("delete x", session_id="default")
        self.assertNotIn("rule_id", ev)

    def test_rules_command_is_exempt_from_refusal(self):
        # Even if a rule says "refuse everything", `rules` itself must
        # always work so the user can disable the rogue rule.
        self._register(
            id="refuse-all",
            when={"event_kind": "dispatch"},
            then={"refuse": {"reason": "invalid_argument", "output": "all blocked"}},
        )
        ev = dispatch_input("rules", session_id="default")
        # Should NOT be refused.
        self.assertNotIn("rule_id", ev)
        self.assertEqual(ev["action"], "rules")

    def test_help_is_exempt_from_refusal(self):
        self._register(
            id="refuse-all",
            when={"event_kind": "dispatch"},
            then={"refuse": {"reason": "invalid_argument", "output": "all blocked"}},
        )
        ev = dispatch_input("help", session_id="default")
        self.assertNotIn("rule_id", ev)

    def test_refusal_publishes_kind_rule_event(self):
        self._register(
            id="block-deletes",
            when={"action": "delete"},
            then={"refuse": {"reason": "invalid_argument", "output": "no"}},
        )
        captured = []
        bus = default_bus()
        bus.subscribe(bus.ANY, lambda e: captured.append(e))
        dispatch_input("delete x", session_id="default")

        rule_events = [e for e in captured if e.kind == KIND_RULE]
        self.assertEqual(len(rule_events), 1)
        self.assertEqual(rule_events[0].payload["rule_id"], "block-deletes")
        self.assertEqual(rule_events[0].payload["action"], "refuse")

    def test_forbid_provider_publishes_event_but_does_not_short_circuit(self):
        # Phase 1 stub: forbid_provider matches publish a KIND_RULE event
        # but don't actually refuse the dispatch (chat-providers ships later).
        self._register(
            id="prod-local-only",
            when={"event_kind": "dispatch", "action": "inbox"},
            then={"forbid_provider": {"not_in": ["ollama"], "reason": "production_local_only"}},
        )
        captured = []
        bus = default_bus()
        bus.subscribe(bus.ANY, lambda e: captured.append(e))
        ev = dispatch_input("inbox", session_id="default")

        # Dispatch still succeeded (no rule_id field).
        self.assertNotIn("rule_id", ev)
        # But the rule fired on the bus.
        rule_events = [e for e in captured if e.kind == KIND_RULE]
        self.assertEqual(len(rule_events), 1)
        self.assertEqual(rule_events[0].payload["action"], "forbid_provider")


# ============================================================
# Dispatcher surface (rules / tags commands)
# ============================================================

class RulesDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        rs._reset_registry_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        rs._reset_registry_for_tests()

    def test_rules_list_with_no_rules(self):
        ev = dispatch_input("rules", session_id="default")
        self.assertIn("No rules registered", ev["output"])

    def test_rules_show_unknown_emits_unknown_target(self):
        ev = dispatch_input("rules show ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_rules_disable_unknown_emits_unknown_target(self):
        ev = dispatch_input("rules disable ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_rules_disable_then_enable_round_trip(self):
        rs.register_rule_for_tests(rs.parse_rule({
            "id": "x",
            "when": {"action": "y"},
            "then": {"refuse": {"reason": "invalid_argument"}},
        }))
        d = dispatch_input("rules disable x", session_id="default")
        self.assertNotIn("reason", d)
        self.assertTrue(rs.is_disabled("x"))
        e = dispatch_input("rules enable x", session_id="default")
        self.assertNotIn("reason", e)
        self.assertFalse(rs.is_disabled("x"))


class TagsDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_tags_show_when_empty(self):
        ev = dispatch_input("tags", session_id="default")
        self.assertIn("no tags", ev["output"].lower())

    def test_tags_add_then_show(self):
        ev = dispatch_input("tags add production", session_id="default")
        self.assertIn("production", ev["output"])
        self.assertEqual(local_memory.get_tags("default"), ["production"])

    def test_tags_remove(self):
        local_memory.add_tag(session_id="default", tag="production")
        ev = dispatch_input("tags remove production", session_id="default")
        self.assertNotIn("production", ev["output"])

    def test_tags_remove_nonexistent_emits_unknown_target(self):
        ev = dispatch_input("tags remove ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_tags_clear(self):
        local_memory.add_tag(session_id="default", tag="a")
        local_memory.add_tag(session_id="default", tag="b")
        dispatch_input("tags clear", session_id="default")
        self.assertEqual(local_memory.get_tags("default"), [])

    def test_tags_add_invalid_emits_invalid_argument(self):
        ev = dispatch_input("tags add !!!bad", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")


if __name__ == "__main__":
    unittest.main()
