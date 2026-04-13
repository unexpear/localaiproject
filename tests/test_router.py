import unittest

from app.supervisor.router import route, route_message


class RouterTests(unittest.TestCase):
    def _history_entry(self, role, lane, content="placeholder"):
        return {
            "role": role,
            "content": content,
            "metadata": {
                "requested_lane": lane,
                "resolved_lane": lane,
            },
        }

    def test_routes_code_requests_to_coding(self):
        self.assertEqual(route("Write a Python function to parse CSV lines."), "coding")

    def test_routes_stack_traces_to_coding(self):
        self.assertEqual(route("Traceback: TypeError in app.py line 42"), "coding")

    def test_routes_visual_requests_to_vision(self):
        self.assertEqual(route("Please analyze this screenshot and extract the text."), "vision")

    def test_routes_general_questions_to_reasoning(self):
        self.assertEqual(route("Help me think through whether I should move to another city."), "reasoning")

    def test_does_not_mistake_codename_for_code(self):
        self.assertEqual(route("What is the launch codename?"), "reasoning")

    def test_route_message_returns_scores_and_confidence(self):
        result = route_message("Refactor this function in main.py and fix the bug.")
        self.assertEqual(result["lane"], "coding")
        self.assertIn(result["confidence"], {"medium", "high"})
        self.assertGreater(result["scores"]["coding"], result["scores"]["reasoning"])
        self.assertTrue(result["signals"]["coding"])

    def test_follow_up_prompt_uses_recent_coding_context(self):
        recent_history = [
            self._history_entry("user", "coding", "Write a Python function to parse CSV lines."),
            self._history_entry("assistant", "coding", "Here is a parser."),
        ]
        result = route_message("make it shorter and faster", recent_history=recent_history)
        self.assertEqual(result["lane"], "coding")
        self.assertTrue(result["context_used"])

    def test_follow_up_prompt_uses_recent_vision_context(self):
        recent_history = [
            self._history_entry("user", "vision", "Analyze this screenshot."),
            self._history_entry("assistant", "vision", "The screenshot contains a login form."),
        ]
        result = route_message("read the text too", recent_history=recent_history)
        self.assertEqual(result["lane"], "vision")
        self.assertTrue(result["context_used"])

    def test_explicit_current_signal_beats_previous_context(self):
        recent_history = [
            self._history_entry("user", "reasoning", "Help me compare apartments."),
            self._history_entry("assistant", "reasoning", "Here are the tradeoffs."),
        ]
        result = route_message("Traceback: TypeError in app.py line 42", recent_history=recent_history)
        self.assertEqual(result["lane"], "coding")
        self.assertEqual(result["primary_signal"], "looks like an error or stack trace")

    def test_active_skill_bias_can_break_tie_for_ambiguous_prompt(self):
        result = route_message("keep going", lane_biases={"coding": 2})
        self.assertEqual(result["lane"], "coding")
        self.assertIn("active session skills favored this lane", result["primary_signal"])


if __name__ == "__main__":
    unittest.main()
