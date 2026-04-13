import unittest
from unittest.mock import patch

from app.services.command_dispatcher import _format_chat_result, dispatch_input


class CommandDispatcherTests(unittest.TestCase):
    def test_format_chat_result_includes_auto_compaction_summary(self):
        result = {
            "response": "Moon Orchard",
            "fallback_used": False,
            "requested_lane": "reasoning",
            "resolved_lane": "reasoning",
            "model": "llama3.1:8b",
            "compaction_performed": True,
            "compaction_message": "Compacted 4 older messages into structured session memory.",
            "compaction_summary_text": "Facts:\n- Launch codename: Moon Orchard\n\nOpen Tasks:\n- Add desktop GUI",
        }

        rendered = _format_chat_result(result)

        self.assertIn("Moon Orchard", rendered)
        self.assertIn("[auto-compacted session memory]", rendered)
        self.assertIn("Compacted 4 older messages", rendered)
        self.assertIn("Launch codename: Moon Orchard", rendered)

    def test_workspace_command_sets_session_workspace(self):
        with patch(
            "app.services.command_dispatcher.resolve_workspace_path",
            return_value=("C:\\Users\\micha\\Desktop\\localaiproject", None),
        ), patch("app.services.command_dispatcher.local_memory.set_workspace") as set_workspace_mock, patch(
            "app.services.command_dispatcher.local_memory.clear_pending_approval"
        ), patch(
            "app.services.command_dispatcher.local_memory.clear_approved_search"
        ), patch(
            "app.services.command_dispatcher.render_workspace_status",
            return_value="Workspace: C:\\Users\\micha\\Desktop\\localaiproject",
        ):
            event = dispatch_input("workspace C:\\Users\\micha\\Desktop\\localaiproject", session_id="default")

        set_workspace_mock.assert_called_once_with(
            session_id="default",
            workspace_path="C:\\Users\\micha\\Desktop\\localaiproject",
        )
        self.assertEqual(event["action"], "workspace")
        self.assertIn("Workspace:", event["output"])

    def test_skill_add_command_enables_skill(self):
        with patch(
            "app.services.command_dispatcher.local_memory.get_session_context",
            return_value={"workspace_path": None, "active_skills": []},
        ), patch("app.services.command_dispatcher.local_memory.set_active_skills") as set_active_skills_mock:
            event = dispatch_input("skill add builder", session_id="default")

        set_active_skills_mock.assert_called_once_with(session_id="default", skills=["builder"])
        self.assertEqual(event["action"], "skills")
        self.assertIn("Enabled skill 'builder'.", event["output"])

    def test_read_command_queues_approval(self):
        with patch(
            "app.services.command_dispatcher.local_memory.get_session_context",
            return_value={"workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject", "active_skills": []},
        ), patch("app.services.command_dispatcher.local_memory.set_pending_approval") as set_pending_mock:
            event = dispatch_input("read app\\supervisor\\router.py", session_id="default")

        set_pending_mock.assert_called_once()
        self.assertEqual(event["action"], "approval")
        self.assertIn("[approval required]", event["output"])
        self.assertIn("router.py", event["output"])

    def test_approve_command_executes_pending_search(self):
        pending_action = {
            "kind": "search",
            "origin": "command",
            "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
            "query": "route_message",
        }
        search_result = {
            "ok": True,
            "kind": "search",
            "query": "route_message",
            "matches": [{"relative_path": "app/supervisor/router.py", "line_number": 10, "line": "def route_message(message):"}],
            "files_with_matches": ["app/supervisor/router.py"],
            "truncated": False,
        }

        with patch(
            "app.services.command_dispatcher.local_memory.get_pending_approval",
            return_value=pending_action,
        ), patch("app.services.command_dispatcher.local_memory.clear_pending_approval"), patch(
            "app.services.command_dispatcher.local_memory.set_approved_search",
        ) as set_approved_search_mock, patch(
            "app.services.command_dispatcher.search_workspace",
            return_value=search_result,
        ):
            event = dispatch_input("approve", session_id="default")

        set_approved_search_mock.assert_called_once_with(
            session_id="default",
            approved_search={
                "query": "route_message",
                "files": ["app/supervisor/router.py"],
            },
        )
        self.assertEqual(event["action"], "approve")
        self.assertIn("[approved workspace search]", event["output"])
        self.assertIn("route_message", event["output"])

    def test_chat_approval_request_is_saved_to_session_context(self):
        pending_action = {
            "kind": "read",
            "origin": "chat",
            "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
            "paths": ["app/supervisor/router.py"],
            "original_message": "Explain app/supervisor/router.py",
        }
        chat_result = {
            "ok": False,
            "response": "[approval required]",
            "fallback_used": False,
            "approval_required": True,
            "pending_action": pending_action,
            "workspace_action_message": "read app/supervisor/router.py",
            "compaction_performed": False,
        }

        with patch("app.services.command_dispatcher.handle_chat", return_value=chat_result), patch(
            "app.services.command_dispatcher.local_memory.set_pending_approval"
        ) as set_pending_mock:
            event = dispatch_input("Explain app/supervisor/router.py", session_id="default")

        set_pending_mock.assert_called_once_with(session_id="default", pending_approval=pending_action)
        self.assertEqual(event["action"], "chat")
        self.assertIn("[approval required]", event["output"])


if __name__ == "__main__":
    unittest.main()
