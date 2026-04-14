import unittest
from unittest.mock import patch

from app.services.onboarding_service import format_doctor_report, format_onboarding_guide


class OnboardingServiceTests(unittest.TestCase):
    def test_doctor_report_includes_runtime_lanes_and_session_state(self):
        status = {
            "runtime_available": True,
            "version": "0.12.7",
            "installed": [{"role": "reasoning", "model": "llama3.1:8b", "required": True, "size_gb": 8}],
            "missing": [{"role": "coding", "model": "qwen2.5-coder:7b", "required": True, "size_gb": 7}],
            "optional": [
                {"role": "vision", "model": "qwen3-vl", "required": False, "size_gb": 9},
                {"role": "unfiltered", "model": "dolphin3:8b", "required": False, "size_gb": 8},
            ],
            "detected": ["llama3.1:8b"],
        }
        context = {
            "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
            "active_skills": ["builder"],
            "pending_approval": {
                "kind": "read",
                "origin": "chat",
                "workspace_path": "C:\\Users\\micha\\Desktop\\localaiproject",
                "paths": ["app/supervisor/router.py"],
            },
            "approved_search": {
                "query": "route_message",
                "files": ["app/supervisor/router.py"],
            },
        }
        summary = {
            "facts": ["Project codename: Nebula Forge"],
            "preferences": ["Keep answers direct"],
            "open_tasks": ["Patch the router"],
        }
        workspace_snapshot = {
            "ok": True,
            "path": "C:\\Users\\micha\\Desktop\\localaiproject",
            "stack": "python",
            "markers": ["pyproject.toml"],
            "top_level": ["app/", "tests/"],
        }

        with patch("app.services.onboarding_service.get_status", return_value=status), patch(
            "app.services.onboarding_service.local_memory.get_session_context",
            return_value=context,
        ), patch(
            "app.services.onboarding_service.compaction_service.get_compact_summary",
            return_value=summary,
        ), patch(
            "app.services.onboarding_service.inspect_workspace",
            return_value=workspace_snapshot,
        ):
            rendered = format_doctor_report(session_id="default")

        self.assertIn("Control Layer Doctor", rendered)
        self.assertIn("reasoning -> llama3.1:8b [ready]", rendered)
        self.assertIn("coding -> qwen2.5-coder:7b [missing required]", rendered)
        self.assertIn("Pending approval: read app/supervisor/router.py", rendered)
        self.assertIn("Compact memory: facts 1, preferences 1, open tasks 1", rendered)
        self.assertIn("Detected project shape: python", rendered)

    def test_onboard_guide_suggests_install_and_workspace_when_not_ready(self):
        status = {
            "runtime_available": True,
            "version": "0.12.7",
            "installed": [],
            "missing": [
                {"role": "reasoning", "model": "llama3.1:8b", "required": True, "size_gb": 8},
                {"role": "coding", "model": "qwen2.5-coder:7b", "required": True, "size_gb": 7},
            ],
            "optional": [],
            "detected": [],
        }
        context = {
            "workspace_path": None,
            "active_skills": [],
            "pending_approval": None,
            "approved_search": None,
        }

        with patch("app.services.onboarding_service.get_status", return_value=status), patch(
            "app.services.onboarding_service.local_memory.get_session_context",
            return_value=context,
        ), patch(
            "app.services.onboarding_service.compaction_service.get_compact_summary",
            return_value={"facts": [], "preferences": [], "open_tasks": []},
        ), patch(
            "app.services.onboarding_service.inspect_workspace",
            return_value={"ok": False, "error": "no_workspace_configured", "path": None},
        ):
            rendered = format_onboarding_guide(session_id="default")

        self.assertIn("install-defaults", rendered)
        self.assertIn("workspace <path>", rendered)
        self.assertIn("doctor", rendered)


if __name__ == "__main__":
    unittest.main()
