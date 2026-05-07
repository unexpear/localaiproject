import unittest
from unittest.mock import patch

from app.services.onboarding_service import format_doctor_report, format_onboarding_guide


_ROOMY_HARDWARE = {"os": "Windows", "cpu": "x86_64", "ram": 64 * (1024 ** 3), "gpu": "RTX 3070 Ti", "disk": {}, "python": "3.13"}
_TIGHT_HARDWARE = {"os": "Windows", "cpu": "x86_64", "ram": 12 * (1024 ** 3), "gpu": None, "disk": {}, "python": "3.13"}


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
        ), patch(
            "app.services.onboarding_service.hardware_summary",
            return_value=_ROOMY_HARDWARE,
        ):
            rendered = format_doctor_report(session_id="default")

        self.assertIn("Control Layer Doctor", rendered)
        self.assertIn("reasoning -> llama3.1:8b [ready]", rendered)
        self.assertIn("coding -> qwen2.5-coder:7b [missing required]", rendered)
        self.assertIn("Pending approval: read app/supervisor/router.py", rendered)
        self.assertIn("Compact memory: facts 1, preferences 1, open tasks 1", rendered)
        self.assertIn("Detected project shape: python", rendered)
        self.assertIn("Hardware", rendered)
        self.assertIn("RAM: 64.0 GB", rendered)
        self.assertIn("8 GB", rendered)
        self.assertIn("safe", rendered)

    def test_doctor_report_flags_tight_lanes_on_low_ram(self):
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
        context = {"workspace_path": None, "active_skills": [], "pending_approval": None, "approved_search": None}

        with patch("app.services.onboarding_service.get_status", return_value=status), patch(
            "app.services.onboarding_service.local_memory.get_session_context",
            return_value=context,
        ), patch(
            "app.services.onboarding_service.compaction_service.get_compact_summary",
            return_value={"facts": [], "preferences": [], "open_tasks": []},
        ), patch(
            "app.services.onboarding_service.inspect_workspace",
            return_value={"ok": False, "error": "no_workspace_configured", "path": None},
        ), patch(
            "app.services.onboarding_service.hardware_summary",
            return_value=_TIGHT_HARDWARE,
        ):
            rendered = format_doctor_report(session_id="default")

        self.assertIn("RAM: 12.0 GB", rendered)
        self.assertIn("tight", rendered)

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
        ), patch(
            "app.services.onboarding_service.hardware_summary",
            return_value=_ROOMY_HARDWARE,
        ):
            rendered = format_onboarding_guide(session_id="default")

        self.assertIn("install-defaults", rendered)
        self.assertIn("workspace <path>", rendered)
        self.assertIn("doctor", rendered)
        self.assertIn("Detected RAM: 64.0 GB", rendered)
        self.assertNotIn("Hardware Fit Warnings", rendered)

    def test_onboard_guide_surfaces_fit_warnings_on_tight_ram(self):
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
        context = {"workspace_path": None, "active_skills": [], "pending_approval": None, "approved_search": None}

        with patch("app.services.onboarding_service.get_status", return_value=status), patch(
            "app.services.onboarding_service.local_memory.get_session_context",
            return_value=context,
        ), patch(
            "app.services.onboarding_service.compaction_service.get_compact_summary",
            return_value={"facts": [], "preferences": [], "open_tasks": []},
        ), patch(
            "app.services.onboarding_service.inspect_workspace",
            return_value={"ok": False, "error": "no_workspace_configured", "path": None},
        ), patch(
            "app.services.onboarding_service.hardware_summary",
            return_value=_TIGHT_HARDWARE,
        ):
            rendered = format_onboarding_guide(session_id="default")

        self.assertIn("Hardware Fit Warnings", rendered)
        self.assertIn("reasoning", rendered)
        self.assertIn("tight", rendered)


if __name__ == "__main__":
    unittest.main()
