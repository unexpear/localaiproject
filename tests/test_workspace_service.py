import os
import tempfile
import unittest

from app.services.workspace_service import plan_workspace_action, read_workspace_file, search_workspace


class WorkspaceServiceTests(unittest.TestCase):
    def test_read_workspace_file_blocks_paths_outside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace_root, tempfile.NamedTemporaryFile(suffix=".py") as outside_file:
            result = read_workspace_file(workspace_root, outside_file.name)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "outside_workspace")

    def test_search_workspace_returns_matches(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            file_path = os.path.join(workspace_root, "app.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")

            result = search_workspace(workspace_root, "route_message")

        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["relative_path"], "app.py")
        self.assertEqual(result["matches"][0]["line_number"], 1)

    def test_search_workspace_filters_config_and_data_files_by_default(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "app", "memory"), exist_ok=True)
            with open(os.path.join(workspace_root, "app", "router.py"), "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")
            with open(os.path.join(workspace_root, "app", "memory", "local_memory.json"), "w", encoding="utf-8") as f:
                f.write('{"query": "route_message"}\n')

            result = search_workspace(workspace_root, "route_message")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files_with_matches"], ["app/router.py"])
        self.assertEqual({match["relative_path"] for match in result["matches"]}, {"app/router.py"})
        self.assertFalse(result["included_config_data"])

    def test_search_workspace_can_include_config_and_data_files_when_requested(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "app", "memory"), exist_ok=True)
            with open(os.path.join(workspace_root, "app", "router.py"), "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")
            with open(os.path.join(workspace_root, "app", "memory", "local_memory.json"), "w", encoding="utf-8") as f:
                f.write('{"query": "route_message"}\n')

            result = search_workspace(workspace_root, "route_message json")

        self.assertTrue(result["ok"])
        self.assertIn("app/router.py", result["files_with_matches"])
        self.assertIn("app/memory/local_memory.json", result["files_with_matches"])
        self.assertTrue(result["included_config_data"])

    def test_search_workspace_filters_docs_and_examples_by_default(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "docs"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "examples"), exist_ok=True)
            with open(os.path.join(workspace_root, "app", "router.py"), "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")
            with open(os.path.join(workspace_root, "docs", "router.md"), "w", encoding="utf-8") as f:
                f.write("route_message appears in the docs\n")
            with open(os.path.join(workspace_root, "examples", "router_example.py"), "w", encoding="utf-8") as f:
                f.write("from app.router import route_message\n")

            result = search_workspace(workspace_root, "route_message")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files_with_matches"], ["app/router.py"])
        self.assertEqual({match["relative_path"] for match in result["matches"]}, {"app/router.py"})
        self.assertFalse(result["included_docs_examples"])

    def test_search_workspace_can_include_docs_and_examples_when_requested(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "docs"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "examples"), exist_ok=True)
            with open(os.path.join(workspace_root, "app", "router.py"), "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")
            with open(os.path.join(workspace_root, "docs", "router.md"), "w", encoding="utf-8") as f:
                f.write("route_message appears in the docs\n")
            with open(os.path.join(workspace_root, "examples", "router_example.py"), "w", encoding="utf-8") as f:
                f.write("from app.router import route_message\n")

            result = search_workspace(workspace_root, "route_message docs examples")

        self.assertTrue(result["ok"])
        self.assertIn("app/router.py", result["files_with_matches"])
        self.assertIn("docs/router.md", result["files_with_matches"])
        self.assertIn("examples/router_example.py", result["files_with_matches"])
        self.assertTrue(result["included_docs_examples"])

    def test_search_workspace_ranks_source_definition_before_tests(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "tests"), exist_ok=True)
            with open(os.path.join(workspace_root, "app", "router.py"), "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")
            with open(os.path.join(workspace_root, "tests", "test_router.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from app.router import route_message\n"
                    "def test_route_message():\n"
                    "    assert route_message('x') == 'x'\n"
                )

            result = search_workspace(workspace_root, "route_message")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files_with_matches"][0], "app/router.py")
        self.assertEqual(result["files_with_matches"], ["app/router.py"])
        self.assertFalse(result["included_tests"])

    def test_search_workspace_can_include_tests_when_requested(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            os.makedirs(os.path.join(workspace_root, "tests"), exist_ok=True)
            with open(os.path.join(workspace_root, "app", "router.py"), "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")
            with open(os.path.join(workspace_root, "tests", "test_router.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from app.router import route_message\n"
                    "def test_route_message():\n"
                    "    assert route_message('x') == 'x'\n"
                )

            result = search_workspace(workspace_root, "route_message tests")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files_with_matches"][0], "app/router.py")
        self.assertIn("tests/test_router.py", result["files_with_matches"])
        self.assertTrue(result["included_tests"])

    def test_search_workspace_includes_tests_for_test_named_query(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "tests"), exist_ok=True)
            with open(os.path.join(workspace_root, "tests", "test_router.py"), "w", encoding="utf-8") as f:
                f.write("def test_route_message():\n    assert True\n")

            result = search_workspace(workspace_root, "test_route_message")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files_with_matches"], ["tests/test_router.py"])
        self.assertTrue(result["included_tests"])

    def test_plan_workspace_action_detects_file_reference(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            os.makedirs(os.path.join(workspace_root, "app"), exist_ok=True)
            file_path = os.path.join(workspace_root, "app", "router.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("def route_message(message):\n    return message\n")

            action = plan_workspace_action("Explain app/router.py", workspace_root)

        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "read")
        self.assertEqual(action["paths"], ["app/router.py"])

    def test_plan_workspace_action_uses_last_approved_search_for_follow_up(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            action = plan_workspace_action(
                "Explain that file too",
                workspace_root,
                approved_search={
                    "query": "route_message",
                    "files": ["app/supervisor/router.py", "app/services/chat_service.py"],
                },
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["kind"], "read")
        self.assertEqual(action["paths"], ["app/supervisor/router.py"])

    def test_plan_workspace_action_can_pick_second_search_result(self):
        with tempfile.TemporaryDirectory() as workspace_root:
            action = plan_workspace_action(
                "Open the second result",
                workspace_root,
                approved_search={
                    "query": "route_message",
                    "files": ["app/supervisor/router.py", "app/services/chat_service.py"],
                },
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["paths"], ["app/services/chat_service.py"])


if __name__ == "__main__":
    unittest.main()
