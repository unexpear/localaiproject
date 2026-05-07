"""Guard tests for the canonical error/refusal reason vocabulary.

Phase 1 of the error-vocabulary rollout (see ``docs/design/error-vocabulary.md``).

Two layers of protection:

1. **Enum pinning** — the frozenset of canonical tags lives in
   ``app/services/error_vocabulary.py``. The test below copies the
   names verbatim and asserts the runtime set matches. Adding a new
   tag without updating this test is a hard fail; that's the point.

2. **Source lint** — every ``"reason": "<literal>"`` in dispatcher
   source must be in the canonical set. Catches typos before they
   reach a consumer that pattern-matches on tags.

Per-handler emit-site tests (Phase 2) live alongside each handler's
existing test file so they're discoverable from the handler itself.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import error_vocabulary
from app.services.command_dispatcher import (
    ERROR_REASONS as REEXPORTED_REASONS,
    _handle_read_command,
    _handle_search_command,
    _handle_session_switch,
    dispatch_input,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# Copied verbatim from docs/design/error-vocabulary.md. Editing this set
# without updating the doc (or vice versa) is a bug — keep them in lock-step.
CANONICAL_REASON_TAGS = frozenset(
    {
        # Workspace / path
        "no_workspace",
        "path_not_found",
        "outside_workspace",
        "binary_file",
        "is_directory",
        # Approval lifecycle
        "approval_required",
        "approval_pending",
        # Coordination
        "no_link",
        "unknown_builder",
        "inbox_empty",
        # Install / runtime
        "unknown_target",
        "runtime_offline",
        "model_unavailable",
        "version_too_old",
        "hardware_too_small",
        # Lifecycle
        "cancelled",
        "timeout",
        "already_running",
        # Generic
        "invalid_argument",
        "unknown_command",
        "internal_error",
        # Mode + workspace-write extensions
        "mode_required",
        "write_cap_reached",
        "write_backup_stale",
        "symlink_unsupported",
        # Chat-provider extensions
        "provider_unknown",
        "provider_unauthenticated",
        "provider_unreachable",
        "provider_quota_exceeded",
        # Skill / browser extensions
        "skill_unavailable",
        "origin_not_approved",
        "eval_refused",
        "playwright_not_installed",
        # Rule + template extensions
        "rule_loop",
        "input_tokens_capped",
        "production_local_only",
        "template_param_missing",
        "template_recursion",
        "template_unknown_step",
        # CoWork extensions
        "cowork_in_progress",
        "invalid_plan_json",
        "empty_plan",
        "too_many_subtasks",
        "unknown_dependency",
        "cycle_in_plan",
        "invalid_state",
        "precondition_failed",
        # Prompt assembly Phase 2 — pinned slots alone exceed the
        # total budget; warning, not refusal. See prompt-assembly.md.
        "prompt_budget_exceeded",
        # Patch taxonomy Phase 1 — typed mutation envelope.
        # See patch-taxonomy.md.
        "unknown_patch_kind",
        "bundle_too_large",
    }
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DISPATCHER_SOURCE = _REPO_ROOT / "app" / "services" / "command_dispatcher.py"

# Matches `"reason": "<tag>"` and `'reason': '<tag>'` literals in source.
# We deliberately allow the trailing comma/brace via lookahead; the inner
# group is just the tag string itself. Skips matches inside a
# ``pending_action`` dict (those have a different semantic — they describe
# why the action was queued, not the canonical error tag).
_REASON_LITERAL_RE = re.compile(r'["\']reason["\']\s*:\s*["\']([a-z_][a-z0-9_]*)["\']')

# Tags that legitimately appear in source as pending_action descriptions
# rather than canonical error tags. Whitelist them so the lint doesn't
# flag known false-positives. (Keep this list short — anything genuinely
# dispatcher-emitted should join the canonical set instead.)
_PENDING_ACTION_REASONS = frozenset(
    {
        "manual workspace read command",
        "manual workspace search command",
    }
)


class ErrorVocabularyEnumTests(unittest.TestCase):
    def test_runtime_set_matches_canonical(self):
        """The module-level ERROR_REASONS must equal the doc-pinned set."""
        self.assertEqual(error_vocabulary.ERROR_REASONS, CANONICAL_REASON_TAGS)

    def test_dispatcher_reexports_same_set(self):
        """`from command_dispatcher import ERROR_REASONS` must work and
        return the same object (identity, not just equality)."""
        self.assertIs(REEXPORTED_REASONS, error_vocabulary.ERROR_REASONS)

    def test_each_module_constant_is_in_set(self):
        """Every named constant in error_vocabulary.py is in the set."""
        for name in dir(error_vocabulary):
            if name.startswith("_") or name == "ERROR_REASONS":
                continue
            value = getattr(error_vocabulary, name)
            if not isinstance(value, str):
                continue
            self.assertIn(
                value,
                error_vocabulary.ERROR_REASONS,
                f"Constant {name}={value!r} is not in ERROR_REASONS",
            )

    def test_is_canonical_helper(self):
        self.assertTrue(error_vocabulary.is_canonical(None))
        self.assertTrue(error_vocabulary.is_canonical("no_workspace"))
        self.assertTrue(error_vocabulary.is_canonical("template_recursion"))
        self.assertFalse(error_vocabulary.is_canonical(""))
        self.assertFalse(error_vocabulary.is_canonical("not_a_real_tag"))
        self.assertFalse(error_vocabulary.is_canonical("INVALID_ARGUMENT"))


class DispatcherSourceLintTests(unittest.TestCase):
    """Scan the dispatcher source for `reason` literals and assert they
    come from the canonical set (or the pending_action whitelist)."""

    def test_every_reason_literal_is_canonical_or_whitelisted(self):
        """No assertion that any literals exist — Phase 2 wiring may
        still be in flight. Only asserts that whatever literals DO
        exist are canonical."""
        source = _DISPATCHER_SOURCE.read_text(encoding="utf-8")
        matches = _REASON_LITERAL_RE.findall(source)
        offenders = [tag for tag in matches if tag not in error_vocabulary.ERROR_REASONS]
        self.assertEqual(
            offenders,
            [],
            f"Non-canonical reason tag(s) in dispatcher source: {offenders}. "
            f"Either fix the typo or add the tag to error_vocabulary.py + this test.",
        )

    def test_known_pending_action_descriptions_are_explicit(self):
        """The two existing pending_action `reason` strings are NOT
        canonical error tags. Document them here so future readers know
        the field name `reason` is overloaded inside `pending_action`
        dicts. This test exists to anchor that fact."""
        source = _DISPATCHER_SOURCE.read_text(encoding="utf-8")
        for description in _PENDING_ACTION_REASONS:
            self.assertIn(
                f'"{description}"',
                source,
                f"Expected pending_action description {description!r} still in source. "
                f"If you removed it, drop it from _PENDING_ACTION_REASONS too.",
            )


class HandlerEmitSiteTests(unittest.TestCase):
    """End-to-end checks that each wired handler emits the expected
    canonical reason on its known error/refusal paths.

    Drives `dispatch_input` directly so the test observes exactly what
    a real consumer (HTTP gateway, MCP server, agent) would see."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    # --- workspace -------------------------------------------------

    def test_read_without_workspace_emits_no_workspace(self):
        event = dispatch_input("read foo.py", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.NO_WORKSPACE)

    def test_search_without_workspace_emits_no_workspace(self):
        event = dispatch_input("search needle", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.NO_WORKSPACE)

    def test_read_without_path_emits_invalid_argument(self):
        # The dispatcher's `dispatch_input` requires `read ` (with
        # trailing token) to route here, so test the handler directly
        # to exercise the empty-path branch.
        local_memory.set_workspace(session_id="default", workspace_path=self._tmp.name)
        event = _handle_read_command("read", "default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_search_without_query_emits_invalid_argument(self):
        local_memory.set_workspace(session_id="default", workspace_path=self._tmp.name)
        event = _handle_search_command("search", "default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_read_success_path_emits_approval_required(self):
        local_memory.set_workspace(session_id="default", workspace_path=self._tmp.name)
        event = dispatch_input("read foo.py", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.APPROVAL_REQUIRED)

    def test_approve_with_nothing_pending_emits_invalid_argument(self):
        event = dispatch_input("approve", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_deny_with_nothing_pending_emits_invalid_argument(self):
        event = dispatch_input("deny", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_workspace_path_not_found_emits_path_not_found(self):
        event = dispatch_input(
            "workspace /this/path/definitely/does/not/exist/anywhere",
            session_id="default",
        )
        self.assertEqual(event["reason"], error_vocabulary.PATH_NOT_FOUND)

    # --- coordination ----------------------------------------------

    def test_dispatch_with_no_link_emits_no_link(self):
        event = dispatch_input("dispatch some task", session_id="planner")
        self.assertEqual(event["reason"], error_vocabulary.NO_LINK)

    def test_dispatch_to_unknown_builder_emits_unknown_builder(self):
        local_memory.add_builder("planner", "builder1")
        event = dispatch_input("dispatch @ghost task", session_id="planner")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_BUILDER)

    def test_dispatch_with_no_text_emits_invalid_argument(self):
        event = dispatch_input("dispatch", session_id="planner")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_inbox_empty_emits_inbox_empty(self):
        event = dispatch_input("inbox", session_id="builder1")
        self.assertEqual(event["reason"], error_vocabulary.INBOX_EMPTY)

    def test_inbox_skip_empty_emits_inbox_empty(self):
        event = dispatch_input("inbox skip", session_id="builder1")
        self.assertEqual(event["reason"], error_vocabulary.INBOX_EMPTY)

    def test_inbox_unknown_subcommand_emits_invalid_argument(self):
        event = dispatch_input("inbox bogus", session_id="builder1")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_tasks_with_unknown_status_emits_invalid_argument(self):
        event = dispatch_input("tasks blue", session_id="planner")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    # --- wait ------------------------------------------------------

    def test_wait_inbox_without_target_emits_invalid_argument(self):
        event = dispatch_input("wait inbox", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_wait_task_unknown_id_emits_unknown_target(self):
        event = dispatch_input("wait task task_does_not_exist", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_TARGET)

    # --- install ---------------------------------------------------

    def test_install_unknown_target_emits_unknown_target(self):
        event = dispatch_input("install bogus_role_name_123", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_TARGET)

    def test_install_without_target_emits_invalid_argument(self):
        event = dispatch_input("install", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_uninstall_unknown_target_emits_unknown_target(self):
        event = dispatch_input("uninstall bogus_role_name_123", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_TARGET)

    # --- skill / mode / session / retry / execution ---------------

    def test_skill_unknown_emits_unknown_target(self):
        event = dispatch_input("skill add ghost_skill_does_not_exist", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_TARGET)

    def test_skill_usage_without_action_emits_invalid_argument(self):
        event = dispatch_input("skill bogus", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_mode_with_unknown_target_emits_invalid_argument(self):
        event = dispatch_input("mode bogus", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_session_switch_without_name_emits_invalid_argument(self):
        # Bare "session" falls through to chat in dispatch_input; the
        # empty-name branch is reachable only via the handler itself.
        event = _handle_session_switch("session")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_retry_unknown_execution_emits_unknown_target(self):
        event = dispatch_input("retry exe_does_not_exist", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_TARGET)

    def test_execution_unknown_id_emits_unknown_target(self):
        event = dispatch_input("execution exe_does_not_exist", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.UNKNOWN_TARGET)

    # --- chat fallback approval ------------------------------------

    def test_chat_approval_required_emits_approval_required(self):
        """When chat returns approval_required, the dispatcher event
        carries the canonical reason tag (not just the result blob)."""
        with patch("app.services.command_dispatcher.handle_chat") as mock_chat:
            mock_chat.return_value = {
                "response": "[approval required] read foo.py",
                "ok": True,
                "approval_required": True,
                "pending_action": {
                    "kind": "read",
                    "origin": "chat",
                    "workspace_path": self._tmp.name,
                    "paths": ["foo.py"],
                },
                "fallback_used": False,
                "requested_lane": "reasoning",
                "resolved_lane": "reasoning",
                "model": "llama3.1:8b",
            }
            # Use a chat input that doesn't match any command prefix
            # (otherwise the dispatcher routes around handle_chat).
            event = dispatch_input("could you check the README for me", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.APPROVAL_REQUIRED)

    # --- success paths leave reason unset -------------------------

    def test_workspace_status_success_has_no_reason(self):
        event = dispatch_input("workspace", session_id="default")
        self.assertNotIn("reason", event)

    def test_inbox_with_queued_task_has_no_reason(self):
        local_memory.add_builder("planner", "builder1")
        local_memory.add_task("planner", "do a thing", builder="builder1")
        event = dispatch_input("inbox", session_id="builder1")
        self.assertNotIn("reason", event)


if __name__ == "__main__":
    unittest.main()
