"""Git plumbing for cowork branch strategies (Phase 5).

Three branch strategies the cowork plan can declare:

  - ``head`` — default, no git involvement; writes apply to whatever
    branch is currently checked out
  - ``branch`` — plan declares a target ``branch_name``; cowork creates
    + checks out the branch (off the current HEAD), runs sub-tasks,
    leaves the branch checked out at the end. User merges by hand.
  - ``merge-to-head`` — like ``branch`` but the branch name is auto-
    generated as ``lai/<session>/<run-id>``; on a successful run cowork
    checks out the original branch and ``git merge --no-ff`` the run
    branch back; on failure leaves the run branch for the user to
    inspect.

Every approved workspace write inside the run gets ``git add`` +
``git commit -m "<run-id> sub_<sub-id>: <path>"`` so the branch
accumulates a clean per-write history.

Tests use ``patch.object(cowork_git, "_run_git", side_effect=...)`` to
inject canned subprocess responses. The real ``_run_git`` shells out
to ``git`` via ``subprocess.run`` (text mode, captured output,
``check=False`` so callers see the return code).
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)


# ============================================================
# Subprocess primitive
# ============================================================

@dataclass
class GitResult:
    """A single ``git`` invocation's outcome."""
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    args: Tuple[str, ...]


def _run_git(workspace: str, *args: str, timeout: float = 30.0) -> GitResult:
    """Run a single git command in ``workspace``.

    The single source of truth for `subprocess.run` invocations in
    this module. Tests monkey-patch this function to inject canned
    GitResult values without forking a real ``git`` process.
    """
    cmd = ("git", *args)
    try:
        completed = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return GitResult(
            ok=False, returncode=-1,
            stdout="", stderr="git not on PATH",
            args=cmd,
        )
    except subprocess.TimeoutExpired:
        return GitResult(
            ok=False, returncode=-1,
            stdout="", stderr=f"git command timed out: {' '.join(cmd)}",
            args=cmd,
        )
    return GitResult(
        ok=(completed.returncode == 0),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        args=cmd,
    )


# ============================================================
# High-level helpers
# ============================================================

def is_git_repo(workspace: str) -> bool:
    """True if ``workspace`` is inside a git working tree."""
    if not workspace or not os.path.isdir(workspace):
        return False
    result = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return result.ok and result.stdout.strip() == "true"


def current_branch(workspace: str) -> Optional[str]:
    """Return the name of the currently checked-out branch, or None on
    detached HEAD or any error."""
    result = _run_git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if not result.ok:
        return None
    name = result.stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def branch_exists(workspace: str, name: str) -> bool:
    result = _run_git(
        workspace, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"
    )
    return result.ok


def create_and_checkout_branch(
    workspace: str, name: str, *, base: Optional[str] = None,
) -> GitResult:
    """``git checkout -b <name> [<base>]`` — fail-fast if branch exists."""
    args = ("checkout", "-b", name)
    if base:
        args = (*args, base)
    return _run_git(workspace, *args)


def checkout_branch(workspace: str, name: str) -> GitResult:
    return _run_git(workspace, "checkout", name)


def commit_path(
    workspace: str,
    rel_path: str,
    message: str,
    *,
    allow_delete: bool = False,
) -> GitResult:
    """``git add <path>`` + ``git commit -m <message>``.

    For deletes, ``git add`` still records the removal (post-`git rm`
    semantics aren't needed since the file is already removed from
    disk). ``allow_delete=True`` is informational; behaviour is the
    same — caller is responsible for the disk-side mutation.
    """
    add = _run_git(workspace, "add", "--", rel_path)
    if not add.ok:
        return add
    commit = _run_git(workspace, "commit", "-m", message)
    return commit


def merge_branch(
    workspace: str, source: str, *, no_ff: bool = True, message: Optional[str] = None,
) -> GitResult:
    """Merge ``source`` into the currently-checked-out branch."""
    args: List[str] = ["merge"]
    if no_ff:
        args.append("--no-ff")
    if message:
        args.extend(["-m", message])
    args.append(source)
    return _run_git(workspace, *args)


def delete_branch(workspace: str, name: str, *, force: bool = False) -> GitResult:
    flag = "-D" if force else "-d"
    return _run_git(workspace, "branch", flag, name)


def has_uncommitted_changes(workspace: str) -> bool:
    """True if the working tree has any tracked changes (added,
    modified, deleted, renamed). Untracked files don't count."""
    result = _run_git(workspace, "status", "--porcelain", "-uno")
    return result.ok and bool(result.stdout.strip())


# ============================================================
# Convenience: build the run-branch name
# ============================================================

def auto_branch_name(session_id: str, run_id: str) -> str:
    """Default branch name for ``merge-to-head`` strategy."""
    return f"lai/{session_id}/{run_id}"
