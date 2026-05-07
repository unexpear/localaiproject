"""Tests for `search_executions` across both storage backends + dispatcher.

The same fixture data is loaded into each backend and the same set of
filter scenarios is exercised, proving the SQL implementation and the
linear-scan implementation honour the same contract.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from app.memory import local_memory
from app.memory.duckdb_storage import DuckDbStorage
from app.memory.json_storage import JsonStorage
from app.services.command_dispatcher import dispatch_input, _parse_executions_args


_FIXTURE = {
    "sessions": {
        "default": {
            "history": [],
            "executions": [
                {
                    "id": "exe_001",
                    "requested_lane": "reasoning",
                    "resolved_lane": "reasoning",
                    "status": "ready",
                    "message": "explain workspace approval",
                    "message_preview": "explain workspace approval",
                    "created_at": "2026-04-29T10:00:00Z",
                },
                {
                    "id": "exe_002",
                    "requested_lane": "coding",
                    "resolved_lane": "coding",
                    "status": "needs_approval",
                    "message": "read app/services/router.py",
                    "message_preview": "read app/services/router.py",
                    "created_at": "2026-04-30T11:30:00Z",
                },
                {
                    "id": "exe_003",
                    "requested_lane": "coding",
                    "resolved_lane": "coding",
                    "status": "ready",
                    "message": "refactor _pull_with_retry callback",
                    "message_preview": "refactor _pull_with_retry callback",
                    "created_at": "2026-04-30T14:15:00Z",
                },
                {
                    "id": "exe_004",
                    "requested_lane": "vision",
                    "resolved_lane": "vision",
                    "status": "blocked",
                    "message": "describe screenshot of settings",
                    "message_preview": "describe screenshot of settings",
                    "created_at": "2026-05-01T09:05:00Z",
                },
            ],
            "summary": {"facts": [], "preferences": [], "open_tasks": []},
            "context": {"workspace_path": None, "active_skills": []},
        }
    }
}


def _backend(kind, tmp_dir):
    if kind == "json":
        return JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    if kind == "duckdb":
        return DuckDbStorage(path=os.path.join(tmp_dir, "memory.duckdb"))
    raise ValueError(kind)


class _SearchContractMixin:
    kind = "json"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = _backend(self.kind, self._tmp.name)
        self.storage.write_store(_FIXTURE)

    def tearDown(self):
        if hasattr(self.storage, "close"):
            self.storage.close()
        self._tmp.cleanup()

    def test_no_filters_returns_newest_first(self):
        results = self.storage.search_executions(session_id="default")
        ids = [e["id"] for e in results]
        self.assertEqual(ids, ["exe_004", "exe_003", "exe_002", "exe_001"])

    def test_query_substring_filter_is_case_insensitive(self):
        results = self.storage.search_executions(session_id="default", query="ROUTER")
        self.assertEqual([e["id"] for e in results], ["exe_002"])

    def test_lane_filter_matches_requested_or_resolved(self):
        results = self.storage.search_executions(session_id="default", lane="coding")
        self.assertEqual({e["id"] for e in results}, {"exe_002", "exe_003"})

    def test_status_filter(self):
        results = self.storage.search_executions(session_id="default", status="blocked")
        self.assertEqual([e["id"] for e in results], ["exe_004"])

    def test_since_filter_drops_older_rows(self):
        results = self.storage.search_executions(
            session_id="default", since="2026-04-30T00:00:00Z"
        )
        self.assertEqual({e["id"] for e in results}, {"exe_002", "exe_003", "exe_004"})

    def test_combined_filters_AND_together(self):
        results = self.storage.search_executions(
            session_id="default", lane="coding", status="ready"
        )
        self.assertEqual([e["id"] for e in results], ["exe_003"])

    def test_limit_is_honoured(self):
        results = self.storage.search_executions(session_id="default", limit=2)
        self.assertEqual(len(results), 2)
        # Newest first
        self.assertEqual(results[0]["id"], "exe_004")

    def test_unknown_session_returns_empty(self):
        results = self.storage.search_executions(session_id="ghost", query="anything")
        self.assertEqual(results, [])


class JsonSearchTests(_SearchContractMixin, unittest.TestCase):
    kind = "json"


class DuckDbSearchTests(_SearchContractMixin, unittest.TestCase):
    kind = "duckdb"


class DuckDbIndexInvalidationTests(unittest.TestCase):
    def test_overwrite_replaces_index_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDbStorage(path=os.path.join(tmp, "memory.duckdb"))
            try:
                storage.write_store(_FIXTURE)
                self.assertEqual(len(storage.search_executions(query="router")), 1)

                # Overwrite with no executions; index must be empty.
                storage.write_store({"sessions": {"default": {"history": [], "executions": []}}})
                self.assertEqual(storage.search_executions(query="router"), [])
            finally:
                storage.close()


class DispatcherSearchTests(unittest.TestCase):
    def test_parse_args_legacy_n(self):
        args = _parse_executions_args("executions 12")
        self.assertEqual(args["limit"], 12)
        self.assertFalse(args["filters_used"])

    def test_parse_args_clamps_to_max_limit(self):
        # EXECUTION_LIST_MAX_LIMIT is 25; anything over should clamp.
        args = _parse_executions_args("executions 999")
        self.assertEqual(args["limit"], 25)

    def test_parse_args_search_query(self):
        args = _parse_executions_args("executions search router.py")
        self.assertEqual(args["query"], "router.py")
        self.assertTrue(args["filters_used"])

    def test_parse_args_filters_combined(self):
        args = _parse_executions_args("executions search workspace lane=coding status=ready limit=5")
        self.assertEqual(args["query"], "workspace")
        self.assertEqual(args["lane"], "coding")
        self.assertEqual(args["status"], "ready")
        self.assertEqual(args["limit"], 5)
        self.assertTrue(args["filters_used"])

    def test_parse_args_lane_only_no_query(self):
        args = _parse_executions_args("executions lane=coding")
        self.assertIsNone(args["query"])
        self.assertEqual(args["lane"], "coding")
        self.assertTrue(args["filters_used"])

    def test_dispatch_executions_search_calls_search_function(self):
        fake_results = [{"id": "exe_x", "status": "ready", "resolved_lane": "coding"}]
        with patch(
            "app.services.command_dispatcher.local_memory.search_executions",
            return_value=fake_results,
        ) as search_mock, patch(
            "app.services.command_dispatcher.format_execution_list",
            return_value="(formatted)",
        ):
            event = dispatch_input("executions search router.py lane=coding", session_id="default")

        search_mock.assert_called_once()
        kwargs = search_mock.call_args.kwargs
        self.assertEqual(kwargs.get("query"), "router.py")
        self.assertEqual(kwargs.get("lane"), "coding")
        self.assertEqual(event["action"], "executions")
        self.assertIn("Executions search", event["output"])
        self.assertIn("query=\"router.py\"", event["output"])
        self.assertIn("lane=coding", event["output"])
        self.assertIn("1 match", event["output"])

    def test_dispatch_executions_legacy_still_works(self):
        with patch(
            "app.services.command_dispatcher.local_memory.get_executions",
            return_value=[],
        ) as get_mock, patch(
            "app.services.command_dispatcher.format_execution_list",
            return_value="(empty)",
        ):
            event = dispatch_input("executions 7", session_id="default")
        get_mock.assert_called_once()
        self.assertEqual(event["action"], "executions")


if __name__ == "__main__":
    unittest.main()
