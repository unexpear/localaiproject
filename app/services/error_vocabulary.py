"""Canonical error / refusal reason tags for the dispatcher.

Every dispatcher event that fails or refuses an action SHOULD carry
``event["reason"] = "<tag>"`` from this fixed enum so consumers can
pattern-match without parsing English.

Design: ``docs/design/error-vocabulary.md``.

The ``output`` field in an event stays human-readable. The ``reason``
field is for machines. Successful events leave ``reason`` unset
(absence-of-key means "everything fine").
"""

from __future__ import annotations

# --- Workspace / path errors -------------------------------------------------

NO_WORKSPACE = "no_workspace"
PATH_NOT_FOUND = "path_not_found"
OUTSIDE_WORKSPACE = "outside_workspace"
BINARY_FILE = "binary_file"
IS_DIRECTORY = "is_directory"

# --- Approval lifecycle ------------------------------------------------------

APPROVAL_REQUIRED = "approval_required"
APPROVAL_PENDING = "approval_pending"

# --- Coordination ------------------------------------------------------------

NO_LINK = "no_link"
UNKNOWN_BUILDER = "unknown_builder"
INBOX_EMPTY = "inbox_empty"

# --- Install / runtime -------------------------------------------------------

UNKNOWN_TARGET = "unknown_target"
RUNTIME_OFFLINE = "runtime_offline"
MODEL_UNAVAILABLE = "model_unavailable"
VERSION_TOO_OLD = "version_too_old"
HARDWARE_TOO_SMALL = "hardware_too_small"

# --- Lifecycle ---------------------------------------------------------------

CANCELLED = "cancelled"
TIMEOUT = "timeout"
ALREADY_RUNNING = "already_running"

# --- Generic -----------------------------------------------------------------

INVALID_ARGUMENT = "invalid_argument"
UNKNOWN_COMMAND = "unknown_command"
INTERNAL_ERROR = "internal_error"

# --- Mode + workspace-write extensions (planned, see workspace-writes.md) ----

MODE_REQUIRED = "mode_required"
WRITE_CAP_REACHED = "write_cap_reached"
WRITE_BACKUP_STALE = "write_backup_stale"
SYMLINK_UNSUPPORTED = "symlink_unsupported"

# --- Chat provider extensions (planned, see chat-providers.md) ---------------

PROVIDER_UNKNOWN = "provider_unknown"
PROVIDER_UNAUTHENTICATED = "provider_unauthenticated"
PROVIDER_UNREACHABLE = "provider_unreachable"
PROVIDER_QUOTA_EXCEEDED = "provider_quota_exceeded"

# --- Skill extensions (planned, see browser-automation.md, extensibility.md) -

SKILL_UNAVAILABLE = "skill_unavailable"
ORIGIN_NOT_APPROVED = "origin_not_approved"
EVAL_REFUSED = "eval_refused"
PLAYWRIGHT_NOT_INSTALLED = "playwright_not_installed"

# --- Rule + template extensions (planned, see agent-rules.md / templates) ----

RULE_LOOP = "rule_loop"
INPUT_TOKENS_CAPPED = "input_tokens_capped"
PRODUCTION_LOCAL_ONLY = "production_local_only"
TEMPLATE_PARAM_MISSING = "template_param_missing"
TEMPLATE_RECURSION = "template_recursion"
TEMPLATE_UNKNOWN_STEP = "template_unknown_step"

# --- CoWork extensions (see docs/design/cowork-mode.md) ----------------------

#: A `cowork plan` was requested but the session already has an active run.
COWORK_IN_PROGRESS = "cowork_in_progress"

#: The planner returned text that didn't contain a parseable JSON plan.
INVALID_PLAN_JSON = "invalid_plan_json"

#: The plan parsed but had no sub-tasks.
EMPTY_PLAN = "empty_plan"

#: The plan exceeded MAX_SUBTASKS (10).
TOO_MANY_SUBTASKS = "too_many_subtasks"

#: A sub-task's `depends_on` referenced an id not present in the plan.
UNKNOWN_DEPENDENCY = "unknown_dependency"

#: The dependency graph contains a cycle (no valid topological order).
CYCLE_IN_PLAN = "cycle_in_plan"

#: Action attempted on a cowork run in the wrong status (e.g. running
#: a run that hasn't been approved yet, cancelling a `done` run).
INVALID_STATE = "invalid_state"

#: A cowork sub-task's `before` hook returned a non-empty reason —
#: the sub-task itself is skipped with this tag and downstream
#: dependents auto-skip with `dependency_failed`.
PRECONDITION_FAILED = "precondition_failed"

#: The pinned slots in the prompt assembly alone exceed
#: ``total_budget`` — eviction can't bring the assembly under cap.
#: This is a *warning* tag (the chat still proceeds with the
#: oversize prompt), surfaced in the trace footer + execution
#: metadata so users can raise the budget or trim a pinned slot.
#: See `docs/design/prompt-assembly.md` Phase 2.
PROMPT_BUDGET_EXCEEDED = "prompt_budget_exceeded"

#: A ``Patch(kind=...)`` was emitted with an unrecognised kind
#: outside ``KNOWN_PATCH_KINDS``. See `docs/design/patch-taxonomy.md`.
UNKNOWN_PATCH_KIND = "unknown_patch_kind"

#: A ``Patch(kind="bundle")`` had more than ``BUNDLE_MAX_SUB_PATCHES``
#: sub-patches. Composers should split larger changes across multiple
#: bundles or fall back to single-write approvals. See
#: `docs/design/patch-taxonomy.md`.
BUNDLE_TOO_LARGE = "bundle_too_large"


#: The canonical, frozen set of every reason tag the dispatcher (or any
#: layered handler) may emit. Anything not in this set is a bug — the
#: ``test_error_vocabulary`` guard catches typos and undocumented tags
#: at test time.
ERROR_REASONS: frozenset[str] = frozenset(
    {
        NO_WORKSPACE,
        PATH_NOT_FOUND,
        OUTSIDE_WORKSPACE,
        BINARY_FILE,
        IS_DIRECTORY,
        APPROVAL_REQUIRED,
        APPROVAL_PENDING,
        NO_LINK,
        UNKNOWN_BUILDER,
        INBOX_EMPTY,
        UNKNOWN_TARGET,
        RUNTIME_OFFLINE,
        MODEL_UNAVAILABLE,
        VERSION_TOO_OLD,
        HARDWARE_TOO_SMALL,
        CANCELLED,
        TIMEOUT,
        ALREADY_RUNNING,
        INVALID_ARGUMENT,
        UNKNOWN_COMMAND,
        INTERNAL_ERROR,
        MODE_REQUIRED,
        WRITE_CAP_REACHED,
        WRITE_BACKUP_STALE,
        SYMLINK_UNSUPPORTED,
        PROVIDER_UNKNOWN,
        PROVIDER_UNAUTHENTICATED,
        PROVIDER_UNREACHABLE,
        PROVIDER_QUOTA_EXCEEDED,
        SKILL_UNAVAILABLE,
        ORIGIN_NOT_APPROVED,
        EVAL_REFUSED,
        PLAYWRIGHT_NOT_INSTALLED,
        RULE_LOOP,
        INPUT_TOKENS_CAPPED,
        PRODUCTION_LOCAL_ONLY,
        TEMPLATE_PARAM_MISSING,
        TEMPLATE_RECURSION,
        TEMPLATE_UNKNOWN_STEP,
        COWORK_IN_PROGRESS,
        INVALID_PLAN_JSON,
        EMPTY_PLAN,
        TOO_MANY_SUBTASKS,
        UNKNOWN_DEPENDENCY,
        CYCLE_IN_PLAN,
        INVALID_STATE,
        PRECONDITION_FAILED,
        PROMPT_BUDGET_EXCEEDED,
        UNKNOWN_PATCH_KIND,
        BUNDLE_TOO_LARGE,
    }
)


def is_canonical(reason: str | None) -> bool:
    """True if ``reason`` is a recognised tag (or ``None``).

    ``None`` is allowed because successful events legitimately leave the
    field unset. Anything non-None must be in ``ERROR_REASONS``.
    """
    return reason is None or reason in ERROR_REASONS
