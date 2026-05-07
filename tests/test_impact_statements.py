"""Tests for tier resolution + shell-generated impact statements +
L3 typed confirmation (Phase 2 of `docs/design/patch-taxonomy.md`).

Covers:
* ``tier_for`` per-kind defaults + explicit override
* ``impact_statement_for`` produces non-empty text for every kind
  the existing approval gate emits today
* ``confirmation_phrase_for`` is deterministic, lowercase-safe,
  short, and only set for L3 patches
* ``confirmation_matches`` is case-insensitive, rejects wrong
  phrases, accepts non-L3 trivially
* ``format_approval_request`` renders the tier badge + L3 prompt
* End-to-end via dispatcher: plain ``approve`` refused for L3,
  ``approve <phrase>`` succeeds, ``deny`` clears L3 same as L1/L2
"""
import os
import tempfile
import unittest

from app.memory import local_memory
from app.services import impact_statements as imp
from app.services.command_dispatcher import dispatch_input
from app.services.workspace_service import format_approval_request


# ============================================================
# tier_for
# ============================================================

class TierForTests(unittest.TestCase):
    def test_default_for_write_is_l2(self):
        self.assertEqual(imp.tier_for({"kind": "write"}), "L2")

    def test_default_for_delete_is_l2(self):
        self.assertEqual(imp.tier_for({"kind": "delete"}), "L2")

    def test_default_for_bundle_is_l2(self):
        self.assertEqual(imp.tier_for({"kind": "bundle"}), "L2")

    def test_default_for_read_is_l1(self):
        self.assertEqual(imp.tier_for({"kind": "read"}), "L1")

    def test_default_for_search_is_l1(self):
        self.assertEqual(imp.tier_for({"kind": "search"}), "L1")

    def test_explicit_tier_override_wins(self):
        self.assertEqual(imp.tier_for({"kind": "write", "tier": "L3"}), "L3")
        self.assertEqual(imp.tier_for({"kind": "read", "tier": "L2"}), "L2")

    def test_invalid_tier_value_falls_back_to_kind_default(self):
        self.assertEqual(imp.tier_for({"kind": "write", "tier": "L99"}), "L2")
        self.assertEqual(imp.tier_for({"kind": "write", "tier": ""}), "L2")

    def test_unknown_kind_falls_back_to_l2(self):
        self.assertEqual(imp.tier_for({"kind": "bogus"}), "L2")

    def test_none_input_returns_l2(self):
        self.assertEqual(imp.tier_for(None), "L2")
        self.assertEqual(imp.tier_for("not a dict"), "L2")


# ============================================================
# impact_statement_for
# ============================================================

class ImpactStatementForTests(unittest.TestCase):
    def test_write_existing_file_says_modify(self):
        text = imp.impact_statement_for({"kind": "write", "path": "x.py", "create": False})
        self.assertIn("MODIFY", text)
        self.assertIn("x.py", text)

    def test_write_new_file_says_create(self):
        text = imp.impact_statement_for({"kind": "write", "path": "x.py", "create": True})
        self.assertIn("CREATE", text)
        self.assertIn("x.py", text)

    def test_delete_says_delete_and_undo_path(self):
        text = imp.impact_statement_for({"kind": "delete", "path": "x.py"})
        self.assertIn("DELETE", text)
        self.assertIn("x.py", text)
        self.assertIn("undo", text)

    def test_bundle_counts_writes_and_deletes(self):
        text = imp.impact_statement_for({
            "kind": "bundle",
            "sub_actions": [
                {"kind": "write", "path": "a.py"},
                {"kind": "write", "path": "b.py"},
                {"kind": "delete", "path": "c.py"},
            ],
        })
        self.assertIn("2 writes", text)
        self.assertIn("1 delete", text)
        self.assertIn("rolls back", text)

    def test_cowork_plan_mentions_run_id(self):
        text = imp.impact_statement_for({"kind": "cowork_plan", "run_id": "abc123"})
        self.assertIn("abc123", text)
        self.assertIn("cowork", text.lower())

    def test_browser_navigate_mentions_origin(self):
        text = imp.impact_statement_for({"kind": "browser_navigate", "origin_host": "example.com"})
        self.assertIn("example.com", text)
        self.assertIn("allow-list", text.lower())

    def test_system_prompt_update_warns_about_global_effect(self):
        text = imp.impact_statement_for({"kind": "system_prompt_update"})
        self.assertIn("CONTROL_SYSTEM_MESSAGE", text)
        self.assertIn("every chat turn", text)

    def test_unknown_kind_returns_empty(self):
        self.assertEqual(imp.impact_statement_for({"kind": "xyz"}), "")

    def test_none_input_returns_empty(self):
        self.assertEqual(imp.impact_statement_for(None), "")


# ============================================================
# confirmation_phrase_for + confirmation_matches
# ============================================================

class ConfirmationPhraseTests(unittest.TestCase):
    def test_non_l3_returns_empty_string(self):
        # L2 patches don't need a phrase.
        self.assertEqual(imp.confirmation_phrase_for({"kind": "write"}), "")
        # L1 patches don't either.
        self.assertEqual(imp.confirmation_phrase_for({"kind": "read"}), "")

    def test_l3_returns_short_lowercase_phrase(self):
        action = {"kind": "write", "tier": "L3", "path": "x", "content": "y"}
        phrase = imp.confirmation_phrase_for(action)
        self.assertTrue(phrase)
        self.assertEqual(phrase, phrase.lower())
        self.assertGreaterEqual(len(phrase), 4)
        self.assertLessEqual(len(phrase), 8)

    def test_phrase_is_deterministic(self):
        action1 = {"kind": "write", "tier": "L3", "path": "x", "content": "y"}
        action2 = {"kind": "write", "tier": "L3", "path": "x", "content": "y"}
        self.assertEqual(
            imp.confirmation_phrase_for(action1),
            imp.confirmation_phrase_for(action2),
        )

    def test_phrase_changes_with_content(self):
        a = {"kind": "write", "tier": "L3", "path": "x", "content": "old"}
        b = {"kind": "write", "tier": "L3", "path": "x", "content": "new"}
        self.assertNotEqual(
            imp.confirmation_phrase_for(a),
            imp.confirmation_phrase_for(b),
        )

    def test_phrase_uses_no_confusable_chars(self):
        # i / l / o / u / 0 / 1 should NEVER appear (Crockford-ish).
        for seed in range(50):
            action = {
                "kind": "write", "tier": "L3", "path": f"x{seed}", "content": str(seed),
            }
            phrase = imp.confirmation_phrase_for(action)
            for confusable in "iloU01":
                self.assertNotIn(
                    confusable.lower(), phrase,
                    f"phrase {phrase!r} from seed {seed} contains confusable {confusable!r}",
                )

    def test_matches_case_insensitive(self):
        action = {"kind": "write", "tier": "L3", "path": "x", "content": "y"}
        phrase = imp.confirmation_phrase_for(action)
        self.assertTrue(imp.confirmation_matches(action, phrase.upper()))
        self.assertTrue(imp.confirmation_matches(action, f"  {phrase}  "))

    def test_matches_rejects_wrong_phrase(self):
        action = {"kind": "write", "tier": "L3", "path": "x", "content": "y"}
        self.assertFalse(imp.confirmation_matches(action, "wrongphrase"))
        self.assertFalse(imp.confirmation_matches(action, ""))

    def test_matches_returns_true_for_non_l3(self):
        # Non-L3 patches don't gate on a phrase, so any input matches.
        self.assertTrue(imp.confirmation_matches({"kind": "write"}, ""))
        self.assertTrue(imp.confirmation_matches({"kind": "write"}, "anything"))


# ============================================================
# format_approval_request — banner rendering
# ============================================================

class ApprovalBannerRenderingTests(unittest.TestCase):
    def test_l1_banner_compact(self):
        action = {"kind": "read", "paths": ["x.py"], "workspace_path": "/ws"}
        banner = format_approval_request(action)
        self.assertIn("Tier: L1", banner)
        self.assertIn("read x.py", banner)
        # L1 banners do NOT include an "Impact:" line (the
        # description is enough since the change is reversible).
        self.assertNotIn("Impact:", banner)

    def test_l2_banner_includes_impact(self):
        action = {
            "kind": "write", "path": "x.py", "create": True,
            "workspace_path": "/ws",
        }
        banner = format_approval_request(action)
        self.assertIn("Tier: L2", banner)
        self.assertIn("Impact:", banner)
        self.assertIn("CREATE", banner)
        self.assertIn("Type `approve`", banner)

    def test_l3_banner_includes_phrase_and_impact(self):
        action = {
            "kind": "write", "path": "CONTROL_SYSTEM_MESSAGE.md",
            "content": "new", "create": True,
            "workspace_path": "/ws", "tier": "L3",
        }
        banner = format_approval_request(action)
        self.assertIn("Tier: L3", banner)
        self.assertIn("Impact:", banner)
        phrase = imp.confirmation_phrase_for(action)
        self.assertIn(phrase, banner)
        self.assertIn("approve", banner)
        self.assertIn("high-impact", banner)


# ============================================================
# Dispatcher end-to-end
# ============================================================

class ApproveDispatcherWithConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _stage_l3_pending(self, sid):
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        local_memory.set_mode(session_id=sid, mode="build")
        action = {
            "kind": "write",
            "origin": "chat",
            "workspace_path": self.workspace,
            "path": "high_impact.txt",
            "content": "important",
            "create": True,
            "tier": "L3",
        }
        local_memory.set_pending_approval(session_id=sid, pending_approval=action)
        return local_memory.get_pending_approval(session_id=sid)

    def test_plain_approve_against_l3_is_refused_with_approval_required(self):
        sid = "l3_plain_approve_refused"
        self._stage_l3_pending(sid)
        result = dispatch_input("approve", session_id=sid)
        self.assertEqual(result.get("reason"), "approval_required")
        # Pending action remains so the user can retry.
        self.assertIsNotNone(local_memory.get_pending_approval(session_id=sid))

    def test_wrong_phrase_against_l3_is_refused(self):
        sid = "l3_wrong_phrase"
        self._stage_l3_pending(sid)
        result = dispatch_input("approve nope", session_id=sid)
        self.assertEqual(result.get("reason"), "approval_required")
        self.assertIsNotNone(local_memory.get_pending_approval(session_id=sid))

    def test_correct_phrase_against_l3_succeeds(self):
        sid = "l3_correct_phrase"
        pending = self._stage_l3_pending(sid)
        phrase = imp.confirmation_phrase_for(pending)
        result = dispatch_input(f"approve {phrase}", session_id=sid)
        # Success: no reason, file landed, pending cleared.
        self.assertNotIn("reason", result)
        self.assertTrue(
            os.path.exists(os.path.join(self.workspace, "high_impact.txt"))
        )
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_correct_phrase_is_case_insensitive(self):
        sid = "l3_case_insensitive"
        pending = self._stage_l3_pending(sid)
        phrase = imp.confirmation_phrase_for(pending).upper()
        result = dispatch_input(f"approve {phrase}", session_id=sid)
        self.assertNotIn("reason", result)

    def test_l2_write_accepts_plain_approve(self):
        # L2 is today's default — confirmation arg is ignored.
        sid = "l2_plain_approve"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        local_memory.set_mode(session_id=sid, mode="build")
        action = {
            "kind": "write",
            "origin": "chat",
            "workspace_path": self.workspace,
            "path": "regular.txt",
            "content": "hi",
            "create": True,
        }
        local_memory.set_pending_approval(session_id=sid, pending_approval=action)
        result = dispatch_input("approve", session_id=sid)
        self.assertNotIn("reason", result)

    def test_l2_write_accepts_approve_with_extra_arg(self):
        # `approve foo` against an L2 patch ignores the arg.
        sid = "l2_approve_with_arg"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        local_memory.set_mode(session_id=sid, mode="build")
        action = {
            "kind": "write",
            "origin": "chat",
            "workspace_path": self.workspace,
            "path": "regular2.txt",
            "content": "hi",
            "create": True,
        }
        local_memory.set_pending_approval(session_id=sid, pending_approval=action)
        result = dispatch_input("approve whatever", session_id=sid)
        self.assertNotIn("reason", result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
