"""Tests for recall memory Phase 1 (`docs/design/recall-memory.md`).

Four layers:
  - parse_relative_time edge cases
  - recall() — substring matching across sessions, since/session/limit filters
  - format_hits() — citation rendering
  - Dispatcher + MCP integration
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import recall_service as rc
from app.services.command_dispatcher import dispatch_input


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _seed_messages(session_id, messages):
    """Append a list of (role, content[, timestamp]) tuples."""
    for entry in messages:
        role, content, *rest = entry
        ts = rest[0] if rest else None
        local_memory.append_message(role, content, session_id=session_id)
        # Override timestamp if specified (append_message stamps with _now()).
        if ts:
            history = local_memory.get_history(session_id=session_id)
            history[-1]["timestamp"] = ts
            local_memory.replace_history(session_id=session_id, history=history)


# ============================================================
# parse_relative_time
# ============================================================

class ParseRelativeTimeTests(unittest.TestCase):
    def test_hours(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        result = rc.parse_relative_time("24h", now=now)
        self.assertTrue(result.startswith("2026-04-30T12:00"))

    def test_days(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        result = rc.parse_relative_time("7d", now=now)
        self.assertTrue(result.startswith("2026-04-24T12:00"))

    def test_weeks(self):
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        result = rc.parse_relative_time("1w", now=now)
        self.assertTrue(result.startswith("2026-04-24"))

    def test_minutes(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        result = rc.parse_relative_time("30m", now=now)
        self.assertTrue(result.startswith("2026-05-01T11:30"))

    def test_iso_string_passthrough(self):
        result = rc.parse_relative_time("2026-04-21T15:30:00Z")
        self.assertTrue(result.startswith("2026-04-21T15:30"))

    def test_iso_date_only(self):
        result = rc.parse_relative_time("2026-04-21")
        self.assertTrue(result.startswith("2026-04-21"))

    def test_invalid_raises(self):
        with self.assertRaises(rc.RecallTimeError):
            rc.parse_relative_time("garbage")

    def test_empty_raises(self):
        with self.assertRaises(rc.RecallTimeError):
            rc.parse_relative_time("")

    def test_uppercase_unit_works(self):
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        result = rc.parse_relative_time("24H", now=now)
        self.assertTrue(result.startswith("2026-04-30T12:00"))


# ============================================================
# recall()
# ============================================================

class RecallSearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_empty_query_returns_no_hits(self):
        _seed_messages("s1", [("user", "hello")])
        self.assertEqual(rc.recall(""), [])

    def test_finds_substring_match(self):
        _seed_messages("s1", [
            ("user", "investigate the auth bug in JWT decoder"),
            ("assistant", "the issue is decode_token"),
        ])
        hits = rc.recall("auth bug")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].session_id, "s1")
        self.assertIn("auth", hits[0].matched_terms)
        self.assertIn("bug", hits[0].matched_terms)
        # Follow-up populated with the assistant message.
        self.assertIsNotNone(hits[0].follow_up)
        self.assertEqual(hits[0].follow_up["role"], "assistant")

    def test_search_is_case_insensitive(self):
        _seed_messages("s1", [("user", "FIX the BUG")])
        hits = rc.recall("bug")
        self.assertEqual(len(hits), 1)

    def test_searches_all_sessions_by_default(self):
        _seed_messages("s1", [("user", "cat in s1")])
        _seed_messages("s2", [("user", "dog in s2")])
        _seed_messages("s3", [("user", "cat in s3")])
        hits = rc.recall("cat")
        sids = {h.session_id for h in hits}
        self.assertEqual(sids, {"s1", "s3"})

    def test_session_filter_restricts_to_one(self):
        _seed_messages("s1", [("user", "shared keyword")])
        _seed_messages("s2", [("user", "shared keyword")])
        hits = rc.recall("shared", session="s1")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].session_id, "s1")

    def test_since_filter_excludes_old_messages(self):
        # Seed two messages with explicit timestamps.
        old_ts = "2026-01-01T00:00:00Z"
        new_ts = "2026-05-01T00:00:00Z"
        _seed_messages("s1", [
            ("user", "ancient bug", old_ts),
            ("user", "recent bug", new_ts),
        ])
        # Cutoff between them: only the "recent" hit returns.
        hits = rc.recall("bug", since="2026-03-01T00:00:00Z")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].content, "recent bug")

    def test_limit_caps_result_count(self):
        _seed_messages("s1", [
            ("user", f"keyword msg {i}") for i in range(20)
        ])
        hits = rc.recall("keyword", limit=5)
        self.assertEqual(len(hits), 5)

    def test_score_ranks_more_matches_higher(self):
        _seed_messages("s1", [
            ("user", "bar foo"),
            ("user", "bar bar foo"),  # repeated 'bar' → bonus
            ("user", "qux"),
        ])
        hits = rc.recall("bar foo")
        # Both messages match "bar" + "foo"; the second wins on
        # occurrence bonus.
        self.assertEqual(hits[0].content, "bar bar foo")

    def test_no_matches_returns_empty(self):
        _seed_messages("s1", [("user", "hello world")])
        self.assertEqual(rc.recall("nonexistent"), [])

    def test_multiword_query_matches_any_term(self):
        _seed_messages("s1", [
            ("user", "this contains apple"),
            ("user", "this contains banana"),
            ("user", "this contains nothing relevant"),
        ])
        hits = rc.recall("apple banana")
        # Both messages with apple OR banana should match.
        contents = sorted(h.content for h in hits)
        self.assertEqual(contents, [
            "this contains apple", "this contains banana",
        ])


# ============================================================
# format_hits
# ============================================================

class FormatHitsTests(unittest.TestCase):
    def test_zero_hits_message(self):
        result = rc.format_hits([], "auth bug")
        self.assertIn("0 hits", result)
        self.assertIn("auth bug", result)

    def test_one_hit_renders_citation(self):
        hit = rc.RecallHit(
            session_id="default",
            message_index=0,
            role="user",
            content="investigate the auth bug",
            timestamp="2026-04-21T15:30:00Z",
            score=2.0,
        )
        result = rc.format_hits([hit], "auth bug")
        self.assertIn("session=default", result)
        self.assertIn("user", result)
        self.assertIn("investigate the auth bug", result)
        self.assertIn("2026-04-21", result)

    def test_long_content_is_truncated(self):
        long_content = "x" * 1000
        hit = rc.RecallHit(
            session_id="s1", message_index=0, role="user",
            content=long_content, timestamp=None, score=1.0,
        )
        result = rc.format_hits([hit], "x")
        self.assertLess(len(result), 600)  # citation block is bounded
        self.assertIn("...", result)

    def test_follow_up_renders_with_arrow(self):
        hit = rc.RecallHit(
            session_id="s1", message_index=0, role="user",
            content="investigate", timestamp=None, score=1.0,
            follow_up={"role": "assistant", "content": "the issue is here"},
        )
        result = rc.format_hits([hit], "investigate")
        self.assertIn("assistant", result)
        self.assertIn("the issue is here", result)


# ============================================================
# Dispatcher
# ============================================================

class RecallDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_recall_with_no_query_emits_invalid_argument(self):
        ev = dispatch_input("recall", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_recall_round_trip(self):
        _seed_messages("s1", [
            ("user", "investigate the auth bug"),
            ("assistant", "the issue is decode_token"),
        ])
        ev = dispatch_input("recall auth bug", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertEqual(len(ev["result"]["hits"]), 1)
        self.assertIn("auth bug", ev["output"])

    def test_recall_with_since_flag(self):
        old_ts = "2026-01-01T00:00:00Z"
        new_ts = "2026-05-01T00:00:00Z"
        _seed_messages("s1", [
            ("user", "ancient apple", old_ts),
            ("user", "recent apple", new_ts),
        ])
        # Cutoff in March: only "recent apple" returns.
        ev = dispatch_input(
            "recall apple --since 2026-03-01T00:00:00Z",
            session_id="default",
        )
        self.assertEqual(len(ev["result"]["hits"]), 1)
        self.assertIn("recent apple", ev["result"]["hits"][0]["content"])

    def test_recall_with_session_flag(self):
        _seed_messages("s1", [("user", "shared keyword in s1")])
        _seed_messages("s2", [("user", "shared keyword in s2")])
        ev = dispatch_input(
            "recall keyword --session s1", session_id="default",
        )
        self.assertEqual(len(ev["result"]["hits"]), 1)
        self.assertEqual(ev["result"]["hits"][0]["session_id"], "s1")

    def test_recall_with_limit_flag(self):
        _seed_messages("s1", [
            ("user", f"keyword msg {i}") for i in range(20)
        ])
        ev = dispatch_input("recall keyword --limit 3", session_id="default")
        self.assertEqual(len(ev["result"]["hits"]), 3)

    def test_recall_with_invalid_since_emits_invalid_argument(self):
        ev = dispatch_input(
            "recall keyword --since nonsense", session_id="default",
        )
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_recall_with_invalid_limit_emits_invalid_argument(self):
        ev = dispatch_input(
            "recall keyword --limit notanumber", session_id="default",
        )
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_recall_zero_hits_returns_ok_with_zero_hits_text(self):
        _seed_messages("s1", [("user", "hello")])
        ev = dispatch_input(
            "recall doesnotexist", session_id="default",
        )
        self.assertNotIn("reason", ev)
        self.assertEqual(len(ev["result"]["hits"]), 0)
        self.assertIn("0 hit", ev["output"])

    def test_recall_dispatch_uses_default_limit_10(self):
        _seed_messages("s1", [
            ("user", f"keyword {i}") for i in range(15)
        ])
        ev = dispatch_input("recall keyword", session_id="default")
        self.assertEqual(len(ev["result"]["hits"]), 10)


# ============================================================
# MCP tool
# ============================================================

class RecallMcpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def _drive(self, messages):
        import io
        from app.mcp import server as mcp_server
        reader = io.StringIO()
        for msg in messages:
            reader.write(json.dumps(msg) + "\n")
        reader.seek(0)
        writer = io.StringIO()
        mcp_server.serve(reader, writer)
        writer.seek(0)
        return [json.loads(line) for line in writer if line.strip()]

    def test_recall_appears_in_tools_list(self):
        responses = self._drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ])
        names = {t["name"] for t in responses[0]["result"]["tools"]}
        self.assertIn("recall", names)

    def test_recall_tool_call_round_trip(self):
        _seed_messages("s1", [
            ("user", "investigate the auth bug"),
        ])
        responses = self._drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "recall",
                        "arguments": {"query": "auth bug"}}},
        ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError", False))
        # structuredContent has the dispatcher's `result` block.
        self.assertEqual(
            result["structuredContent"]["result"]["query"],
            "auth bug",
        )
        self.assertEqual(
            len(result["structuredContent"]["result"]["hits"]),
            1,
        )

    def test_recall_tool_call_missing_query_returns_iserror(self):
        responses = self._drive([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "recall", "arguments": {}}},
        ])
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["reason"], "invalid_argument",
        )


if __name__ == "__main__":
    unittest.main()
