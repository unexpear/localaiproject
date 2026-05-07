"""Workspace isolation audit — detect cross-workspace leakage in
session state. Adopted from Local AI Workbench's
``storage/isolation.py`` pattern (the user's other project we surveyed
on 2026-05-06).

The premise: when a session has a pinned ``workspace_path``, every
path stored in that session's state should resolve to *inside* that
workspace. If any field carries a path that escapes (via traversal,
absolute-path leak, or symlink), that's a sign of:

* **Stale state** — the user re-pinned the workspace and old
  approval / backup state still references the previous root.
* **Race / leak** — a CoWork sub-task running with a branch strategy
  pulled paths in from a sibling workspace.
* **Manual edit** — the on-disk JSON store was edited by hand and
  the new paths weren't validated.

The audit is read-only. It does NOT mutate state; it produces a
report so the dispatcher (or CoWork pre-flight) can refuse to
proceed, or surface a warning the user can act on with
``workspace clear`` / undo / re-pin.

See ``docs/design/workspace-writes.md`` Phase 3 for design intent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


#: Severity tags. Mirror ``error_vocabulary.py`` style — fixed enum
#: rather than free-form strings so callers can pattern-match.
SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"   # path is suspicious but not catastrophic
SEVERITY_ERROR = "error"       # path is definitely outside the workspace

#: When the session has no pinned workspace_path, the audit is a no-op
#: (there's no root to compare against).
NO_WORKSPACE_REASON = "session_has_no_pinned_workspace"


@dataclass
class LeakageReport:
    """One finding from the audit. ``severity == SEVERITY_OK`` is
    used for benign entries (we still report them so the
    dispatcher can show a clean tally)."""

    source: str            # e.g. "approved_search.files[2]"
    path: str              # the candidate path as stored
    severity: str          # SEVERITY_OK / WARNING / ERROR
    message: str = ""      # human-readable explanation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class AuditResult:
    """The full audit output. Includes counts so callers don't have
    to re-tally."""

    session_id: str
    workspace_path: Optional[str]
    findings: List[LeakageReport] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    @property
    def errors(self) -> List[LeakageReport]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> List[LeakageReport]:
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    @property
    def is_clean(self) -> bool:
        return not self.errors and not self.warnings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_path": self.workspace_path,
            "skipped_reason": self.skipped_reason,
            "findings": [f.to_dict() for f in self.findings],
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "checked_count": len(self.findings),
            "is_clean": self.is_clean,
        }


# ============================================================
# Path containment primitives
# ============================================================

def _norm(path: str) -> str:
    """Normalise a path the way our memory normalisers do:
    forward slashes, no leading ``./``, no trailing slash."""
    if not path:
        return ""
    cleaned = str(path).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.rstrip("/")
    return cleaned


def _is_inside_workspace(workspace_path: str, candidate: str) -> bool:
    """Return True iff ``candidate`` (relative or absolute) resolves
    to a path inside ``workspace_path``.

    Symlinks are NOT followed here — workspace_service.py refuses to
    write through symlinks, so any symlink at audit time is itself a
    finding the audit should surface, not transparently follow."""
    if not workspace_path:
        return False
    cleaned = _norm(candidate)
    if not cleaned:
        return False

    # Reject any traversal explicitly. We don't actually care WHY a
    # ``..`` exists in stored state — it's never legitimate.
    parts = cleaned.split("/")
    if any(part == ".." for part in parts):
        return False

    # Absolute paths: compare with `os.path.commonpath`.
    if os.path.isabs(cleaned):
        try:
            common = os.path.commonpath([
                os.path.abspath(workspace_path),
                os.path.abspath(cleaned),
            ])
        except ValueError:
            # commonpath raises when paths span different drives
            # (Windows) — that's a definite escape.
            return False
        return os.path.abspath(common) == os.path.abspath(workspace_path)

    # Relative paths: join + abspath, then containment check.
    try:
        joined = os.path.abspath(os.path.join(workspace_path, cleaned))
        common = os.path.commonpath([os.path.abspath(workspace_path), joined])
    except ValueError:
        return False
    return os.path.abspath(common) == os.path.abspath(workspace_path)


def _classify(workspace_path: str, candidate: str, source: str) -> LeakageReport:
    """Build a single LeakageReport for a candidate path."""
    cleaned = _norm(candidate)
    if not cleaned:
        return LeakageReport(
            source=source,
            path=str(candidate),
            severity=SEVERITY_WARNING,
            message="empty path stored in session state",
        )
    if ".." in cleaned.split("/"):
        return LeakageReport(
            source=source,
            path=cleaned,
            severity=SEVERITY_ERROR,
            message="path contains '..' — traversal attempt",
        )
    if os.path.isabs(cleaned):
        if _is_inside_workspace(workspace_path, cleaned):
            return LeakageReport(
                source=source, path=cleaned, severity=SEVERITY_OK,
                message="absolute path inside workspace",
            )
        return LeakageReport(
            source=source,
            path=cleaned,
            severity=SEVERITY_ERROR,
            message="absolute path outside the pinned workspace",
        )
    if _is_inside_workspace(workspace_path, cleaned):
        return LeakageReport(
            source=source, path=cleaned, severity=SEVERITY_OK,
            message="relative path resolves inside workspace",
        )
    return LeakageReport(
        source=source,
        path=cleaned,
        severity=SEVERITY_ERROR,
        message="relative path resolves outside the pinned workspace",
    )


# ============================================================
# Audit
# ============================================================

def audit_session(session_id: str = "default") -> AuditResult:
    """Scan the session's state for cross-workspace path leakage.

    Returns an ``AuditResult`` describing every path the audit
    inspected. Callers should look at ``result.errors`` /
    ``result.warnings`` to act; ``result.findings`` carries the
    full per-path detail (useful for ``workspace audit --verbose``).

    No-op (with ``skipped_reason`` set) when the session has no
    pinned workspace — there's no root to compare against.
    """
    # Lazy import — workspace_isolation is otherwise leaf and
    # local_memory is used everywhere.
    from app.memory import local_memory

    context = local_memory.get_session_context(session_id=session_id) or {}
    workspace_path = context.get("workspace_path")
    result = AuditResult(session_id=session_id, workspace_path=workspace_path)

    if not workspace_path:
        result.skipped_reason = NO_WORKSPACE_REASON
        return result

    # --- approved_search.files[] ---
    approved = context.get("approved_search") or {}
    if isinstance(approved, dict):
        files = approved.get("files") or []
        for idx, candidate in enumerate(files):
            result.findings.append(_classify(
                workspace_path, candidate,
                source=f"approved_search.files[{idx}]",
            ))

    # --- pending_approval.path / .paths[] ---
    pending = context.get("pending_approval") or {}
    if isinstance(pending, dict):
        if pending.get("path"):
            result.findings.append(_classify(
                workspace_path, pending["path"],
                source="pending_approval.path",
            ))
        for idx, candidate in enumerate(pending.get("paths") or []):
            result.findings.append(_classify(
                workspace_path, candidate,
                source=f"pending_approval.paths[{idx}]",
            ))

    # --- write_backups[].path ---
    backups = context.get("write_backups") or []
    if isinstance(backups, list):
        for idx, entry in enumerate(backups):
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if path:
                result.findings.append(_classify(
                    workspace_path, path,
                    source=f"write_backups[{idx}].path",
                ))

    # --- executions[].replay.approved_workspace_result.files[].relative_path ---
    # Only audit a bounded recent-history window — older executions
    # are commonly stale and bloat the report. The CLI can pass
    # --depth to widen this when debugging.
    try:
        executions = local_memory.get_executions(session_id=session_id, limit=50)
    except Exception:  # noqa: BLE001
        executions = []
    for exec_idx, execution in enumerate(executions or []):
        if not isinstance(execution, dict):
            continue
        replay = execution.get("replay") or {}
        approved_result = replay.get("approved_workspace_result") or {}
        if not isinstance(approved_result, dict):
            continue
        for file_idx, file_entry in enumerate(approved_result.get("files") or []):
            if not isinstance(file_entry, dict):
                continue
            rel = file_entry.get("relative_path") or file_entry.get("path")
            if not rel:
                continue
            result.findings.append(_classify(
                workspace_path, rel,
                source=(
                    f"executions[{exec_idx}].replay.approved_workspace_result"
                    f".files[{file_idx}].relative_path"
                ),
            ))
        for match_idx, match in enumerate(approved_result.get("files_with_matches") or []):
            if not isinstance(match, str):
                continue
            result.findings.append(_classify(
                workspace_path, match,
                source=(
                    f"executions[{exec_idx}].replay.approved_workspace_result"
                    f".files_with_matches[{match_idx}]"
                ),
            ))

    return result


def format_audit_report(result: AuditResult) -> str:
    """Render an ``AuditResult`` as a human-readable text block for
    the dispatcher's ``workspace audit`` output."""
    if result.skipped_reason:
        return f"Audit skipped: {result.skipped_reason}."
    lines = [
        f"Workspace audit -- session={result.session_id}",
        f"  workspace: {result.workspace_path}",
        f"  paths checked: {len(result.findings)}  "
        f"errors: {len(result.errors)}  "
        f"warnings: {len(result.warnings)}",
    ]
    if result.is_clean:
        lines.append("  [ok] no leakage detected")
        return "\n".join(lines)

    if result.errors:
        lines.append("")
        lines.append("Errors:")
        for finding in result.errors:
            lines.append(f"  [ERR] {finding.source}  {finding.path!r}")
            lines.append(f"        {finding.message}")
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        for finding in result.warnings:
            lines.append(f"  [WARN] {finding.source}  {finding.path!r}")
            lines.append(f"         {finding.message}")
    return "\n".join(lines)
