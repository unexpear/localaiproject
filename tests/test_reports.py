"""Tests for ``app/services/reports.py`` (typed pipeline reports
adopted from `local_ai_workbench/shared/schemas.py`).

Covers:
* ``StageReport`` dataclass round-trip via ``to_dict`` / ``from_dict``
* ``one_line_summary`` rendering
* ``cowork_report_from_run`` over realistic run shapes:
  - happy path (all sub-tasks done)
  - mixed (some done, some failed, some skipped)
  - cancelled mid-flight (no synthesis, count fallback summary)
  - empty / malformed input (defensive defaults)
* ``format_report`` multi-line rendering
* Dispatcher: ``cowork report [<run_id>]`` end-to-end
"""
import unittest
import uuid

from app.memory import local_memory
from app.services import reports
from app.services.command_dispatcher import dispatch_input


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ============================================================
# StageReport dataclass
# ============================================================

class StageReportDataclassTests(unittest.TestCase):
    def test_default_values(self):
        r = reports.StageReport()
        self.assertEqual(r.name, "")
        self.assertEqual(r.status, "")
        self.assertEqual(r.total, 0)
        self.assertEqual(r.paths, [])
        self.assertEqual(r.errors, [])
        self.assertEqual(r.extra, {})

    def test_to_dict_includes_all_fields(self):
        r = reports.StageReport(
            name="x", status="done", total=3, done=3,
            paths=["a.py"], extra={"k": 1},
        )
        d = r.to_dict()
        for key in (
            "name", "status", "started_at", "finished_at",
            "duration_seconds", "total", "done", "failed",
            "skipped", "paths", "errors", "summary", "extra",
        ):
            self.assertIn(key, d)

    def test_from_dict_roundtrip(self):
        original = reports.StageReport(
            name="x", status="done", duration_seconds=12.5,
            total=5, done=4, failed=1,
            paths=["a", "b"], errors=[{"sub_id": "s1", "reason": "x"}],
            extra={"branch": "feat/x"},
        )
        clone = reports.StageReport.from_dict(original.to_dict())
        self.assertEqual(clone, original)

    def test_from_dict_tolerates_partial_input(self):
        r = reports.StageReport.from_dict({"name": "y", "status": "x"})
        self.assertEqual(r.name, "y")
        self.assertEqual(r.total, 0)
        self.assertEqual(r.paths, [])
        self.assertEqual(r.errors, [])

    def test_from_dict_handles_none(self):
        r = reports.StageReport.from_dict(None)
        self.assertIsInstance(r, reports.StageReport)
        self.assertEqual(r.name, "")

    def test_one_line_summary_basic(self):
        r = reports.StageReport(
            name="cowork run cw_x", status="done",
            total=3, done=3, duration_seconds=42.5,
        )
        line = r.one_line_summary()
        self.assertIn("cowork run cw_x", line)
        self.assertIn("status=done", line)
        self.assertIn("done=3/3", line)
        self.assertIn("42.5s", line)

    def test_one_line_summary_omits_zero_counts(self):
        r = reports.StageReport(name="x", status="done", total=2, done=2)
        line = r.one_line_summary()
        self.assertNotIn("failed=", line)
        self.assertNotIn("skipped=", line)


# ============================================================
# cowork_report_from_run
# ============================================================

class CoworkReportFromRunTests(unittest.TestCase):
    def _run(self, **fields):
        base = {
            "id": "cw_test1",
            "session_id": "s1",
            "goal": "test goal",
            "status": "done",
            "created_at": "2026-05-06T10:00:00Z",
            "approved_at": "2026-05-06T10:01:00Z",
            "completed_at": "2026-05-06T10:05:00Z",
            "plan": [
                {"id": "s1", "builder": "coder", "task": "do thing 1"},
                {"id": "s2", "builder": "coder", "task": "do thing 2"},
            ],
            "subtask_results": {
                "s1": {"status": "done", "result": "ok"},
                "s2": {"status": "done", "result": "ok"},
            },
            "git_state": {},
            "synthesis": "all done",
            "branch_strategy": "head",
            "branch_name": "",
            "deliverables": [],
        }
        base.update(fields)
        return base

    def test_happy_path(self):
        report = reports.cowork_report_from_run(self._run())
        self.assertEqual(report.status, "done")
        self.assertEqual(report.total, 2)
        self.assertEqual(report.done, 2)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.errors, [])
        self.assertIn("test goal", report.name)
        self.assertEqual(report.summary, "all done")
        self.assertEqual(report.duration_seconds, 240.0)

    def test_mixed_outcomes_count_correctly(self):
        run = self._run(
            subtask_results={
                "s1": {"status": "done", "result": "ok"},
                "s2": {"status": "failed", "result": "boom",
                       "hook_reason": ""},
            },
        )
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.done, 1)
        self.assertEqual(report.failed, 1)
        self.assertEqual(len(report.errors), 1)
        err = report.errors[0]
        self.assertEqual(err["sub_id"], "s2")
        self.assertEqual(err["builder"], "coder")
        self.assertEqual(err["status"], "failed")
        self.assertIn("boom", err["reason"])

    def test_skipped_sub_task_records_an_error_entry(self):
        run = self._run(
            subtask_results={
                "s1": {"status": "done", "result": "ok"},
                "s2": {
                    "status": "skipped",
                    "result": "skipped — upstream dependency failed",
                },
            },
        )
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0]["status"], "skipped")

    def test_cancelled_run_with_no_synthesis_falls_back_to_count_summary(self):
        run = self._run(
            status="cancelled", synthesis="",
            subtask_results={
                "s1": {"status": "done", "result": "ok"},
                "s2": {
                    "status": "skipped",
                    "result": "skipped — run cancelled",
                },
            },
        )
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.status, "cancelled")
        self.assertIn("1/2 done", report.summary)
        self.assertIn("1 skipped", report.summary)

    def test_paths_from_git_state_commits_dedup_preserving_order(self):
        run = self._run(
            git_state={
                "strategy": "merge-to-head",
                "commits": [
                    {"path": "a.py", "kind": "write"},
                    {"path": "b.py", "kind": "write"},
                    {"path": "a.py", "kind": "write"},  # duplicate
                ],
            },
        )
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.paths, ["a.py", "b.py"])

    def test_extra_carries_branch_strategy_and_synthesis_preview(self):
        run = self._run(
            branch_strategy="merge-to-head",
            branch_name="feat/x",
            synthesis="x" * 500,  # over 200 chars
        )
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.extra["branch_strategy"], "merge-to-head")
        self.assertEqual(report.extra["branch_name"], "feat/x")
        self.assertLessEqual(len(report.extra["synthesis_preview"]), 200)

    def test_handles_none_input_gracefully(self):
        report = reports.cowork_report_from_run(None)
        self.assertEqual(report.status, "unknown")
        self.assertEqual(report.total, 0)

    def test_handles_run_without_plan(self):
        run = self._run(plan=[])
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.total, 0)

    def test_handles_malformed_subtask_results(self):
        run = self._run(subtask_results="not a dict")
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.done, 0)
        self.assertEqual(report.failed, 0)

    def test_no_completed_at_means_zero_duration(self):
        run = self._run(completed_at="")
        report = reports.cowork_report_from_run(run)
        self.assertEqual(report.duration_seconds, 0.0)
        self.assertEqual(report.finished_at, "")


# ============================================================
# format_report
# ============================================================

class FormatReportTests(unittest.TestCase):
    def test_renders_name_status_and_counts(self):
        r = reports.StageReport(
            name="cowork run cw_x", status="done",
            total=3, done=2, failed=1,
        )
        text = reports.format_report(r)
        self.assertIn("cowork run cw_x", text)
        self.assertIn("Status: done", text)
        self.assertIn("Sub-tasks: 2/3 done, 1 failed", text)

    def test_renders_paths_with_truncation_marker_when_many(self):
        r = reports.StageReport(
            name="x", status="done",
            paths=[f"file{i}.py" for i in range(25)],
        )
        text = reports.format_report(r)
        self.assertIn("Files touched (25)", text)
        self.assertIn("file0.py", text)
        self.assertIn("+5 more", text)

    def test_renders_errors_section(self):
        r = reports.StageReport(
            name="x", status="failed",
            total=2, done=1, failed=1,
            errors=[{
                "sub_id": "s2", "builder": "reviewer",
                "status": "failed", "reason": "lint check failed",
            }],
        )
        text = reports.format_report(r)
        self.assertIn("Errors / skipped (1)", text)
        self.assertIn("[failed] s2 (builder: reviewer)", text)
        self.assertIn("lint check failed", text)

    def test_handles_none_input(self):
        self.assertIn("no report", reports.format_report(None))


# ============================================================
# Dispatcher: `cowork report [<run_id>]`
# ============================================================

class CoworkReportDispatcherTests(unittest.TestCase):
    def _seed_run(self, sid):
        run = local_memory.add_cowork_run(
            session_id=sid, goal="test goal",
            plan=[{"id": "s1", "builder": "b1", "task": "t1", "depends_on": []}],
        )
        local_memory.update_cowork_run(
            run["id"],
            status="done",
            completed_at="2026-05-06T10:05:00Z",
            subtask_results={"s1": {"status": "done", "result": "ok"}},
            synthesis="finished cleanly",
        )
        return run["id"]

    def test_report_with_explicit_run_id(self):
        sid = _unique("cowork_report_explicit")
        run_id = self._seed_run(sid)
        result = dispatch_input(f"cowork report {run_id}", session_id=sid)
        self.assertEqual(result["action"], "cowork")
        self.assertNotIn("reason", result)
        self.assertIn(run_id, result["output"])
        self.assertIn("Status: done", result["output"])
        self.assertEqual(result["result"]["total"], 1)
        self.assertEqual(result["result"]["done"], 1)

    def test_report_unknown_id_returns_unknown_target(self):
        sid = _unique("cowork_report_unknown")
        result = dispatch_input("cowork report cw_does_not_exist", session_id=sid)
        self.assertEqual(result.get("reason"), "unknown_target")

    def test_report_no_args_falls_back_to_most_recent_run(self):
        sid = _unique("cowork_report_recent")
        run_id = self._seed_run(sid)
        result = dispatch_input("cowork report", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertEqual(result["result"]["extra"]["run_id"], run_id)

    def test_report_no_runs_returns_invalid_argument(self):
        sid = _unique("cowork_report_empty")
        result = dispatch_input("cowork report", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
