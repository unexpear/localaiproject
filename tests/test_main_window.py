import unittest

from app.ui.main_window import _default_graph_positions, _detail_anchor_prefix, _execution_row_label


class MainWindowTests(unittest.TestCase):
    def test_execution_row_label_includes_id_status_lane_and_preview(self):
        label = _execution_row_label(
            {
                "id": "exe_123",
                "status": "needs_approval",
                "resolved_lane": "coding",
                "message_preview": "Explain app/router.py",
            }
        )

        self.assertIn("exe_123", label)
        self.assertIn("needs approval", label)
        self.assertIn("coding", label)
        self.assertIn("Explain app/router.py", label)

    def test_detail_anchor_prefix_maps_nodes_to_detail_sections(self):
        self.assertEqual(_detail_anchor_prefix("input"), "- Message:")
        self.assertEqual(_detail_anchor_prefix("workspace"), "- Approval:")
        self.assertIsNone(_detail_anchor_prefix("unknown"))

    def test_default_graph_positions_wrap_after_three_columns(self):
        positions = _default_graph_positions(
            [
                {"id": "input"},
                {"id": "route"},
                {"id": "model"},
                {"id": "workspace"},
            ],
            canvas_width=900,
        )

        self.assertEqual(positions["input"][1], positions["route"][1])
        self.assertEqual(positions["route"][1], positions["model"][1])
        self.assertGreater(positions["workspace"][1], positions["input"][1])


if __name__ == "__main__":
    unittest.main()
