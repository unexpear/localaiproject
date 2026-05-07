"""Tests for workspace writes Phase 1 (`docs/design/workspace-writes.md`).

Covers:
  - propose write → diff → approve → file appears on disk + backup recorded
  - propose delete → approve → file removed + backup recorded
  - heredoc syntax parses correctly
  - Plan mode refuses write/delete with mode_required
  - 20-write cap refuses with write_cap_reached
  - Symlinks refused with symlink_unsupported
  - Path traversal refused with outside_workspace
  - Stale-content detection: file changed between propose and approve
  - Bus events fire on apply + deny
"""
from __future__ import annotations

import os
import tempfile
import unittest

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import error_vocabulary
from app.services.command_dispatcher import dispatch_input
from app.services.gateway.bus import (
    KIND_WORKSPACE_DENY,
    KIND_WORKSPACE_WRITE,
    default_bus,
    reset_default_bus_for_tests,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


class WorkspaceWritesTests(unittest.TestCase):
    def setUp(self):
        self._mem_tmp = tempfile.TemporaryDirectory()
        self._ws_tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._mem_tmp.name)
        # Real workspace on disk so writes go somewhere predictable.
        self.workspace = self._ws_tmp.name
        local_memory.set_workspace(session_id="default", workspace_path=self.workspace)
        local_memory.set_mode(session_id="default", mode="build")
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._mem_tmp.cleanup()
        self._ws_tmp.cleanup()
        reset_default_bus_for_tests()

    # --- happy path ------------------------------------------------

    def test_write_then_approve_lands_on_disk(self):
        text = "write hello.txt <<EOF\nhello world\nEOF"
        propose = dispatch_input(text, session_id="default")
        self.assertEqual(propose.get("reason"), error_vocabulary.APPROVAL_REQUIRED)
        self.assertEqual(propose["pending_action"]["kind"], "write")
        self.assertEqual(propose["pending_action"]["path"], "hello.txt")

        approve = dispatch_input("approve", session_id="default")
        self.assertNotIn("reason", approve)
        target = os.path.join(self.workspace, "hello.txt")
        self.assertTrue(os.path.exists(target))
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "hello world\n")

        # Backup + write_count are recorded.
        backups = local_memory.get_write_backups(session_id="default")
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["kind"], "write")
        self.assertEqual(backups[0]["path"], "hello.txt")
        self.assertFalse(backups[0]["previous_existed"])
        self.assertEqual(local_memory.get_write_count(session_id="default"), 1)

    def test_modify_existing_file_records_previous_content(self):
        target = os.path.join(self.workspace, "exists.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("original\n")

        dispatch_input("write exists.txt <<EOF\nupdated\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")

        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "updated\n")
        backups = local_memory.get_write_backups(session_id="default")
        self.assertEqual(backups[-1]["previous_existed"], True)
        self.assertEqual(backups[-1]["previous_content"], "original\n")

    def test_inline_write_form(self):
        # `write <path> <content-on-same-line>`
        dispatch_input("write notes.txt one liner here", session_id="default")
        dispatch_input("approve", session_id="default")
        with open(os.path.join(self.workspace, "notes.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "one liner here")

    def test_delete_then_approve_removes_file(self):
        target = os.path.join(self.workspace, "doomed.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("delete me")

        propose = dispatch_input("delete doomed.txt", session_id="default")
        self.assertEqual(propose.get("reason"), error_vocabulary.APPROVAL_REQUIRED)

        approve = dispatch_input("approve", session_id="default")
        self.assertNotIn("reason", approve)
        self.assertFalse(os.path.exists(target))
        backups = local_memory.get_write_backups(session_id="default")
        self.assertEqual(backups[-1]["kind"], "delete")
        self.assertEqual(backups[-1]["previous_content"], "delete me")

    # --- diff ------------------------------------------------------

    def test_diff_shows_pending_write(self):
        target = os.path.join(self.workspace, "src.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("def old():\n    pass\n")

        dispatch_input("write src.py <<EOF\ndef new():\n    return 1\nEOF", session_id="default")
        diff_event = dispatch_input("diff", session_id="default")
        self.assertNotIn("reason", diff_event)
        out = diff_event["output"]
        self.assertIn("[pending write] src.py (modify)", out)
        self.assertIn("-def old():", out)
        self.assertIn("+def new():", out)

    def test_diff_with_no_pending_emits_invalid_argument(self):
        event = dispatch_input("diff", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    # --- mode + cap ------------------------------------------------

    def test_plan_mode_refuses_write(self):
        local_memory.set_mode(session_id="default", mode="plan")
        event = dispatch_input("write foo.txt <<EOF\nbody\nEOF", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.MODE_REQUIRED)

    def test_plan_mode_refuses_delete(self):
        target = os.path.join(self.workspace, "exists.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        local_memory.set_mode(session_id="default", mode="plan")
        event = dispatch_input("delete exists.txt", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.MODE_REQUIRED)

    def test_write_cap_blocks_after_threshold(self):
        # Lower the cap to make this test fast.
        local_memory.set_write_cap(session_id="default", cap=2)
        # Fill the cap.
        dispatch_input("write a.txt <<EOF\nA\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        dispatch_input("write b.txt <<EOF\nB\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        # Third propose should be refused at propose-time.
        event = dispatch_input("write c.txt <<EOF\nC\nEOF", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.WRITE_CAP_REACHED)

    def test_writes_bump_raises_cap(self):
        local_memory.set_write_cap(session_id="default", cap=1)
        dispatch_input("write a.txt <<EOF\nA\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        # At cap (1/1).
        blocked = dispatch_input("write b.txt <<EOF\nB\nEOF", session_id="default")
        self.assertEqual(blocked["reason"], error_vocabulary.WRITE_CAP_REACHED)
        # Bump and retry.
        bump = dispatch_input("workspace writes-bump 5", session_id="default")
        self.assertNotIn("reason", bump)
        dispatch_input("write b.txt <<EOF\nB\nEOF", session_id="default")
        approve = dispatch_input("approve", session_id="default")
        self.assertNotIn("reason", approve)

    def test_no_workspace_pinned_emits_no_workspace(self):
        local_memory.set_workspace(session_id="default", workspace_path=None)
        event = dispatch_input("write foo.txt <<EOF\nbody\nEOF", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.NO_WORKSPACE)

    # --- safety ----------------------------------------------------

    def test_path_traversal_refused(self):
        event = dispatch_input(
            "write ../escape.txt <<EOF\nbad\nEOF", session_id="default"
        )
        self.assertEqual(event["reason"], error_vocabulary.OUTSIDE_WORKSPACE)

    def test_writing_to_directory_refused(self):
        os.makedirs(os.path.join(self.workspace, "subdir"), exist_ok=True)
        event = dispatch_input("write subdir <<EOF\nbody\nEOF", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.IS_DIRECTORY)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported on this OS")
    def test_symlink_target_refused(self):
        target = os.path.join(self.workspace, "real.txt")
        link = os.path.join(self.workspace, "link.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("Cannot create symlinks in this environment")
        event = dispatch_input("write link.txt <<EOF\nbody\nEOF", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.SYMLINK_UNSUPPORTED)

    def test_stale_file_between_propose_and_approve(self):
        target = os.path.join(self.workspace, "race.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("v1")

        dispatch_input("write race.txt <<EOF\nv2-from-agent\nEOF", session_id="default")
        # Simulate someone else editing the file between propose + approve.
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("v1.5-from-someone-else")

        approve = dispatch_input("approve", session_id="default")
        self.assertEqual(approve["reason"], error_vocabulary.WRITE_BACKUP_STALE)
        # File on disk preserved (we refused to overwrite).
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "v1.5-from-someone-else")

    def test_delete_nonexistent_file_emits_path_not_found(self):
        event = dispatch_input("delete ghost.txt", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.PATH_NOT_FOUND)

    # --- syntax errors --------------------------------------------

    def test_write_unclosed_heredoc_emits_invalid_argument(self):
        event = dispatch_input("write foo.txt <<EOF\nno close marker", session_id="default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    def test_delete_without_path_emits_invalid_argument(self):
        # `delete` alone falls through to chat; `delete ` (trailing space)
        # routes here. Keep the test exercising the routed case.
        from app.services.command_dispatcher import _handle_delete_command
        event = _handle_delete_command("delete", "default")
        self.assertEqual(event["reason"], error_vocabulary.INVALID_ARGUMENT)

    # --- bus events -----------------------------------------------

    def test_apply_publishes_workspace_write_event(self):
        captured = []
        bus = default_bus()
        bus.subscribe(bus.ANY, lambda evt: captured.append(evt))

        dispatch_input("write evt.txt <<EOF\nbody\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")

        kinds = [e.kind for e in captured]
        self.assertIn(KIND_WORKSPACE_WRITE, kinds)
        write_event = next(e for e in captured if e.kind == KIND_WORKSPACE_WRITE)
        self.assertEqual(write_event.payload["kind"], "write")
        self.assertEqual(write_event.payload["path"], "evt.txt")
        self.assertEqual(write_event.payload["action"], "create")

    def test_deny_publishes_workspace_deny_event(self):
        captured = []
        bus = default_bus()
        bus.subscribe(bus.ANY, lambda evt: captured.append(evt))

        dispatch_input("write evt.txt <<EOF\nbody\nEOF", session_id="default")
        dispatch_input("deny", session_id="default")

        kinds = [e.kind for e in captured]
        self.assertIn(KIND_WORKSPACE_DENY, kinds)
        deny_event = next(e for e in captured if e.kind == KIND_WORKSPACE_DENY)
        self.assertEqual(deny_event.payload["kind"], "write")
        self.assertEqual(deny_event.payload["path"], "evt.txt")
        # File should NOT have been written.
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "evt.txt")))

    # --- undo (Phase 1.5) -----------------------------------------
    #
    # Note on the staleness gap: Phase 1.5 of `workspace-writes.md`
    # records `previous_sha256` (what the file was BEFORE we wrote)
    # but not what we wrote. So if another process modified the file
    # between our write and our undo, we'll happily restore the
    # previous content over their change. This is documented in the
    # design doc as a fallback safety net, not a transactional
    # guarantee. Phase 1.6 (or whenever) can add `applied_sha256` to
    # close the gap.

    def test_undo_restores_overwritten_file_content(self):
        """Undo after a modify-style write rolls the file back to the
        original content on disk."""
        target = os.path.join(self.workspace, "rollback.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("original content\n")

        dispatch_input(
            "write rollback.txt <<EOF\nclobbered content\nEOF",
            session_id="default",
        )
        dispatch_input("approve", session_id="default")
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "clobbered content\n")

        ev = dispatch_input("undo", session_id="default")
        self.assertEqual(ev["action"], "undo")
        self.assertNotIn("reason", ev)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "original content\n")
        # Backup popped.
        self.assertEqual(local_memory.get_write_backups(session_id="default"), [])

    def test_undo_after_create_deletes_file(self):
        """Undo after a create-style write removes the file from disk."""
        target = os.path.join(self.workspace, "ghost.txt")
        dispatch_input("write ghost.txt <<EOF\nfresh\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        self.assertTrue(os.path.exists(target))

        ev = dispatch_input("undo", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(local_memory.get_write_backups(session_id="default"), [])

    def test_undo_after_delete_restores_file(self):
        """Undo after an approved delete restores the original file content."""
        target = os.path.join(self.workspace, "phoenix.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("rise from ashes\n")

        dispatch_input("delete phoenix.txt", session_id="default")
        dispatch_input("approve", session_id="default")
        self.assertFalse(os.path.exists(target))

        ev = dispatch_input("undo", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertTrue(os.path.exists(target))
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "rise from ashes\n")

    def test_undo_decrements_write_count(self):
        """Undo should free a slot under the soft cap so the user can
        keep working at the same cap after rolling back."""
        local_memory.set_write_cap(session_id="default", cap=2)
        # Two writes, cap full (2/2).
        dispatch_input("write a.txt <<EOF\nA\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        dispatch_input("write b.txt <<EOF\nB\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        self.assertEqual(local_memory.get_write_count(session_id="default"), 2)
        # Third propose blocked.
        blocked = dispatch_input("write c.txt <<EOF\nC\nEOF", session_id="default")
        self.assertEqual(blocked["reason"], error_vocabulary.WRITE_CAP_REACHED)

        # Undo frees a slot.
        dispatch_input("undo", session_id="default")
        self.assertEqual(local_memory.get_write_count(session_id="default"), 1)

        # Now a third write goes through.
        propose = dispatch_input("write c.txt <<EOF\nC\nEOF", session_id="default")
        self.assertEqual(propose.get("reason"), error_vocabulary.APPROVAL_REQUIRED)
        approve = dispatch_input("approve", session_id="default")
        self.assertNotIn("reason", approve)

    def test_undo_with_no_backups_falls_through_to_memory_undo(self):
        """If there are no workspace-write backups, `undo` behaves like
        the original in-memory snapshot rollback (today's behaviour)."""
        # Force two memory mutations so the undo stack has something.
        local_memory.set_workspace(session_id="default", workspace_path=self.workspace)
        local_memory.set_workspace(session_id="default", workspace_path=self.workspace + "_two")
        # Sanity: no write_backups present.
        self.assertEqual(local_memory.get_write_backups(session_id="default"), [])

        ev = dispatch_input("undo", session_id="default")
        self.assertEqual(ev["action"], "undo")
        # Memory undo path returns the "Reverted last memory mutation" string.
        self.assertIn("Reverted", ev["output"])

    def test_undo_with_no_workspace_pinned_emits_no_workspace(self):
        """If a backup is present but the workspace got cleared, the
        undo refuses cleanly with reason=no_workspace."""
        target = os.path.join(self.workspace, "orphan.txt")
        dispatch_input("write orphan.txt <<EOF\nbody\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        self.assertTrue(os.path.exists(target))

        # Clear the workspace pin while keeping the backup record.
        local_memory.set_workspace(session_id="default", workspace_path=None)
        self.assertEqual(len(local_memory.get_write_backups(session_id="default")), 1)

        ev = dispatch_input("undo", session_id="default")
        self.assertEqual(ev["reason"], error_vocabulary.NO_WORKSPACE)
        # File still exists — we refused to act, not silently swallowed it.
        self.assertTrue(os.path.exists(target))
        # Backup still present (we didn't pop it).
        self.assertEqual(len(local_memory.get_write_backups(session_id="default")), 1)

    def test_write_undo_command_is_alias_of_undo_when_backup_present(self):
        """`write undo` is a clarity alias for `undo` mid-write-flow."""
        target = os.path.join(self.workspace, "alias.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("original\n")
        dispatch_input("write alias.txt <<EOF\nupdated\nEOF", session_id="default")
        dispatch_input("approve", session_id="default")
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "updated\n")

        ev = dispatch_input("write undo", session_id="default")
        self.assertEqual(ev["action"], "undo")
        self.assertNotIn("reason", ev)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "original\n")


# ============================================================
# Phase 2 — chat-flow fenced-block detection
# ============================================================

class FencedWriteParserTests(unittest.TestCase):
    """Pure-function parser tests — no chat, no storage."""

    def test_extracts_python_block_with_file_attr(self):
        from app.services.workspace_service import parse_fenced_writes
        text = (
            "Here you go:\n\n"
            "```python file=src/foo.py\n"
            "def foo():\n    return 1\n"
            "```\n"
        )
        results = parse_fenced_writes(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "src/foo.py")
        self.assertEqual(results[0]["content"], "def foo():\n    return 1")

    def test_returns_empty_for_block_without_file_attr(self):
        from app.services.workspace_service import parse_fenced_writes
        text = "```python\ndef plain(): pass\n```"
        self.assertEqual(parse_fenced_writes(text), [])

    def test_extracts_multiple_blocks_in_order(self):
        from app.services.workspace_service import parse_fenced_writes
        text = (
            "```python file=a.py\nA\n```\n"
            "Some prose.\n"
            "```text file=README.md\nREADME body\n```\n"
        )
        results = parse_fenced_writes(text)
        self.assertEqual([r["path"] for r in results], ["a.py", "README.md"])

    def test_handles_quoted_paths(self):
        from app.services.workspace_service import parse_fenced_writes
        text = '```python file="src/has spaces.py"\nbody\n```'
        results = parse_fenced_writes(text)
        self.assertEqual(results[0]["path"], "src/has spaces.py")

    def test_normalises_backslashes_and_dot_slash(self):
        from app.services.workspace_service import parse_fenced_writes
        text = "```python file=./src\\foo.py\nbody\n```"
        results = parse_fenced_writes(text)
        self.assertEqual(results[0]["path"], "src/foo.py")

    def test_ignores_block_with_no_path_after_file_attr(self):
        from app.services.workspace_service import parse_fenced_writes
        text = "```python file=\nbody\n```"
        self.assertEqual(parse_fenced_writes(text), [])

    def test_empty_text_returns_empty(self):
        from app.services.workspace_service import parse_fenced_writes
        self.assertEqual(parse_fenced_writes(""), [])
        self.assertEqual(parse_fenced_writes(None), [])


class ChatFlowDetectionTests(unittest.TestCase):
    """End-to-end: a chat response with a fenced block becomes a
    pending write that the user approves through the existing gate."""

    def setUp(self):
        self._mem_tmp = tempfile.TemporaryDirectory()
        self._ws_tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._mem_tmp.name)
        self.workspace = self._ws_tmp.name
        local_memory.set_workspace(session_id="default", workspace_path=self.workspace)
        local_memory.set_mode(session_id="default", mode="build")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._mem_tmp.cleanup()
        self._ws_tmp.cleanup()

    def _stub_chat(self, response_text):
        """Patch chat_providers.dispatch_chat to return a canned model
        response (so we don't hit Ollama)."""
        from unittest.mock import patch as _patch
        return _patch(
            "app.services.chat_service.chat_providers.dispatch_chat",
            return_value={
                "ok": True, "content": response_text,
                "error": None, "raw": {},
            },
        )

    def _stub_router(self, lane="reasoning"):
        from unittest.mock import patch as _patch
        return _patch(
            "app.supervisor.router.route_message",
            return_value={
                "lane": lane, "confidence": 0.9, "scores": {lane: 0.9},
                "primary_signal": "test", "context_used": False, "context_reason": "",
            },
        )

    def _stub_resolution(self, model="llama3.1:8b", lane="reasoning"):
        from unittest.mock import patch as _patch
        return _patch(
            "app.models.model_registry.resolve_model_for_role",
            return_value={
                "model": model, "resolved_role": lane, "fallback_used": False,
            },
        )

    def test_fenced_block_in_response_emits_write_proposal(self):
        from app.services.chat_service import handle_chat
        canned_response = (
            "Here's the file:\n\n"
            "```python file=hello.py\n"
            "print('hi')\n"
            "```"
        )
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            result = handle_chat("write me hello.py", session_id="default")

        self.assertTrue(result["approval_required"])
        self.assertIsNotNone(result["pending_action"])
        self.assertEqual(result["pending_action"]["kind"], "write")
        self.assertEqual(result["pending_action"]["path"], "hello.py")
        self.assertEqual(result["pending_action"]["origin"], "chat")
        self.assertEqual(result["pending_action"]["content"], "print('hi')")

    def test_plan_mode_does_not_emit_proposal(self):
        from app.services.chat_service import handle_chat
        local_memory.set_mode(session_id="default", mode="plan")
        canned_response = "```python file=hello.py\nprint('hi')\n```"
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            result = handle_chat("write me hello.py", session_id="default")
        self.assertFalse(result["approval_required"])
        self.assertIsNone(result["pending_action"])

    def test_response_without_file_attr_does_not_propose(self):
        from app.services.chat_service import handle_chat
        canned_response = (
            "Here's some code (no file= info, just for show):\n"
            "```python\nprint('hi')\n```"
        )
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            result = handle_chat("show me", session_id="default")
        self.assertFalse(result["approval_required"])

    def test_proposal_skipped_if_already_pending(self):
        from app.services.chat_service import handle_chat
        # Queue a prior approval (a read this time).
        local_memory.set_pending_approval(
            session_id="default",
            pending_approval={
                "kind": "read",
                "origin": "command",
                "workspace_path": self.workspace,
                "paths": ["other.txt"],
            },
        )
        canned_response = "```python file=hello.py\nprint('hi')\n```"
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            result = handle_chat("write me hello.py", session_id="default")
        # The chat-flow proposal didn't clobber the prior pending.
        self.assertFalse(result["approval_required"])

    def test_proposal_skipped_when_no_workspace_pinned(self):
        from app.services.chat_service import handle_chat
        local_memory.set_workspace(session_id="default", workspace_path=None)
        canned_response = "```python file=hello.py\nprint('hi')\n```"
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            result = handle_chat("write me hello.py", session_id="default")
        self.assertFalse(result["approval_required"])

    def test_path_traversal_in_fenced_block_skips_proposal(self):
        from app.services.chat_service import handle_chat
        canned_response = "```python file=../escape.py\nbad\n```"
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            result = handle_chat("escape", session_id="default")
        # The resolver rejects ../, so no proposal is emitted (the
        # response itself still passes through to the user).
        self.assertFalse(result["approval_required"])

    def test_approve_chat_origin_write_lands_on_disk(self):
        """End-to-end: chat → fenced block → approve → file appears."""
        # First turn: send a chat that proposes a write.
        canned_response = (
            "```python file=greeting.py\n"
            "def greet():\n    return 'hello'\n"
            "```"
        )
        with self._stub_chat(canned_response), self._stub_router(), self._stub_resolution():
            propose = dispatch_input("please write greeting.py", session_id="default")

        self.assertEqual(propose.get("reason"), "approval_required")
        # Approve applies the write.
        approve = dispatch_input("approve", session_id="default")
        self.assertNotIn("reason", approve)
        target = os.path.join(self.workspace, "greeting.py")
        self.assertTrue(os.path.exists(target))
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "def greet():\n    return 'hello'")


class ChatFlowTaskCompletionTests(unittest.TestCase):
    """When a chat-origin write applies, the corresponding builder task
    (paused with the approval marker) flips to done so cowork can move on.
    """

    def setUp(self):
        self._mem_tmp = tempfile.TemporaryDirectory()
        self._ws_tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._mem_tmp.name)
        self.workspace = self._ws_tmp.name
        local_memory.add_builder("planner", "coder")
        local_memory.set_workspace(session_id="coder", workspace_path=self.workspace)
        local_memory.set_mode(session_id="coder", mode="build")

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._mem_tmp.cleanup()
        self._ws_tmp.cleanup()

    def test_paused_task_marked_done_after_chat_write_approves(self):
        from unittest.mock import patch as _patch
        canned_response = (
            "Here it is:\n```python file=foo.py\nx = 1\n```"
        )

        # Drive the inbox manually: planner adds a task; runner processes
        # it; the chat fenced block produces a paused task; we approve;
        # we verify the task is now done.
        task = local_memory.add_task("planner", "create foo.py", builder="coder")

        with _patch(
            "app.services.chat_service.chat_providers.dispatch_chat",
            return_value={"ok": True, "content": canned_response, "error": None, "raw": {}},
        ), _patch(
            "app.supervisor.router.route_message",
            return_value={
                "lane": "coding", "confidence": 0.9, "scores": {"coding": 0.9},
                "primary_signal": "t", "context_used": False, "context_reason": "",
            },
        ), _patch(
            "app.models.model_registry.resolve_model_for_role",
            return_value={
                "model": "llama3.1:8b", "resolved_role": "coding",
                "fallback_used": False,
            },
        ):
            ev = dispatch_input("inbox next", session_id="coder")

        # Task should be paused awaiting approval.
        snap = local_memory.get_task(task["id"])
        self.assertEqual(snap["status"], "in_progress")
        self.assertEqual(snap["result"], "(awaiting workspace approval)")

        # Approve the chat-origin write; the dispatcher should ALSO mark
        # the paused task done so cowork can move on.
        approve = dispatch_input("approve", session_id="coder")
        self.assertNotIn("reason", approve)

        snap = local_memory.get_task(task["id"])
        self.assertEqual(snap["status"], "done")
        self.assertIn("foo.py", snap["result"])
        # File made it to disk.
        target = os.path.join(self.workspace, "foo.py")
        self.assertTrue(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
