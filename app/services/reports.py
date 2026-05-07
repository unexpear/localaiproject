"""Pipeline reports — typed dataclasses summarising long-running
operations. Adopted from
``local_ai_workbench/shared/schemas.py``'s pattern (the user's
other project surveyed on 2026-05-06).

The keystone idea: every long-running stage emits a typed report
with counts (total / done / failed / skipped), file paths touched,
per-failure error entries, and a one-line human summary. Callers
can pattern-match on the typed shape without parsing English.

Phase 1 ships:
* ``StageReport`` dataclass + ``to_dict`` / ``from_dict`` /
  ``one_line_summary`` helpers.
* ``cowork_report_from_run(run)`` — converts a stored cowork run
  dict into a ``StageReport``. Counts come from the per-sub-task
  status entries in ``subtask_results``; paths from
  ``git_state.commits``; errors from any non-``done`` sub-task.
* ``format_report(report)`` — multi-line text rendering for the
  dispatcher's ``cowork report`` command.

Future phases can add ``template_report_from_run`` (workflow
templates) and ``recall_report_from_results`` (search hits)
without changing the dataclass.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ============================================================
# Dataclass
# ============================================================

@dataclass
class StageReport:
    """One typed report. Every field is JSON-safe so the dataclass
    can round-trip through MCP / HTTP without custom encoders."""

    name: str = ""
    status: str = ""               # canonical run status
    started_at: str = ""           # ISO timestamp; "" when unknown
    finished_at: str = ""          # ISO timestamp; "" when not yet finished
    duration_seconds: float = 0.0
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    paths: List[str] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "StageReport":
        data = data or {}
        return cls(
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            total=int(data.get("total") or 0),
            done=int(data.get("done") or 0),
            failed=int(data.get("failed") or 0),
            skipped=int(data.get("skipped") or 0),
            paths=list(data.get("paths") or []),
            errors=list(data.get("errors") or []),
            summary=str(data.get("summary") or ""),
            extra=dict(data.get("extra") or {}),
        )

    def one_line_summary(self) -> str:
        """Compact one-line summary for footer / log lines.

        Format: ``<name>: status=<x> done=<n>/<total> failed=<n> skipped=<n> [<duration>s]``
        """
        bits = [self.name or "(unnamed)"]
        bits.append(f"status={self.status or '?'}")
        if self.total:
            bits.append(f"done={self.done}/{self.total}")
        if self.failed:
            bits.append(f"failed={self.failed}")
        if self.skipped:
            bits.append(f"skipped={self.skipped}")
        if self.duration_seconds:
            bits.append(f"{self.duration_seconds:.1f}s")
        return " · ".join(bits)


# ============================================================
# Internals
# ============================================================

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Forgiving ISO-8601 parser. Returns None on anything we can't
    interpret. Accepts the trailing-Z form ``local_memory._now()``
    emits."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _duration(start: Optional[str], end: Optional[str]) -> float:
    """Compute (end - start) in seconds, or 0.0 if either is
    missing / malformed."""
    a = _parse_iso(start)
    b = _parse_iso(end)
    if a is None or b is None:
        return 0.0
    delta = b - a
    return max(0.0, delta.total_seconds())


def _truncate(text: str, n: int = 200) -> str:
    text = str(text or "").replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 3].rstrip() + "..."


# ============================================================
# CoWork run -> StageReport
# ============================================================

def cowork_report_from_run(run: Optional[Dict[str, Any]]) -> StageReport:
    """Convert a stored cowork run dict (from
    ``local_memory.get_cowork_run`` / ``add_cowork_run``) into a
    ``StageReport``. Tolerates partial / malformed runs — missing
    fields default to zero / empty.

    See ``docs/design/cowork-mode.md`` for the run lifecycle and
    ``docs/design/patch-taxonomy.md`` for the report adoption
    rationale.
    """
    if not isinstance(run, dict):
        return StageReport(name="(no run)", status="unknown")

    run_id = str(run.get("id") or "")
    goal = str(run.get("goal") or "")
    name = f"cowork run {run_id}" if run_id else "cowork run"
    if goal:
        name = f"{name} — {_truncate(goal, 120)}"

    plan = run.get("plan") or []
    if not isinstance(plan, list):
        plan = []
    plan_by_id = {
        str(entry.get("id") or ""): entry
        for entry in plan
        if isinstance(entry, dict)
    }
    total = len(plan)

    subtask_results = run.get("subtask_results") or {}
    if not isinstance(subtask_results, dict):
        subtask_results = {}

    done = failed = skipped = 0
    errors: List[Dict[str, Any]] = []
    for sub_id, entry in subtask_results.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        if status == "done":
            done += 1
            continue
        if status == "skipped":
            skipped += 1
        elif status == "failed":
            failed += 1
        # both skipped and failed contribute an error record so the
        # report's `errors` list is the full list of "things that
        # didn't go to plan."
        plan_entry = plan_by_id.get(str(sub_id), {})
        errors.append({
            "sub_id": str(sub_id),
            "builder": str(plan_entry.get("builder") or ""),
            "task": _truncate(plan_entry.get("task") or "", 120),
            "status": status,
            "reason": _truncate(
                entry.get("hook_reason") or entry.get("result") or "", 200
            ),
        })

    # Paths: from git_state commits (Phase 5 branch strategies). We
    # dedupe but preserve commit order for readability.
    paths: List[str] = []
    seen_paths = set()
    git_state = run.get("git_state") or {}
    if isinstance(git_state, dict):
        commits = git_state.get("commits") or []
        if isinstance(commits, list):
            for commit in commits:
                if not isinstance(commit, dict):
                    continue
                path = commit.get("path")
                if not path or not isinstance(path, str):
                    continue
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                paths.append(path)

    started_at = run.get("approved_at") or run.get("created_at") or ""
    finished_at = run.get("completed_at") or ""
    duration = _duration(started_at, finished_at)

    summary = _truncate(run.get("synthesis") or "", 200)
    if not summary:
        # Fall back to a count-based summary so the report is never
        # blank for a run that ended without synthesis (cancelled
        # mid-flight, all sub-tasks failed, etc.).
        bits = [f"{done}/{total} done"]
        if failed:
            bits.append(f"{failed} failed")
        if skipped:
            bits.append(f"{skipped} skipped")
        summary = ", ".join(bits)

    extra: Dict[str, Any] = {
        "run_id": run_id,
        "session_id": str(run.get("session_id") or ""),
        "goal": goal,
        "branch_strategy": run.get("branch_strategy"),
        "branch_name": run.get("branch_name"),
        "git_state": git_state,
        "synthesis_preview": summary,
        "deliverables_count": len(run.get("deliverables") or []),
    }

    return StageReport(
        name=name,
        status=str(run.get("status") or ""),
        started_at=str(started_at or ""),
        finished_at=str(finished_at or ""),
        duration_seconds=duration,
        total=total,
        done=done,
        failed=failed,
        skipped=skipped,
        paths=paths,
        errors=errors,
        summary=summary,
        extra=extra,
    )


# ============================================================
# Renderer
# ============================================================

def format_report(report: StageReport) -> str:
    """Multi-line human-readable rendering of a StageReport for the
    dispatcher's ``cowork report`` output. ASCII-only so the
    Windows console doesn't choke."""
    if not isinstance(report, StageReport):
        return "(no report)"
    lines: List[str] = []
    lines.append(report.name or "(unnamed report)")
    lines.append("-" * min(80, len(report.name) or 12))
    lines.append(f"Status: {report.status or '?'}")
    if report.started_at or report.finished_at or report.duration_seconds:
        timing = f"  started: {report.started_at or '?'}"
        if report.finished_at:
            timing += f"\n  finished: {report.finished_at}"
        if report.duration_seconds:
            timing += f"\n  duration: {report.duration_seconds:.1f}s"
        lines.append(timing)
    if report.total:
        bits = [f"{report.done}/{report.total} done"]
        if report.failed:
            bits.append(f"{report.failed} failed")
        if report.skipped:
            bits.append(f"{report.skipped} skipped")
        lines.append("Sub-tasks: " + ", ".join(bits))

    if report.paths:
        lines.append("")
        lines.append(f"Files touched ({len(report.paths)}):")
        for path in report.paths[:20]:
            lines.append(f"  - {path}")
        if len(report.paths) > 20:
            lines.append(f"  ... +{len(report.paths) - 20} more")

    if report.errors:
        lines.append("")
        lines.append(f"Errors / skipped ({len(report.errors)}):")
        for err in report.errors:
            sub = err.get("sub_id") or "?"
            builder = err.get("builder") or ""
            status = err.get("status") or "?"
            reason = err.get("reason") or ""
            head = f"  [{status}] {sub}"
            if builder:
                head += f" (builder: {builder})"
            lines.append(head)
            if reason:
                lines.append(f"      {reason}")

    if report.summary:
        lines.append("")
        lines.append(f"Summary: {report.summary}")

    return "\n".join(lines)
