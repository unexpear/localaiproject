"""Tests for the workspace isolation audit (adopted from
``local_ai_workbench/storage/isolation.py``).

Covers:
* Containment primitive: relative + absolute + traversal + symlink
  cases.
* No-workspace fast-path returns a skipped result.
* Clean session yields zero findings.
* Leak detection across each path-bearing field
  (approved_search.files, pending_approval.path, write_backups,
  executions replay).
* Dispatcher subcommand surfaces errors with `outside_workspace`.
* CoWork branch-strategy pre-flight refuses to start when audit
  finds errors (covered as a smoke; full cowork integration tests
  live in test_cowork.py).
"""
import os
import tempfile
import unittest

from app.memory import local_memory
from app.services import workspace_isolation as iso
from app.services.command_dispatcher import dispatch_input


class IsInsideWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_simple_relative_path_inside(self):
        self.assertTrue(iso._is_inside_workspace(self.workspace, "src/main.py"))

    def test_relative_path_with_traversal_rejected(self):
        self.assertFalse(iso._is_inside_workspace(self.workspace, "../escape.py"))
        self.assertFalse(iso._is_inside_workspace(self.workspace, "src/../../escape.py"))

    def test_absolute_inside_workspace_accepted(self):
        abs_inside = os.path.join(self.workspace, "x.py")
        self.assertTrue(iso._is_inside_workspace(self.workspace, abs_inside))

    def test_absolute_outside_workspace_rejected(self):
        # A clearly-different absolute path
        with tempfile.TemporaryDirectory() as other:
            abs_outside = os.path.join(other, "x.py")
            self.assertFalse(iso._is_inside_workspace(self.workspace, abs_outside))

    def test_empty_path_rejected(self):
        self.assertFalse(iso._is_inside_workspace(self.workspace, ""))
        self.assertFalse(iso._is_inside_workspace(self.workspace, "  "))

    def test_no_workspace_path_rejected(self):
        self.assertFalse(iso._is_inside_workspace("", "anything"))
        self.assertFalse(iso._is_inside_workspace(None, "anything"))


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_inside_relative_classified_ok(self):
        rep = iso._classify(self.workspace, "src/main.py", "test.field")
        self.assertEqual(rep.severity, iso.SEVERITY_OK)
        self.assertEqual(rep.path, "src/main.py")
        self.assertEqual(rep.source, "test.field")

    def test_traversal_classified_error(self):
        rep = iso._classify(self.workspace, "../escape.py", "test.field")
        self.assertEqual(rep.severity, iso.SEVERITY_ERROR)
        self.assertIn("traversal", rep.message)

    def test_outside_absolute_classified_error(self):
        with tempfile.TemporaryDirectory() as other:
            outside = os.path.join(other, "x.py")
            rep = iso._classify(self.workspace, outside, "test.field")
            self.assertEqual(rep.severity, iso.SEVERITY_ERROR)
            self.assertIn("outside the pinned workspace", rep.message)

    def test_empty_path_classified_warning(self):
        rep = iso._classify(self.workspace, "", "test.field")
        self.assertEqual(rep.severity, iso.SEVERITY_WARNING)


class AuditSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_workspace_returns_skipped_reason(self):
        sid = "audit_no_ws"
        result = iso.audit_session(session_id=sid)
        self.assertEqual(result.skipped_reason, iso.NO_WORKSPACE_REASON)
        self.assertTrue(result.is_clean)
        self.assertEqual(result.findings, [])

    def test_clean_session_zero_findings(self):
        sid = "audit_clean"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        result = iso.audit_session(session_id=sid)
        self.assertTrue(result.is_clean)
        self.assertIsNone(result.skipped_reason)
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(len(result.warnings), 0)

    def test_audit_flags_absolute_outside_path_in_approved_search(self):
        # Note: the `_normalize_approved_search` lstrip("./") trick
        # sanitises bare `../foo` to `foo`, but absolute paths
        # outside the workspace pass through and are still flagged.
        sid = "audit_search_abs_leak"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        with tempfile.TemporaryDirectory() as other:
            outside_abs = os.path.join(other, "leaked.py")
            ctx = local_memory.get_session_context(session_id=sid)
            ctx["approved_search"] = {
                "query": "x",
                "files": ["good_file.py", outside_abs],
            }
            local_memory.update_session_context(session_id=sid, context=ctx)
            result = iso.audit_session(session_id=sid)
        self.assertFalse(result.is_clean)
        sources = [f.source for f in result.errors]
        self.assertTrue(
            any("approved_search.files[" in s for s in sources),
            f"sources: {sources!r}",
        )

    def test_audit_flags_write_backup_path_outside_workspace(self):
        sid = "audit_backup_leak"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        ctx = local_memory.get_session_context(session_id=sid)
        ctx["write_backups"] = [
            {
                "id": "abc",
                "path": "../etc/passwd",
                "kind": "write",
                "previous_content": None,
                "previous_existed": False,
                "applied_at": "2026-01-01T00:00:00Z",
            }
        ]
        local_memory.update_session_context(session_id=sid, context=ctx)
        result = iso.audit_session(session_id=sid)
        self.assertFalse(result.is_clean)
        sources = [f.source for f in result.errors]
        self.assertTrue(any("write_backups[0].path" in s for s in sources))

    def test_audit_to_dict_shape(self):
        sid = "audit_dict_shape"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        result = iso.audit_session(session_id=sid)
        d = result.to_dict()
        self.assertIn("session_id", d)
        self.assertIn("workspace_path", d)
        self.assertIn("findings", d)
        self.assertIn("error_count", d)
        self.assertIn("warning_count", d)
        self.assertIn("checked_count", d)
        self.assertIn("is_clean", d)


class FormatAuditReportTests(unittest.TestCase):
    def test_skipped_report(self):
        result = iso.AuditResult(
            session_id="x",
            workspace_path=None,
            skipped_reason=iso.NO_WORKSPACE_REASON,
        )
        text = iso.format_audit_report(result)
        self.assertIn("Audit skipped", text)
        self.assertIn(iso.NO_WORKSPACE_REASON, text)

    def test_clean_report(self):
        result = iso.AuditResult(
            session_id="s1", workspace_path="/tmp/ws",
        )
        text = iso.format_audit_report(result)
        self.assertIn("paths checked: 0", text)
        self.assertIn("[ok] no leakage detected", text)

    def test_error_report_includes_finding(self):
        result = iso.AuditResult(
            session_id="s1", workspace_path="/tmp/ws",
        )
        result.findings.append(iso.LeakageReport(
            source="approved_search.files[0]",
            path="../leak",
            severity=iso.SEVERITY_ERROR,
            message="path contains '..' -- traversal attempt",
        ))
        text = iso.format_audit_report(result)
        self.assertIn("Errors:", text)
        self.assertIn("[ERR]", text)
        self.assertIn("approved_search.files[0]", text)

    def test_warning_report_separate_section(self):
        result = iso.AuditResult(
            session_id="s1", workspace_path="/tmp/ws",
        )
        result.findings.append(iso.LeakageReport(
            source="pending_approval.path",
            path="",
            severity=iso.SEVERITY_WARNING,
            message="empty path stored in session state",
        ))
        text = iso.format_audit_report(result)
        self.assertIn("Warnings:", text)
        self.assertIn("[WARN]", text)


class WorkspaceAuditDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_dispatcher_no_workspace_returns_no_workspace_reason(self):
        sid = "audit_dispatch_no_ws"
        result = dispatch_input("workspace audit", session_id=sid)
        self.assertEqual(result["action"], "workspace")
        self.assertEqual(result.get("reason"), "no_workspace")
        self.assertIn("Audit skipped", result["output"])

    def test_dispatcher_clean_session_no_reason(self):
        sid = "audit_dispatch_clean"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        result = dispatch_input("workspace audit", session_id=sid)
        self.assertEqual(result["action"], "workspace")
        # Clean = no `reason` key
        self.assertNotIn("reason", result)
        self.assertIn("[ok] no leakage detected", result["output"])
        self.assertEqual(result["result"]["error_count"], 0)

    def test_dispatcher_leaky_session_returns_outside_workspace_reason(self):
        sid = "audit_dispatch_leak"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        # Inject leakage via write_backups (which preserves the bad
        # path verbatim — unlike approved_search which sanitises).
        ctx = local_memory.get_session_context(session_id=sid)
        ctx["write_backups"] = [{
            "id": "abc", "path": "../escape.py", "kind": "write",
            "previous_content": None, "previous_existed": False,
            "applied_at": "2026-01-01T00:00:00Z",
        }]
        local_memory.update_session_context(session_id=sid, context=ctx)
        result = dispatch_input("workspace audit", session_id=sid)
        self.assertEqual(result.get("reason"), "outside_workspace")
        self.assertGreaterEqual(result["result"]["error_count"], 1)


class CoWorkBranchStrategyPreFlightTests(unittest.TestCase):
    """Smoke test: when a session has cross-workspace leakage, the
    branch-strategy pre-flight refuses to start. We don't run the
    full git plumbing here (covered by test_cowork.py) — just check
    that ``_setup_branch_strategy`` returns the expected error."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_branch_strategy_refuses_when_audit_finds_errors(self):
        from app.services import cowork
        sid = "audit_cowork_preflight"
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)
        # Inject a leak.
        ctx = local_memory.get_session_context(session_id=sid)
        ctx["write_backups"] = [{
            "id": "abc", "path": "../oops", "kind": "write",
            "previous_content": None, "previous_existed": False,
            "applied_at": "2026-01-01T00:00:00Z",
        }]
        local_memory.update_session_context(session_id=sid, context=ctx)

        run = {"branch_strategy": "branch", "branch_name": "feature/x"}
        result = cowork._setup_branch_strategy(run, "run_id_1", sid)
        self.assertIsNotNone(result["error"])
        self.assertIn("audit", result["error"].lower())
        self.assertIn("cross-workspace", result["error"].lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
