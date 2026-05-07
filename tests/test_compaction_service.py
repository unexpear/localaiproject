import unittest
from unittest.mock import patch

from app.services.compaction_service import (
    SUMMARY_SYSTEM_PROMPT,
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


class CompactionGroundingTests(unittest.TestCase):
    """Verify the model-as-primary, heuristic-as-fallback grounding
    introduced 2026-05-07 (was previously inverted, blocking model
    improvements)."""

    def _session_with_history(self, history):
        return {
            "history": history,
            "summary": {
                "facts": [], "preferences": [], "open_tasks": [],
                "updated_at": None, "compacted_messages": 0,
            },
        }

    def _run_with_model_response(self, history, model_content):
        session = self._session_with_history(history)
        with patch(
            "app.services.compaction_service.local_memory.get_session",
            return_value=session,
        ), patch(
            "app.services.compaction_service.local_memory.update_session_summary",
        ), patch(
            "app.services.compaction_service.local_memory.replace_history",
        ), patch(
            "app.services.compaction_service.model_registry.resolve_model_for_role",
            return_value={"model": "llama3.1:8b"},
        ), patch(
            "app.services.compaction_service.runtime_client.infer_chat",
            return_value={
                "ok": True,
                "content": model_content,
                "error": None,
                "raw": None,
            },
        ):
            return maybe_compact_session(session_id="test", force=True)

    def test_model_facts_win_over_heuristic_facts(self):
        # User message that the heuristic would classify as a fact.
        history = [
            {"role": "user", "content": "Some random fact A."},
            {"role": "assistant", "content": "Noted A."},
            {"role": "user", "content": "Some random fact B."},
            {"role": "assistant", "content": "Noted B."},
        ] + [
            {"role": "user", "content": f"Filler {i}."} for i in range(6)
        ]
        # The model returns a much better consolidated fact.
        model_content = (
            '{"facts":["Debugging OAuth in app/auth.py:142 — '
            'JWT decoding throws InvalidSignature"],'
            '"preferences":[],"open_tasks":[]}'
        )
        result = self._run_with_model_response(history, model_content)
        self.assertTrue(result["performed"])
        # Model's identifier-preserving fact is what survives —
        # NOT the heuristic's verbatim "Some random fact A." dump.
        self.assertEqual(
            result["summary"]["facts"],
            [
                "Debugging OAuth in app/auth.py:142 — "
                "JWT decoding throws InvalidSignature",
            ],
        )

    def test_model_empty_bucket_falls_back_to_heuristic(self):
        # User explicitly states a preference; model leaves
        # preferences empty. The heuristic catches the preference
        # via its \\bprefer\\b regex; we should NOT lose it.
        history = [
            {"role": "user", "content": "I prefer concise answers."},
            {"role": "assistant", "content": "Okay."},
        ] + [
            {"role": "user", "content": f"Filler {i}."} for i in range(8)
        ]
        model_content = (
            '{"facts":["Some fact"],"preferences":[],"open_tasks":[]}'
        )
        result = self._run_with_model_response(history, model_content)
        self.assertEqual(
            result["summary"]["preferences"],
            ["I prefer concise answers."],
        )
        # And the model's facts still win.
        self.assertEqual(result["summary"]["facts"], ["Some fact"])

    def test_model_failure_falls_back_to_pure_heuristic(self):
        # Model returns invalid JSON → fallback heuristic runs.
        history = [
            {"role": "user", "content": "I prefer concise answers."},
            {"role": "assistant", "content": "Okay."},
            {"role": "user", "content": "Open task is fixing the build."},
            {"role": "assistant", "content": "Noted."},
        ] + [
            {"role": "user", "content": f"Filler {i}."} for i in range(6)
        ]
        result = self._run_with_model_response(history, "not json at all")
        self.assertTrue(result["performed"])
        # Fallback summary picks up explicit preferences + open tasks.
        self.assertIn("I prefer concise answers.", result["summary"]["preferences"])
        self.assertIn(
            "Open task is fixing the build.", result["summary"]["open_tasks"],
        )


class SummarySystemPromptTests(unittest.TestCase):
    """The expanded SUMMARY_SYSTEM_PROMPT is essential to summary
    quality on small local models. Lock in the high-leverage parts
    so a future edit can't quietly regress them."""

    def test_prompt_names_all_three_buckets(self):
        for key in ("facts", "preferences", "open_tasks"):
            self.assertIn(key, SUMMARY_SYSTEM_PROMPT)

    def test_prompt_emphasises_identifier_preservation(self):
        # The single most important guidance — without it small models
        # produce abstract summaries that lose file paths and decisions.
        self.assertIn("PRESERVE", SUMMARY_SYSTEM_PROMPT)
        self.assertIn("file paths", SUMMARY_SYSTEM_PROMPT.lower())

    def test_prompt_has_concrete_good_vs_bad_example(self):
        # Few-shot examples disproportionately help small models.
        self.assertIn("good:", SUMMARY_SYSTEM_PROMPT.lower())
        self.assertIn("bad:", SUMMARY_SYSTEM_PROMPT.lower())

    def test_prompt_directs_consolidation_not_enumeration(self):
        self.assertIn("Consolidate", SUMMARY_SYSTEM_PROMPT)

    def test_prompt_demands_json_only(self):
        # Closing instruction matters — small models love adding
        # "Here's the JSON:" preambles.
        self.assertIn("JSON only", SUMMARY_SYSTEM_PROMPT)
        self.assertIn("No markdown fences", SUMMARY_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
