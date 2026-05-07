"""Tests for the patch taxonomy (Phase 1 — see
`docs/design/patch-taxonomy.md`).

Covers:
* ``Patch`` dataclass round-trip + kind constants
* ``validate_patch`` per-kind shape rules
* ``bundle_from_action_dicts`` composition helper
* ``_normalize_pending_approval`` accepting ``kind="bundle"``
* ``_maybe_chat_write_proposal`` emitting a bundle when multiple
  fenced blocks are valid (single-block path unchanged)
* End-to-end ``approve`` of a bundle: every sub-action applies,
  every backup lands on the stack
* **Atomic rollback** on partial failure: if sub-action #N fails,
  the previously-applied #1..#N-1 are undone
* ``deny`` of a bundle clears the pending action
* ``undo`` 1× rolls back ONE sub-action (the bundle backups stack
  individually so the user can roll back partially)
"""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch as mock_patch

from app.memory import local_memory
from app.services import patches
from app.services.command_dispatcher import dispatch_input


def _unique(prefix):
    """Each test gets a fresh session id so state doesn't leak from
    earlier pytest runs (the conftest tempdir is reused across runs
    in the same shell)."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ============================================================
# Patch dataclass + validators
# ============================================================

class PatchDataclassTests(unittest.TestCase):
    def test_default_tier_is_l2(self):
        p = patches.Patch(kind=patches.PATCH_WORKSPACE_WRITE)
        self.assertEqual(p.tier, patches.TIER_L2)

    def test_l3_tier_inferred_for_system_prompt_update(self):
        # Caller passes default L2; init promotes to L3 for the
        # dangerous kind.
        p = patches.Patch(kind=patches.PATCH_SYSTEM_PROMPT_UPDATE)
        self.assertEqual(p.tier, patches.TIER_L3)

    def test_proposed_at_auto_filled(self):
        p = patches.Patch(kind=patches.PATCH_WORKSPACE_WRITE)
        self.assertTrue(p.proposed_at)
        self.assertIn("T", p.proposed_at)  # ISO format

    def test_to_dict_round_trip(self):
        original = patches.Patch(
            kind=patches.PATCH_WORKSPACE_WRITE,
            payload={"workspace_path": "/ws", "path": "x.py", "content": "hi"},
            rationale="test",
            tier=patches.TIER_L2,
            origin="chat",
        )
        clone = patches.Patch.from_dict(original.to_dict())
        self.assertEqual(clone.kind, original.kind)
        self.assertEqual(clone.payload, original.payload)
        self.assertEqual(clone.tier, original.tier)
        self.assertEqual(clone.rationale, original.rationale)

    def test_bundle_round_trip_preserves_sub_patches(self):
        sub = patches.Patch(
            kind=patches.PATCH_WORKSPACE_WRITE,
            payload={"workspace_path": "/ws", "path": "x.py"},
        )
        bundle = patches.Patch(kind=patches.PATCH_BUNDLE, sub_patches=[sub])
        clone = patches.Patch.from_dict(bundle.to_dict())
        self.assertIsNotNone(clone.sub_patches)
        self.assertEqual(len(clone.sub_patches), 1)
        self.assertEqual(clone.sub_patches[0].kind, patches.PATCH_WORKSPACE_WRITE)


class ValidatePatchTests(unittest.TestCase):
    def test_unknown_kind_rejected(self):
        p = patches.Patch(kind="bogus")
        self.assertEqual(patches.validate_patch(p), "unknown_patch_kind")

    def test_workspace_write_requires_workspace_path(self):
        p = patches.Patch(
            kind=patches.PATCH_WORKSPACE_WRITE,
            payload={"path": "x.py"},
        )
        self.assertEqual(patches.validate_patch(p), "no_workspace")

    def test_workspace_write_requires_path(self):
        p = patches.Patch(
            kind=patches.PATCH_WORKSPACE_WRITE,
            payload={"workspace_path": "/ws"},
        )
        self.assertEqual(patches.validate_patch(p), "invalid_argument")

    def test_workspace_write_minimal_valid(self):
        p = patches.Patch(
            kind=patches.PATCH_WORKSPACE_WRITE,
            payload={"workspace_path": "/ws", "path": "x.py"},
        )
        self.assertIsNone(patches.validate_patch(p))

    def test_empty_bundle_rejected(self):
        p = patches.Patch(kind=patches.PATCH_BUNDLE, sub_patches=[])
        self.assertEqual(patches.validate_patch(p), "invalid_argument")

    def test_oversize_bundle_rejected(self):
        sub = patches.Patch(
            kind=patches.PATCH_WORKSPACE_WRITE,
            payload={"workspace_path": "/ws", "path": "x.py"},
        )
        big = patches.Patch(
            kind=patches.PATCH_BUNDLE,
            sub_patches=[sub] * (patches.BUNDLE_MAX_SUB_PATCHES + 1),
        )
        self.assertEqual(patches.validate_patch(big), "bundle_too_large")

    def test_nested_bundle_rejected(self):
        inner = patches.Patch(kind=patches.PATCH_BUNDLE, sub_patches=[])
        outer = patches.Patch(kind=patches.PATCH_BUNDLE, sub_patches=[inner])
        # Nested bundles are flagged before we even validate inner.
        self.assertEqual(patches.validate_patch(outer), "invalid_argument")


class BundleFromActionDictsTests(unittest.TestCase):
    def test_returns_none_for_empty_list(self):
        self.assertIsNone(patches.bundle_from_action_dicts([]))

    def test_returns_none_for_single_action(self):
        # Single action → caller should keep using the legacy single-
        # write path; bundle is N>1 only.
        actions = [{
            "kind": "write", "workspace_path": "/ws", "path": "x.py",
            "content": "hi",
        }]
        self.assertIsNone(patches.bundle_from_action_dicts(actions))

    def test_builds_bundle_for_n_gt_1(self):
        actions = [
            {"kind": "write", "workspace_path": "/ws", "path": "a.py", "content": "x"},
            {"kind": "write", "workspace_path": "/ws", "path": "b.py", "content": "y"},
        ]
        bundle = patches.bundle_from_action_dicts(actions, rationale="r")
        self.assertEqual(bundle["kind"], "bundle")
        self.assertEqual(len(bundle["sub_actions"]), 2)
        self.assertEqual(bundle["workspace_path"], "/ws")
        self.assertEqual(bundle["rationale"], "r")

    def test_returns_none_when_workspaces_disagree(self):
        actions = [
            {"kind": "write", "workspace_path": "/ws-a", "path": "x"},
            {"kind": "write", "workspace_path": "/ws-b", "path": "y"},
        ]
        self.assertIsNone(patches.bundle_from_action_dicts(actions))

    def test_returns_none_when_oversize(self):
        actions = [
            {"kind": "write", "workspace_path": "/ws", "path": f"f{i}.py"}
            for i in range(patches.BUNDLE_MAX_SUB_PATCHES + 1)
        ]
        self.assertIsNone(patches.bundle_from_action_dicts(actions))


class DescribeBundleSummaryTests(unittest.TestCase):
    def test_short_bundle_lists_all_paths(self):
        action = {
            "kind": "bundle",
            "sub_actions": [
                {"kind": "write", "path": "a.py"},
                {"kind": "write", "path": "b.py"},
            ],
        }
        summary = patches.describe_bundle_summary(action)
        self.assertIn("2 file change", summary)
        self.assertIn("a.py", summary)
        self.assertIn("b.py", summary)

    def test_long_bundle_truncates_with_more_marker(self):
        action = {
            "kind": "bundle",
            "sub_actions": [
                {"kind": "write", "path": f"f{i}.py"} for i in range(6)
            ],
        }
        summary = patches.describe_bundle_summary(action)
        self.assertIn("6 file change", summary)
        self.assertIn("+3 more", summary)


# ============================================================
# Storage normalisation
# ============================================================

class BundleNormalisationTests(unittest.TestCase):
    def test_set_pending_approval_round_trips_bundle(self):
        sid = "bundle_norm_roundtrip"
        with tempfile.TemporaryDirectory() as ws:
            local_memory.set_workspace(session_id=sid, workspace_path=ws)
            bundle = {
                "kind": "bundle",
                "origin": "chat",
                "workspace_path": ws,
                "rationale": "r",
                "sub_actions": [
                    {"kind": "write", "workspace_path": ws, "path": "a.py", "content": "x"},
                    {"kind": "write", "workspace_path": ws, "path": "b.py", "content": "y"},
                ],
            }
            local_memory.set_pending_approval(session_id=sid, pending_approval=bundle)
            stored = local_memory.get_pending_approval(session_id=sid)
            self.assertEqual(stored["kind"], "bundle")
            self.assertEqual(len(stored["sub_actions"]), 2)
            self.assertEqual(stored["workspace_path"], ws)

    def test_bundle_with_oversize_sub_actions_rejected(self):
        sid = "bundle_norm_oversize"
        with tempfile.TemporaryDirectory() as ws:
            local_memory.set_workspace(session_id=sid, workspace_path=ws)
            bundle = {
                "kind": "bundle",
                "workspace_path": ws,
                "sub_actions": [
                    {"kind": "write", "workspace_path": ws, "path": f"f{i}.py", "content": "x"}
                    for i in range(11)
                ],
            }
            local_memory.set_pending_approval(session_id=sid, pending_approval=bundle)
            stored = local_memory.get_pending_approval(session_id=sid)
            # Normaliser refused → no pending approval set.
            self.assertIsNone(stored)

    def test_bundle_with_mixed_kinds_rejects_non_write_delete(self):
        sid = "bundle_norm_mixed"
        with tempfile.TemporaryDirectory() as ws:
            local_memory.set_workspace(session_id=sid, workspace_path=ws)
            bundle = {
                "kind": "bundle",
                "workspace_path": ws,
                "sub_actions": [
                    {"kind": "write", "workspace_path": ws, "path": "a.py", "content": "x"},
                    {"kind": "read", "workspace_path": ws, "paths": ["b.py"]},
                ],
            }
            local_memory.set_pending_approval(session_id=sid, pending_approval=bundle)
            stored = local_memory.get_pending_approval(session_id=sid)
            self.assertIsNone(stored)


# ============================================================
# Chat-flow emits bundle for N>1 candidates
# ============================================================

class ChatFlowBundleEmissionTests(unittest.TestCase):
    """``_maybe_chat_write_proposal`` returns a single ``write`` for
    one fenced block but a ``bundle`` for two-or-more."""

    def test_single_block_returns_single_write(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        with tempfile.TemporaryDirectory() as ws:
            response = (
                "Here's the change:\n\n"
                "```python file=foo.py\n"
                "x = 1\n"
                "```\n"
            )
            action = _maybe_chat_write_proposal(
                message="please update foo",
                response_text=response,
                session_id="single_block_test",
                session_context={"mode": "build", "workspace_path": ws},
            )
            self.assertIsNotNone(action)
            self.assertEqual(action["kind"], "write")
            self.assertEqual(action["path"], "foo.py")

    def test_two_blocks_returns_bundle(self):
        from app.services.chat_service import _maybe_chat_write_proposal
        with tempfile.TemporaryDirectory() as ws:
            response = (
                "Here are the changes:\n\n"
                "```python file=a.py\n"
                "import os\n"
                "```\n\n"
                "```python file=b.py\n"
                "print('hi')\n"
                "```\n"
            )
            action = _maybe_chat_write_proposal(
                message="please update both",
                response_text=response,
                session_id="two_block_test",
                session_context={"mode": "build", "workspace_path": ws},
            )
            self.assertIsNotNone(action)
            self.assertEqual(action["kind"], "bundle")
            self.assertEqual(len(action["sub_actions"]), 2)
            paths = [s["path"] for s in action["sub_actions"]]
            self.assertEqual(paths, ["a.py", "b.py"])


# ============================================================
# End-to-end approve + atomic rollback
# ============================================================

class BundleApproveEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _build_pending_bundle(self, sid, files):
        """Helper: create a 3-write bundle and stage it as pending."""
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        local_memory.set_mode(session_id=sid, mode="build")
        actions = [
            {"kind": "write", "origin": "chat", "workspace_path": self.workspace,
             "path": fname, "content": content, "create": True}
            for fname, content in files
        ]
        bundle = patches.bundle_from_action_dicts(actions, rationale="test")
        local_memory.set_pending_approval(session_id=sid, pending_approval=bundle)
        return bundle

    def test_approve_applies_all_sub_actions(self):
        sid = _unique("bundle_e2e_apply")
        self._build_pending_bundle(sid, [
            ("a.py", "A"),
            ("sub/b.py", "B"),
            ("c.py", "C"),
        ])
        result = dispatch_input("approve", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertEqual(result["result"]["count"], 3)
        for fname in ("a.py", "sub/b.py", "c.py"):
            with open(os.path.join(self.workspace, fname)) as fh:
                self.assertTrue(fh.read())  # content present

    def test_approve_records_a_backup_per_sub_action(self):
        sid = _unique("bundle_e2e_backups")
        self._build_pending_bundle(sid, [
            ("x.py", "1"), ("y.py", "2"), ("z.py", "3"),
        ])
        before = len(local_memory.get_write_backups(sid) or [])
        dispatch_input("approve", session_id=sid)
        after = len(local_memory.get_write_backups(sid) or [])
        self.assertEqual(after - before, 3)

    def test_atomic_rollback_when_apply_fails_midway(self):
        """Inject a failure on the 2nd sub-action; confirm the 1st
        is rolled back from disk."""
        sid = _unique("bundle_e2e_rollback")
        self._build_pending_bundle(sid, [
            ("first.py", "ok"),
            ("second.py", "will_fail"),
            ("third.py", "should_never_reach"),
        ])

        # Patch apply_workspace_write so the SECOND call fails.
        from app.services import command_dispatcher as cd
        real_apply = cd.apply_workspace_write
        call_count = {"n": 0}

        def flaky_apply(absolute_path, content):
            call_count["n"] += 1
            if call_count["n"] == 2:
                return {"ok": False, "error": "simulated_disk_full"}
            return real_apply(absolute_path, content)

        with mock_patch.object(cd, "apply_workspace_write", side_effect=flaky_apply):
            result = dispatch_input("approve", session_id=sid)

        self.assertIn("reason", result)
        self.assertIn("Bundle failed at sub-action 2/3", result["output"])
        self.assertIn("Rolled back 1/1", result["output"])
        # First file must be GONE (rolled back); second never wrote;
        # third never attempted.
        self.assertFalse(
            os.path.exists(os.path.join(self.workspace, "first.py")),
            "first.py was not rolled back",
        )
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "second.py")))
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "third.py")))

    def test_deny_clears_pending_action(self):
        sid = _unique("bundle_e2e_deny")
        self._build_pending_bundle(sid, [("x.py", "1"), ("y.py", "2")])
        self.assertIsNotNone(local_memory.get_pending_approval(session_id=sid))
        result = dispatch_input("deny", session_id=sid)
        self.assertEqual(result["action"], "deny")
        self.assertIsNone(local_memory.get_pending_approval(session_id=sid))

    def test_undo_after_bundle_rolls_back_one_sub_action_per_call(self):
        """Each sub-action records its own backup, so a single
        ``undo`` rolls back the LAST sub-action; users repeat for
        the rest. This is intentional — the bundle is atomic at
        APPLY time but stacked at the backup level."""
        sid = _unique("bundle_e2e_undo")
        self._build_pending_bundle(sid, [
            ("u1.py", "1"), ("u2.py", "2"), ("u3.py", "3"),
        ])
        dispatch_input("approve", session_id=sid)
        for fname in ("u1.py", "u2.py", "u3.py"):
            self.assertTrue(os.path.exists(os.path.join(self.workspace, fname)))

        # First undo should remove u3 (last applied).
        dispatch_input("undo", session_id=sid)
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "u3.py")))
        self.assertTrue(os.path.exists(os.path.join(self.workspace, "u2.py")))

        # Two more undos remove the rest.
        dispatch_input("undo", session_id=sid)
        dispatch_input("undo", session_id=sid)
        for fname in ("u1.py", "u2.py", "u3.py"):
            self.assertFalse(os.path.exists(os.path.join(self.workspace, fname)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
