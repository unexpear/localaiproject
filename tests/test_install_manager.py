import unittest
from unittest.mock import patch

from app.models import install_manager


class ResolveInstallTargetTests(unittest.TestCase):
    def test_resolves_role_name_to_model(self):
        role, model = install_manager.resolve_install_target("vision")
        self.assertEqual(role, "vision")
        self.assertEqual(model, "qwen3-vl")

    def test_resolves_known_model_name_back_to_role(self):
        role, model = install_manager.resolve_install_target("llama3.1:8b")
        self.assertEqual(role, "reasoning")
        self.assertEqual(model, "llama3.1:8b")

    def test_unknown_target_returns_none(self):
        self.assertEqual(install_manager.resolve_install_target("nonsense:99b"), (None, None))
        self.assertEqual(install_manager.resolve_install_target(""), (None, None))
        self.assertEqual(install_manager.resolve_install_target(None), (None, None))


class InstallModelTests(unittest.TestCase):
    def test_unknown_target_reports_error_and_skips_pull(self):
        with patch("app.models.install_manager.pull_model") as pull_mock:
            result = install_manager.install_model("totally-missing", installed_models=set())

        self.assertFalse(result["success"])
        self.assertIsNone(result["model"])
        self.assertIsNone(result["role"])
        self.assertEqual(result["requested_target"], "totally-missing")
        self.assertIn("Unknown role or model", result["output"])
        pull_mock.assert_not_called()

    def test_already_installed_short_circuits_pull(self):
        with patch("app.models.install_manager.pull_model") as pull_mock, patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ):
            result = install_manager.install_model("coding", installed_models={"qwen2.5-coder:7b"})

        self.assertTrue(result["success"])
        self.assertTrue(result["already_installed"])
        self.assertEqual(result["model"], "qwen2.5-coder:7b")
        pull_mock.assert_not_called()

    def test_unfiltered_role_attaches_warning(self):
        with patch("app.models.install_manager.pull_model", return_value=(True, "success")), patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            result = install_manager.install_model("unfiltered", installed_models=set())

        self.assertTrue(result["success"])
        self.assertEqual(result["model"], "dolphin3:8b")
        self.assertTrue(any("no safety filter" in w for w in result["warnings"]))

    def test_blocked_when_ollama_version_too_old(self):
        with patch("app.models.install_manager.pull_model") as pull_mock, patch(
            "app.models.install_manager.get_ollama_version", return_value="0.10.0"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            result = install_manager.install_model("vision", installed_models=set())

        self.assertFalse(result["success"])
        self.assertIn("Ollama 0.12.7+", result["output"])
        pull_mock.assert_not_called()

    def test_blocked_when_model_too_big_for_ram(self):
        with patch("app.models.install_manager.pull_model") as pull_mock, patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ), patch("app.models.install_manager.is_model_too_big", return_value=True):
            result = install_manager.install_model("vision", installed_models=set())

        self.assertFalse(result["success"])
        self.assertIn("too large", result["output"])
        pull_mock.assert_not_called()

    def test_pull_success_returns_role_model_and_output(self):
        with patch("app.models.install_manager.pull_model", return_value=(True, "success")), patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            result = install_manager.install_model("vision", installed_models=set())

        self.assertTrue(result["success"])
        self.assertEqual(result["role"], "vision")
        self.assertEqual(result["model"], "qwen3-vl")
        self.assertFalse(result["already_installed"])

    def test_install_model_forwards_progress_events(self):
        events = []

        def fake_pull(name, progress_callback=None, cancel_event=None):
            if progress_callback:
                progress_callback({"status": "downloading", "completed": 10, "total": 100, "model": name})
            return True, "success"

        with patch("app.models.install_manager.pull_model", side_effect=fake_pull), patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            install_manager.install_model("vision", installed_models=set(), progress_callback=events.append)

        phases = [e.get("phase") for e in events if e.get("phase")]
        self.assertIn("start", phases)
        self.assertIn("done", phases)
        # Also saw the raw pull event forwarded:
        self.assertTrue(any(e.get("status") == "downloading" for e in events))

    def test_install_model_already_installed_emits_done_only(self):
        events = []
        with patch("app.models.install_manager.pull_model") as pull_mock:
            result = install_manager.install_model(
                "coding",
                installed_models={"qwen2.5-coder:7b"},
                progress_callback=events.append,
            )

        pull_mock.assert_not_called()
        self.assertTrue(result["already_installed"])
        self.assertEqual([e["phase"] for e in events], ["done"])
        self.assertTrue(events[0]["success"])

    def test_install_model_blocked_emits_blocked_and_done(self):
        events = []
        with patch("app.models.install_manager.pull_model") as pull_mock, patch(
            "app.models.install_manager.get_ollama_version", return_value="0.10.0"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            install_manager.install_model(
                "vision",
                installed_models=set(),
                progress_callback=events.append,
            )

        pull_mock.assert_not_called()
        phases = [e.get("phase") for e in events if e.get("phase")]
        self.assertEqual(phases[:1], ["start"])
        self.assertIn("blocked", phases)
        self.assertIn("done", phases)


class EnsureDefaultModelsTests(unittest.TestCase):
    def test_emits_queue_start_done_per_missing_model(self):
        events = []
        with patch("app.models.install_manager.pull_model", return_value=(True, "success")), patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            results = install_manager.ensure_default_models([], progress_callback=events.append)

        self.assertEqual(len(results), 2)  # reasoning + coding are required
        queue_events = [e for e in events if e.get("phase") == "queue"]
        self.assertEqual(len(queue_events), 1)
        self.assertEqual(set(queue_events[0]["models"]), {"llama3.1:8b", "qwen2.5-coder:7b"})
        start_events = [e for e in events if e.get("phase") == "start"]
        self.assertEqual(len(start_events), 2)
        self.assertEqual(start_events[0]["total"], 2)
        done_events = [e for e in events if e.get("phase") == "done"]
        self.assertEqual(len(done_events), 2)
        self.assertTrue(all(e["success"] for e in done_events))


class CancelInstallTests(unittest.TestCase):
    def test_ensure_default_models_emits_cancelled_phase_and_stops(self):
        import threading

        cancel = threading.Event()
        cancel.set()  # cancel before the loop even starts
        events = []

        with patch("app.models.install_manager.pull_model") as pull_mock, patch(
            "app.models.install_manager.get_ollama_version", return_value="0.12.7"
        ), patch("app.models.install_manager.is_model_too_big", return_value=False):
            results = install_manager.ensure_default_models(
                [], progress_callback=events.append, cancel_event=cancel
            )

        pull_mock.assert_not_called()
        self.assertEqual(results, [])
        phases = [e.get("phase") for e in events if e.get("phase")]
        self.assertIn("queue", phases)
        self.assertIn("cancelled", phases)

    def test_retry_does_not_fire_when_cancelled(self):
        import threading

        cancel = threading.Event()

        def fake_pull(name, progress_callback=None, cancel_event=None):
            # Simulate cancellation happening during the first pull.
            cancel.set()
            return False, "cancelled"

        with patch("app.models.install_manager.pull_model", side_effect=fake_pull) as pull_mock:
            success, output = install_manager._pull_with_retry("x", cancel_event=cancel)

        self.assertEqual(pull_mock.call_count, 1)  # no retry after cancel
        self.assertFalse(success)
        self.assertEqual(output, "cancelled")


class UninstallModelTests(unittest.TestCase):
    def test_unknown_target_returns_error(self):
        with patch("app.models.install_manager.delete_model") as delete_mock:
            result = install_manager.uninstall_model("nonsense")

        delete_mock.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIn("Unknown role or model", result["output"])

    def test_not_installed_returns_flag_and_skips_delete(self):
        with patch("app.models.install_manager.delete_model") as delete_mock:
            result = install_manager.uninstall_model("vision", installed_models=set())

        delete_mock.assert_not_called()
        self.assertFalse(result["success"])
        self.assertTrue(result["not_installed"])
        self.assertEqual(result["model"], "qwen3-vl")

    def test_installed_target_calls_delete(self):
        with patch("app.models.install_manager.delete_model", return_value=(True, "deleted")) as delete_mock:
            result = install_manager.uninstall_model(
                "vision",
                installed_models={"qwen3-vl"},
            )

        delete_mock.assert_called_once_with("qwen3-vl")
        self.assertTrue(result["success"])
        self.assertEqual(result["role"], "vision")


if __name__ == "__main__":
    unittest.main()
