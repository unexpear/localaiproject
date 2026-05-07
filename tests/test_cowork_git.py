"""Tests for app.services.cowork_git — git plumbing for cowork
branch strategies (Phase 5).

The high-level helpers wrap a single `_run_git` subprocess call;
these tests patch `_run_git` to return canned `GitResult` values so
no real `git` process is spawned.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from app.services import cowork_git as g


def _ok(stdout="", stderr="", returncode=0, args=("git",)):
    return g.GitResult(
        ok=(returncode == 0), returncode=returncode,
        stdout=stdout, stderr=stderr, args=args,
    )


def _err(stderr="boom", returncode=1, args=("git",)):
    return g.GitResult(
        ok=False, returncode=returncode,
        stdout="", stderr=stderr, args=args,
    )


class IsGitRepoTests(unittest.TestCase):
    def setUp(self):
        # is_git_repo gates on os.path.isdir before invoking git, so
        # we need a real directory for the mocked _run_git to be reached.
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_true_when_git_says_true(self):
        with patch.object(g, "_run_git", return_value=_ok(stdout="true\n")):
            self.assertTrue(g.is_git_repo(self._tmp.name))

    def test_returns_false_when_git_returns_nonzero(self):
        with patch.object(g, "_run_git", return_value=_err()):
            self.assertFalse(g.is_git_repo(self._tmp.name))

    def test_returns_false_for_empty_path(self):
        # No subprocess call at all when the path is empty.
        self.assertFalse(g.is_git_repo(""))

    def test_returns_false_for_nonexistent_path(self):
        self.assertFalse(g.is_git_repo("/this/does/not/exist/anywhere"))


class CurrentBranchTests(unittest.TestCase):
    def test_returns_branch_name(self):
        with patch.object(g, "_run_git", return_value=_ok(stdout="main\n")):
            self.assertEqual(g.current_branch("/x"), "main")

    def test_returns_none_on_detached_head(self):
        with patch.object(g, "_run_git", return_value=_ok(stdout="HEAD\n")):
            self.assertIsNone(g.current_branch("/x"))

    def test_returns_none_on_error(self):
        with patch.object(g, "_run_git", return_value=_err()):
            self.assertIsNone(g.current_branch("/x"))


class BranchExistsTests(unittest.TestCase):
    def test_true_when_show_ref_succeeds(self):
        with patch.object(g, "_run_git", return_value=_ok()):
            self.assertTrue(g.branch_exists("/x", "feat/foo"))

    def test_false_when_show_ref_fails(self):
        with patch.object(g, "_run_git", return_value=_err()):
            self.assertFalse(g.branch_exists("/x", "ghost"))


class CreateAndCheckoutBranchTests(unittest.TestCase):
    def test_passes_b_flag_and_optional_base(self):
        captured = {}

        def fake(workspace, *args, timeout=30.0):
            captured["args"] = args
            return _ok()

        with patch.object(g, "_run_git", side_effect=fake):
            g.create_and_checkout_branch("/x", "feat/y", base="main")
        self.assertEqual(captured["args"], ("checkout", "-b", "feat/y", "main"))

    def test_omits_base_when_not_provided(self):
        captured = {}

        def fake(workspace, *args, timeout=30.0):
            captured["args"] = args
            return _ok()

        with patch.object(g, "_run_git", side_effect=fake):
            g.create_and_checkout_branch("/x", "feat/y")
        self.assertEqual(captured["args"], ("checkout", "-b", "feat/y"))


class CommitPathTests(unittest.TestCase):
    def test_runs_add_then_commit(self):
        calls = []

        def fake(workspace, *args, timeout=30.0):
            calls.append(args)
            return _ok()

        with patch.object(g, "_run_git", side_effect=fake):
            result = g.commit_path("/x", "src/foo.py", "msg")
        self.assertEqual(calls[0], ("add", "--", "src/foo.py"))
        self.assertEqual(calls[1], ("commit", "-m", "msg"))
        self.assertTrue(result.ok)

    def test_returns_early_if_add_fails(self):
        calls = []

        def fake(workspace, *args, timeout=30.0):
            calls.append(args)
            return _err(stderr="permission denied")

        with patch.object(g, "_run_git", side_effect=fake):
            result = g.commit_path("/x", "src/foo.py", "msg")
        # Only the add call was made.
        self.assertEqual(len(calls), 1)
        self.assertFalse(result.ok)


class MergeBranchTests(unittest.TestCase):
    def test_default_uses_no_ff(self):
        captured = {}

        def fake(workspace, *args, timeout=30.0):
            captured["args"] = args
            return _ok()

        with patch.object(g, "_run_git", side_effect=fake):
            g.merge_branch("/x", "feat/y", message="msg")
        self.assertEqual(
            captured["args"],
            ("merge", "--no-ff", "-m", "msg", "feat/y"),
        )

    def test_omits_no_ff_when_disabled(self):
        captured = {}

        def fake(workspace, *args, timeout=30.0):
            captured["args"] = args
            return _ok()

        with patch.object(g, "_run_git", side_effect=fake):
            g.merge_branch("/x", "feat/y", no_ff=False)
        self.assertEqual(captured["args"], ("merge", "feat/y"))


class HasUncommittedChangesTests(unittest.TestCase):
    def test_true_when_status_porcelain_has_output(self):
        with patch.object(g, "_run_git", return_value=_ok(stdout=" M src/x.py\n")):
            self.assertTrue(g.has_uncommitted_changes("/x"))

    def test_false_on_clean_tree(self):
        with patch.object(g, "_run_git", return_value=_ok(stdout="")):
            self.assertFalse(g.has_uncommitted_changes("/x"))


class AutoBranchNameTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            g.auto_branch_name("planner", "cw_abcd1234"),
            "lai/planner/cw_abcd1234",
        )


class GitResultTests(unittest.TestCase):
    def test_ok_attr_reflects_returncode(self):
        good = g.GitResult(ok=True, returncode=0, stdout="", stderr="", args=())
        self.assertTrue(good.ok)
        bad = g.GitResult(ok=False, returncode=1, stdout="", stderr="", args=())
        self.assertFalse(bad.ok)


if __name__ == "__main__":
    unittest.main()
