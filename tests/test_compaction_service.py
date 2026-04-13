import unittest
from unittest.mock import patch

from app.services.compaction_service import (
    build_summary_system_message,
    maybe_compact_session,
    render_compact_summary,
)


class CompactionServiceTests(unittest.TestCase):
    def test_build_summary_system_message_includes_summary_sections(self):
        summary = {
            "facts": ["User works on a local AI project."],
            "preferences": ["Prefers concise answers."],
            "open_tasks": ["Add a GUI."],
        }
        message = build_summary_system_message(summary)
        self.assertEqual(message["role"], "system")
        self.assertIn("Facts:", message["content"])
        self.assertIn("Preferences:", message["content"])
        self.assertIn("Open Tasks:", message["content"])

    def test_render_compact_summary_formats_sections(self):
        rendered = render_compact_summary(
            {
                "facts": ["Fact A"],
                "preferences": ["Preference A"],
                "open_tasks": ["Task A"],
            }
        )
        self.assertIn("Facts:", rendered)
        self.assertIn("Preferences:", rendered)
        self.assertIn("Open Tasks:", rendered)

    def test_maybe_compact_session_summarizes_and_trims_history(self):
        history = [
            {"role": "user", "content": "I prefer concise answers."},
            {"role": "assistant", "content": "Okay, concise answers."},
            {"role": "user", "content": "An open task is adding a desktop GUI."},
            {"role": "assistant", "content": "Noted. Add a desktop GUI."},
            {"role": "user", "content": "Fact C"},
            {"role": "assistant", "content": "Noted C"},
            {"role": "user", "content": "Fact D"},
            {"role": "assistant", "content": "Noted D"},
            {"role": "user", "content": "Fact E"},
            {"role": "assistant", "content": "Noted E"},
        ]
        session = {
            "history": history,
            "summary": {"facts": [], "preferences": [], "open_tasks": [], "updated_at": None, "compacted_messages": 0},
        }

        with patch("app.services.compaction_service.local_memory.get_session", return_value=session), patch(
            "app.services.compaction_service.local_memory.update_session_summary"
        ) as update_summary_mock, patch(
            "app.services.compaction_service.local_memory.replace_history"
        ) as replace_history_mock, patch(
            "app.services.compaction_service.model_registry.resolve_model_for_role",
            return_value={"model": "llama3.1:8b"},
        ), patch(
            "app.services.compaction_service.runtime_client.infer_chat",
            return_value={
                "ok": True,
                "content": '{"facts":["Remembered fact"],"preferences":[],"open_tasks":[]}',
                "error": None,
                "raw": None,
            },
        ):
            result = maybe_compact_session(session_id="test", force=True)

        self.assertTrue(result["performed"])
        self.assertEqual(result["summary"]["preferences"], ["I prefer concise answers."])
        self.assertEqual(result["summary"]["open_tasks"], ["An open task is adding a desktop GUI."])
        update_summary_mock.assert_called_once()
        replace_history_mock.assert_called_once()
        kept_history = replace_history_mock.call_args.kwargs["history"]
        self.assertEqual(len(kept_history), 6)


if __name__ == "__main__":
    unittest.main()
