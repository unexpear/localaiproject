"""Tests for backlog adapters Phase 1 (`docs/design/extensibility.md` §8).

Four layers:
  - JsonFileSource (built-in source — file format validation + parsing)
  - Registry: register_backlog_source / list_sources / get_source
  - pull_into_inbox: dispatches each new item; idempotent on re-pull
  - Dispatcher commands: `backlog list / sources / pull <id> [k=v ...]`
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterator

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import backlog_service as bl
from app.services.command_dispatcher import dispatch_input


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _write_backlog_file(path, items):
    Path(path).write_text(json.dumps(items), encoding="utf-8")


# ============================================================
# JsonFileSource
# ============================================================

class JsonFileSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        bl._reset_for_tests()

    def tearDown(self):
        self._tmp.cleanup()
        bl._reset_for_tests()

    def test_iter_items_yields_each_entry(self):
        path = os.path.join(self._tmp.name, "tasks.json")
        _write_backlog_file(path, [
            {"id": "t1", "body": "Do X"},
            {"id": "t2", "body": "Do Y", "builder_hint": "coder"},
        ])
        source = bl.JsonFileSource()
        items = list(source.iter_items({"path": path}))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].id, "t1")
        self.assertEqual(items[1].builder_hint, "coder")

    def test_skips_entries_missing_id_or_body(self):
        path = os.path.join(self._tmp.name, "tasks.json")
        _write_backlog_file(path, [
            {"id": "good", "body": "ok"},
            {"id": "no-body"},
            {"body": "no-id"},
            {},
        ])
        items = list(bl.JsonFileSource().iter_items({"path": path}))
        self.assertEqual([i.id for i in items], ["good"])

    def test_missing_path_raises(self):
        with self.assertRaises(ValueError):
            list(bl.JsonFileSource().iter_items({}))

    def test_nonexistent_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            list(bl.JsonFileSource().iter_items({"path": "/nope/does/not/exist.json"}))

    def test_invalid_json_raises_value_error(self):
        path = os.path.join(self._tmp.name, "bad.json")
        Path(path).write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            list(bl.JsonFileSource().iter_items({"path": path}))

    def test_non_array_root_raises_value_error(self):
        path = os.path.join(self._tmp.name, "obj.json")
        Path(path).write_text(json.dumps({"items": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            list(bl.JsonFileSource().iter_items({"path": path}))

    def test_extra_fields_preserved_in_metadata(self):
        path = os.path.join(self._tmp.name, "tasks.json")
        _write_backlog_file(path, [
            {"id": "t1", "body": "x", "priority": "high", "owner": "alice"},
        ])
        items = list(bl.JsonFileSource().iter_items({"path": path}))
        self.assertEqual(items[0].metadata["priority"], "high")
        self.assertEqual(items[0].metadata["owner"], "alice")


# ============================================================
# Registry + custom sources
# ============================================================

class RegistryTests(unittest.TestCase):
    def setUp(self):
        bl._reset_for_tests()

    def tearDown(self):
        bl._reset_for_tests()

    def test_json_file_registered_by_default(self):
        ids = {s.id for s in bl.list_sources()}
        self.assertIn("json_file", ids)

    def test_register_custom_source(self):
        class FakeSource:
            id = "fake"
            description = "in-memory test source"

            def iter_items(self, config):
                for item in config.get("items") or []:
                    yield bl.BacklogItem(id=item["id"], body=item["body"])

        bl.register_backlog_source(FakeSource())
        self.assertIsNotNone(bl.get_source("fake"))

    def test_register_replaces_same_id(self):
        class A:
            id = "x"
            description = "first"
            def iter_items(self, config): yield from ()

        class B:
            id = "x"
            description = "second"
            def iter_items(self, config): yield from ()

        bl.register_backlog_source(A())
        bl.register_backlog_source(B())
        registered = bl.get_source("x")
        self.assertEqual(registered.description, "second")

    def test_register_with_empty_id_raises(self):
        class Bad:
            id = ""
            description = "no id"
            def iter_items(self, config): yield from ()

        with self.assertRaises(ValueError):
            bl.register_backlog_source(Bad())


# ============================================================
# pull_into_inbox
# ============================================================

class PullIntoInboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        bl._reset_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        bl._reset_for_tests()

    def _path(self, items):
        path = os.path.join(self._tmp.name, "tasks.json")
        _write_backlog_file(path, items)
        return path

    def test_pull_dispatches_each_item_as_a_task(self):
        path = self._path([
            {"id": "t1", "body": "Do A"},
            {"id": "t2", "body": "Do B"},
        ])
        result = bl.pull_into_inbox(
            "json_file", {"path": path},
            planner_session="planner", builder="coder",
        )
        self.assertEqual(result.requested, 2)
        self.assertEqual(result.pulled, 2)
        self.assertEqual(len(result.task_ids), 2)
        self.assertEqual(result.skipped_duplicate, 0)
        # Tasks visible on the builder's inbox.
        inbox = local_memory.get_inbox("coder", status="queued")
        self.assertEqual(len(inbox), 2)

    def test_pull_is_idempotent_on_re_pull(self):
        path = self._path([
            {"id": "t1", "body": "Do A"},
            {"id": "t2", "body": "Do B"},
        ])
        bl.pull_into_inbox(
            "json_file", {"path": path},
            planner_session="planner", builder="coder",
        )
        # Re-pull: both items skipped as duplicates.
        second = bl.pull_into_inbox(
            "json_file", {"path": path},
            planner_session="planner", builder="coder",
        )
        self.assertEqual(second.pulled, 0)
        self.assertEqual(second.skipped_duplicate, 2)

    def test_pull_uses_builder_hint_when_no_explicit_builder(self):
        local_memory.add_builder("planner", "writer")
        path = self._path([
            {"id": "t1", "body": "Code thing", "builder_hint": "coder"},
            {"id": "t2", "body": "Write thing", "builder_hint": "writer"},
        ])
        bl.pull_into_inbox(
            "json_file", {"path": path},
            planner_session="planner",
        )
        coder_inbox = local_memory.get_inbox("coder", status="queued")
        writer_inbox = local_memory.get_inbox("writer", status="queued")
        self.assertEqual(len(coder_inbox), 1)
        self.assertEqual(len(writer_inbox), 1)

    def test_pull_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            bl.pull_into_inbox(
                "ghost", {}, planner_session="planner", builder="coder",
            )

    def test_per_session_dedup_does_not_block_other_sessions(self):
        # Same source + same items, but session "planner-b" hasn't seen them.
        path = self._path([{"id": "t1", "body": "X"}])
        bl.pull_into_inbox(
            "json_file", {"path": path},
            planner_session="planner", builder="coder",
        )
        local_memory.add_builder("planner-b", "coder-b")
        result = bl.pull_into_inbox(
            "json_file", {"path": path},
            planner_session="planner-b", builder="coder-b",
        )
        self.assertEqual(result.pulled, 1)


# ============================================================
# Dispatcher
# ============================================================

class BacklogDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        local_memory.add_builder("planner", "coder")
        bl._reset_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        bl._reset_for_tests()

    def test_backlog_list(self):
        ev = dispatch_input("backlog list", session_id="planner")
        self.assertIn("json_file", ev["output"])

    def test_backlog_pull_unknown_source(self):
        ev = dispatch_input("backlog pull made_up_source", session_id="planner")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_backlog_pull_no_args(self):
        ev = dispatch_input("backlog pull", session_id="planner")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_backlog_pull_bad_kv(self):
        ev = dispatch_input(
            "backlog pull json_file not-key-value", session_id="planner",
        )
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_backlog_pull_missing_file_emits_invalid_argument(self):
        ev = dispatch_input(
            "backlog pull json_file path=/does/not/exist.json builder=coder",
            session_id="planner",
        )
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_backlog_pull_round_trip(self):
        path = os.path.join(self._tmp.name, "tasks.json")
        _write_backlog_file(path, [
            {"id": "task-A", "body": "Do A"},
            {"id": "task-B", "body": "Do B"},
        ])
        ev = dispatch_input(
            f"backlog pull json_file path={path} builder=coder",
            session_id="planner",
        )
        self.assertNotIn("reason", ev)
        self.assertEqual(ev["result"]["pulled"], 2)
        self.assertEqual(ev["result"]["requested"], 2)
        # Inbox now has the two tasks.
        self.assertEqual(len(local_memory.get_inbox("coder", status="queued")), 2)


if __name__ == "__main__":
    unittest.main()
