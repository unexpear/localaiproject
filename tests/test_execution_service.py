import unittest

from app.services.execution_service import (
    build_execution_graph,
    build_execution_record,
    format_execution_detail,
    format_execution_list,
)


class ExecutionServiceTests(unittest.TestCase):
    def test_build_execution_record_captures_replay_snapshot(self):
        record = build_execution_record(
            message="Explain that file too",
            status="completed",
            requested_lane="coding",
            resolved_lane="coding",
            model="qwen2.5-coder:7b",
            approved_workspace_result={
                "ok": True,
                "kind": "read",
                "files": [
                    {
                        "ok": True,
                        "relative_path": "app/supervisor/router.py",
                        "content_with_line_numbers": "1: def route_message(message):",
                        "truncated": False,
                    }
                ],
            },
            runtime_messages=[
                {"role": "system", "content": "Pinned workspace context"},
                {"role": "user", "content": "Explain that file too"},
            ],
        )

        self.assertEqual(record["approval"]["state"], "used")
        self.assertEqual(record["approval"]["description"], "read app/supervisor/router.py")
        self.assertEqual(record["replay"]["model"], "qwen2.5-coder:7b")
        self.assertEqual(record["replay"]["messages"][0]["role"], "system")
        self.assertEqual(record["replay"]["approved_workspace_result"]["kind"], "read")

    def test_format_execution_list_shows_retry_hint(self):
        rendered = format_execution_list(
            [
                {
                    "id": "exe_123",
                    "status": "completed",
                    "message": "Explain router",
                    "message_preview": "Explain router",
                    "resolved_lane": "coding",
                    "model": "qwen2.5-coder:7b",
                    "approval": {"state": "used", "description": "read app/router.py"},
                    "retry_source_execution_id": None,
                }
            ],
            session_id="default",
        )

        self.assertIn("exe_123", rendered)
        self.assertIn("approval read app/router.py", rendered)
        self.assertIn("retry <id> or retry last", rendered)

    def test_format_execution_detail_includes_flow_breakdown(self):
        rendered = format_execution_detail(
            {
                "id": "exe_123",
                "created_at": "2026-04-14T12:00:00Z",
                "status": "completed",
                "message": "Explain app/router.py",
                "message_preview": "Explain app/router.py",
                "requested_lane": "coding",
                "resolved_lane": "coding",
                "model": "qwen2.5-coder:7b",
                "route_confidence": "high",
                "route_primary_signal": "contains an explicit coding request",
                "inference_message_count": 5,
                "summary_included": True,
                "compaction_performed": False,
                "approval": {"state": "used", "description": "read app/router.py"},
                "replay": {
                    "messages": [{"role": "system", "content": "Pinned workspace context"}],
                    "approved_workspace_result": {
                        "ok": True,
                        "kind": "read",
                        "files": [{"ok": True, "relative_path": "app/router.py"}],
                    },
                },
            }
        )

        self.assertIn("Execution exe_123", rendered)
        self.assertIn("Replay Snapshot", rendered)
        self.assertIn("Flow", rendered)
        self.assertIn("Route: coding (high confidence)", rendered)
        self.assertIn("Workspace: read app/router.py", rendered)

    def test_build_execution_graph_returns_clickable_flow_nodes(self):
        graph = build_execution_graph(
            {
                "message": "Explain app/router.py",
                "message_preview": "Explain app/router.py",
                "requested_lane": "coding",
                "resolved_lane": "coding",
                "model": "qwen2.5-coder:7b",
                "route_confidence": "high",
                "route_primary_signal": "contains an explicit coding request",
                "approval": {"state": "used", "description": "read app/router.py"},
                "status": "completed",
                "replay": {},
            }
        )

        self.assertEqual([node["id"] for node in graph["nodes"]], ["input", "route", "model", "workspace", "result", "memory"])
        self.assertIn(("input", "route"), graph["edges"])
        self.assertEqual(graph["nodes"][1]["kind"], "route")


if __name__ == "__main__":
    unittest.main()
