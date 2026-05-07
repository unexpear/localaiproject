import copy
import unittest
from unittest.mock import patch

from app.services.chat_service import _build_runtime_messages, handle_chat, retry_saved_execution


class ChatServiceTests(unittest.TestCase):
    def test_build_runtime_messages_appends_current_user_message(self):
        history = [
            {"role": "user", "content": "Call the project Nebula Forge."},
            {"role": "assistant", "content": "Got it."},
        ]
        messages = _build_runtime_messages({}, history, "What should I call it?")
        # First message is the control system prompt
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("local desktop AI control layer", messages[0]["content"])
        # Last message is the current user turn
        self.assertEqual(messages[-1], {"role": "user", "content": "What should I call it?"})
        # History is included verbatim
        self.assertIn({"role": "user", "content": "Call the project Nebula Forge."}, messages)
        self.assertIn({"role": "assistant", "content": "Got it."}, messages)

    def test_build_runtime_messages_includes_structured_summary(self):
        summary = {"facts": ["Project codename: Nebula Forge"], "preferences": [], "open_tasks": []}
        messages = _build_runtime_messages(summary, [], "What should I call it?")
        self.assertEqual(messages[0]["role"], "system")
        # The summary system message lives somewhere in the system block, not at a fixed index.
        rendered = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertIn("Facts:", rendered)
        self.assertIn("Nebula Forge", rendered)
        self.assertEqual(messages[-1], {"role": "user", "content": "What should I call it?"})

    def test_build_runtime_messages_includes_workspace_and_skills(self):
        messages = _build_runtime_messages(
            {},
            [],
            "Keep going.",
            session_context={
                "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
                "active_skills": ["builder", "safe"],
            },
        )
        rendered = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("Active operating skills for this session", rendered)
        self.assertIn("Pinned workspace context for this session", rendered)
        self.assertIn("builder", rendered)
        self.assertIn("localaiproject", rendered)

    def test_build_runtime_messages_includes_approved_workspace_inspection(self):
        messages = _build_runtime_messages(
            {},
            [],
            "Explain it.",
            session_context={"workspace_path": None, "active_skills": []},
            approved_workspace_result={
                "ok": True,
                "kind": "read",
                "files": [
                    {
                        "ok": True,
                        "relative_path": "app/supervisor/router.py",
                        "content_with_line_numbers": "1: def route_message(message):\n2:     pass",
                        "truncated": False,
                    }
                ],
            },
        )
        rendered = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
        self.assertIn("Approved workspace inspection results", rendered)
        self.assertIn("app/supervisor/router.py", rendered)

    def test_handle_chat_requests_approval_for_workspace_action(self):
        pending_action = {
            "kind": "read",
            "origin": "chat",
            "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
            "paths": ["app/supervisor/router.py"],
            "reason": "your message referenced pinned workspace files",
        }

        with patch(
            "app.services.chat_service.local_memory.get_session_context",
            return_value={"workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject", "active_skills": []},
        ), patch("app.services.chat_service.plan_workspace_action", return_value=pending_action), patch(
            "app.services.chat_service.local_memory.append_message"
        ) as append_message_mock, patch(
            "app.services.chat_service.local_memory.append_execution"
        ) as append_execution_mock:
            result = handle_chat("Explain app/supervisor/router.py", session_id="test-session")

        self.assertTrue(result["approval_required"])
        self.assertEqual(result["pending_action"]["kind"], "read")
        self.assertIn("approve", result["response"])
        append_message_mock.assert_not_called()
        append_execution_mock.assert_called_once()

    def test_handle_chat_uses_last_approved_search_for_follow_up_file_pick(self):
        with patch(
            "app.services.chat_service.local_memory.get_session_context",
            return_value={
                "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
                "active_skills": [],
                "approved_search": {
                    "query": "route_message",
                    "files": ["app/supervisor/router.py", "app/services/chat_service.py"],
                },
            },
        ), patch("app.services.chat_service.local_memory.append_message") as append_message_mock, patch(
            "app.services.chat_service.local_memory.append_execution"
        ) as append_execution_mock:
            result = handle_chat("Explain that file too", session_id="test-session")

        self.assertTrue(result["approval_required"])
        self.assertEqual(result["pending_action"]["kind"], "read")
        self.assertEqual(result["pending_action"]["paths"], ["app/supervisor/router.py"])
        append_message_mock.assert_not_called()
        append_execution_mock.assert_called_once()

    def test_handle_chat_sends_recent_history_to_runtime(self):
        router_history = [
            {"role": "user", "content": "Call the project Nebula Forge."},
            {"role": "assistant", "content": "Okay, I will."},
        ]
        route_info = {
            "lane": "reasoning",
            "confidence": "medium",
            "scores": {"coding": 0, "vision": 0, "reasoning": 4},
            "primary_signal": "recent user context favored reasoning",
            "context_used": True,
            "context_reason": "follow-up prompt, ambiguous current prompt",
        }
        resolution = {
            "requested_role": "reasoning",
            "resolved_role": "reasoning",
            "model": "llama3.1:8b",
            "fallback_used": False,
        }
        inference_result = {
            "ok": True,
            "content": "You said to call it Nebula Forge.",
            "error": None,
            "raw": None,
        }
        summary = {"facts": ["Project codename: Nebula Forge"], "preferences": [], "open_tasks": []}

        with patch("app.services.chat_service.local_memory.get_history", side_effect=[router_history, router_history]), patch(
            "app.services.chat_service.local_memory.get_session_context",
            return_value={"workspace_path": None, "active_skills": []},
        ), patch(
            "app.services.chat_service.local_memory.append_message"
        ), patch("app.services.chat_service.local_memory.append_execution"), patch(
            "app.services.chat_service.router.route_message", return_value=route_info
        ), patch(
            "app.services.chat_service.model_registry.resolve_model_for_role", return_value=resolution
        ), patch(
            "app.services.chat_service.runtime_client.infer_chat", return_value=inference_result
        ) as infer_chat_mock, patch(
            "app.services.chat_service.compaction_service.get_compact_summary", return_value=summary
        ), patch(
            "app.services.chat_service.compaction_service.maybe_compact_session", return_value={"performed": False}
        ):
            result = handle_chat("What should I call it?", session_id="test-session")

        runtime_messages = infer_chat_mock.call_args.kwargs["messages"]
        self.assertEqual(runtime_messages[0]["role"], "system")
        self.assertIn("local desktop AI control layer", runtime_messages[0]["content"])
        rendered_systems = "\n\n".join(m["content"] for m in runtime_messages if m["role"] == "system")
        self.assertIn("Project codename: Nebula Forge", rendered_systems)
        self.assertEqual(runtime_messages[-1], {"role": "user", "content": "What should I call it?"})
        self.assertEqual(result["response"], "You said to call it Nebula Forge.")
        self.assertEqual(result["model"], "llama3.1:8b")

    def test_handle_chat_returns_auto_compaction_summary(self):
        route_info = {
            "lane": "reasoning",
            "confidence": "low",
            "scores": {"coding": 0, "vision": 0, "reasoning": 1},
            "primary_signal": "default general-purpose lane",
            "context_used": False,
            "context_reason": None,
        }
        resolution = {
            "requested_role": "reasoning",
            "resolved_role": "reasoning",
            "model": "llama3.1:8b",
            "fallback_used": False,
        }
        inference_result = {
            "ok": True,
            "content": "Moon Orchard.",
            "error": None,
            "raw": None,
        }
        compact_result = {
            "performed": True,
            "method": "model",
            "model": "llama3.1:8b",
            "compacted_messages": 4,
            "kept_messages": 6,
            "summary": {
                "facts": ["Launch codename: Moon Orchard"],
                "preferences": [],
                "open_tasks": ["Add desktop GUI"],
            },
        }

        with patch("app.services.chat_service.local_memory.get_history", side_effect=[[], []]), patch(
            "app.services.chat_service.local_memory.get_session_context",
            return_value={"workspace_path": None, "active_skills": []},
        ), patch(
            "app.services.chat_service.local_memory.append_message"
        ), patch("app.services.chat_service.local_memory.append_execution"), patch(
            "app.services.chat_service.router.route_message", return_value=route_info
        ), patch(
            "app.services.chat_service.model_registry.resolve_model_for_role", return_value=resolution
        ), patch(
            "app.services.chat_service.runtime_client.infer_chat", return_value=inference_result
        ), patch(
            "app.services.chat_service.compaction_service.get_compact_summary", return_value={}
        ), patch(
            "app.services.chat_service.compaction_service.maybe_compact_session", return_value=compact_result
        ):
            result = handle_chat("What is the launch codename?", session_id="test-session")

        self.assertTrue(result["compaction_performed"])
        self.assertIn("Launch codename: Moon Orchard", result["compaction_summary_text"])
        self.assertIn("Compacted 4 older messages", result["compaction_message"])

    def test_retry_saved_execution_uses_saved_runtime_state(self):
        execution = {
            "id": "exe_123",
            "message": "What should I call it?",
            "requested_lane": "reasoning",
            "resolved_lane": "reasoning",
            "fallback_used": False,
            "replay": {
                "model": "llama3.1:8b",
                "messages": [
                    {"role": "system", "content": "You are a local desktop AI control layer."},
                    {"role": "user", "content": "What should I call it?"},
                ],
                "approved_workspace_result": None,
            },
        }
        inference_result = {
            "ok": True,
            "content": "Nebula Forge.",
            "error": None,
            "raw": None,
        }

        with patch(
            "app.services.chat_service.local_memory.get_session_context",
            return_value={"workspace_path": None, "active_skills": []},
        ), patch(
            "app.services.chat_service.local_memory.append_message"
        ), patch(
            "app.services.chat_service.local_memory.append_execution"
        ) as append_execution_mock, patch(
            "app.services.chat_service.runtime_client.infer_chat",
            return_value=inference_result,
        ) as infer_chat_mock, patch(
            "app.services.chat_service.compaction_service.get_compact_summary",
            return_value={},
        ), patch(
            "app.services.chat_service.compaction_service.maybe_compact_session",
            return_value={"performed": False},
        ):
            result = retry_saved_execution(copy.deepcopy(execution), session_id="test-session")

        infer_chat_mock.assert_called_once_with(
            "llama3.1:8b",
            messages=execution["replay"]["messages"],
        )
        self.assertEqual(result["response"], "Nebula Forge.")
        self.assertEqual(result["retried_from_execution_id"], "exe_123")
        self.assertEqual(result["retry_mode"], "exact")
        append_execution_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
