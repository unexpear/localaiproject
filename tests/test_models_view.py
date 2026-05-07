import os
import unittest
from unittest.mock import patch

# Headless rendering for CI / testing
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from app.ui.models_view import ModelsView  # noqa: E402


_FAKE_STATUS = {
    "runtime_available": True,
    "version": "0.12.7",
    "installed": [
        {"role": "reasoning", "model": "llama3.1:8b", "required": True, "size_gb": 8},
    ],
    "missing": [
        {"role": "coding", "model": "qwen2.5-coder:7b", "required": True, "size_gb": 7},
    ],
    "optional": [
        {"role": "vision", "model": "qwen3-vl", "required": False, "size_gb": 9},
        {"role": "unfiltered", "model": "dolphin3:8b", "required": False, "size_gb": 8},
    ],
    "detected": ["llama3.1:8b", "phi3:mini", "nomic-embed-text:latest"],
}

_FAKE_HARDWARE = {
    "os": "Windows",
    "cpu": "x86_64",
    "ram": 32 * (1024 ** 3),
    "gpu": "RTX 3070 Ti",
    "disk": {},
    "python": "3.13",
}


def _qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class ModelsViewBuildTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()

    def test_view_constructs_with_lane_rows_and_banner(self):
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            view = ModelsView()

        try:
            self.assertIn("connected", view.banner_label.text().lower())
            self.assertIn("0.12.7", view.banner_label.text())
            self.assertIn("32.0 GB", view.banner_label.text())
            # 4 lane rows wrapped in QWidget items + a stretch — at least 4 widgets
            self.assertGreaterEqual(view.lanes_layout.count(), 4)
            # Extras should be visible because phi3:mini and nomic-embed-text aren't registered.
            # isVisible() requires actual show() — use isHidden() to read the explicit flag.
            self.assertFalse(view.extras_label.isHidden())
            self.assertIn("phi3:mini", view.extras_label.text())
            self.assertIn("nomic-embed-text", view.extras_label.text())
        finally:
            view.deleteLater()

    def test_extras_section_hidden_when_no_unmanaged_models(self):
        status = dict(_FAKE_STATUS)
        status["detected"] = [m["model"] for m in _FAKE_STATUS["installed"]]
        with patch("app.ui.models_view.get_status", return_value=status), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            view = ModelsView()

        try:
            self.assertTrue(view.extras_label.isHidden())
            self.assertTrue(view.extras_head.isHidden())
        finally:
            view.deleteLater()

    def test_summary_label_counts_installed_lanes(self):
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            view = ModelsView()

        try:
            text = view.summary_label.text()
            # 1 installed (reasoning) of 4 registered
            self.assertIn("1 / 4", text)
        finally:
            view.deleteLater()


class ModelsViewActionSignalTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()

    def _build_view(self):
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            return ModelsView()

    def _find_button(self, view, label_text):
        # Walk all buttons, return first whose text matches
        for btn in view.findChildren(QPushButton):
            if btn.text() == label_text:
                return btn
        return None

    def test_install_button_emits_install_command_for_role(self):
        view = self._build_view()
        signals = []
        view.lane_action.connect(signals.append)
        try:
            install_btn = self._find_button(view, "Install")
            self.assertIsNotNone(install_btn)
            install_btn.click()
            self.assertTrue(any(s.startswith("install ") for s in signals))
        finally:
            view.deleteLater()

    def test_uninstall_button_emits_uninstall_command_for_role(self):
        view = self._build_view()
        signals = []
        view.lane_action.connect(signals.append)
        try:
            uninstall_btn = self._find_button(view, "Uninstall")
            self.assertIsNotNone(uninstall_btn)
            uninstall_btn.click()
            # Must reference one of the installed lanes (reasoning is the only one in fixture)
            self.assertTrue(any(s == "uninstall reasoning" for s in signals))
        finally:
            view.deleteLater()

    def test_install_defaults_button_emits_install_defaults(self):
        view = self._build_view()
        signals = []
        view.lane_action.connect(signals.append)
        try:
            view.install_defaults_button.click()
            self.assertIn("install-defaults", signals)
        finally:
            view.deleteLater()


class ModelsViewLivePullTests(unittest.TestCase):
    def setUp(self):
        self.app = _qapp()

    def _build_view(self):
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            return ModelsView()

    def _row_button_label(self, view, model_name):
        # Find the lane row whose model label text matches, return its action button text
        for child in view.lanes_container.findChildren(QPushButton):
            # The action button is the last button in each row; we just look for the one whose
            # parent row contains the model name.
            row = child.parent()
            if row is None:
                continue
            for label in row.findChildren(type(view.banner_label)):  # QLabel
                if label.text() == model_name:
                    return child.text()
        return None

    def test_start_phase_swaps_install_button_to_cancel(self):
        view = self._build_view()
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            try:
                self.assertEqual(self._row_button_label(view, "qwen3-vl"), "Install")
                view.set_pull_progress({"phase": "start", "model": "qwen3-vl", "index": 1, "total": 1})
                self.assertEqual(self._row_button_label(view, "qwen3-vl"), "Cancel")
            finally:
                view.deleteLater()

    def test_done_phase_clears_pull_state(self):
        view = self._build_view()
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            try:
                view.set_pull_progress({"phase": "start", "model": "qwen3-vl", "index": 1, "total": 1})
                view.set_pull_progress({"phase": "done", "model": "qwen3-vl", "success": True, "output": "ok"})
                self.assertIsNone(view._active_pull_model)
                self.assertEqual(self._row_button_label(view, "qwen3-vl"), "Install")
            finally:
                view.deleteLater()

    def test_stream_event_updates_status_text(self):
        view = self._build_view()
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            try:
                view.set_pull_progress({"phase": "start", "model": "qwen3-vl", "index": 1, "total": 1})
                view.set_pull_progress({
                    "model": "qwen3-vl",
                    "status": "downloading",
                    "completed": 1024 * 1024 * 1180,
                    "total": 1024 * 1024 * 2510,
                })
                self.assertIsNotNone(view._active_pull_percent)
                self.assertEqual(view._active_pull_percent, 47)
                self.assertIn("47%", view._active_pull_status)
                self.assertIn("downloading", view._active_pull_status)
            finally:
                view.deleteLater()

    def test_cancel_button_emits_cancel_requested(self):
        view = self._build_view()
        signals = []
        view.cancel_requested.connect(lambda: signals.append("cancel"))
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            try:
                view.set_pull_progress({"phase": "start", "model": "qwen3-vl", "index": 1, "total": 1})
                # Find the lane row whose action button is "Cancel" and click it.
                cancel_clicked = False
                for btn in view.lanes_container.findChildren(QPushButton):
                    if btn.text() == "Cancel":
                        btn.click()
                        cancel_clicked = True
                        break
                self.assertTrue(cancel_clicked, "no Cancel button found in any lane row")
                self.assertEqual(signals, ["cancel"])
            finally:
                view.deleteLater()

    def test_cancelled_phase_clears_pull_state(self):
        view = self._build_view()
        with patch("app.ui.models_view.get_status", return_value=_FAKE_STATUS), patch(
            "app.ui.models_view.hardware_summary", return_value=_FAKE_HARDWARE
        ):
            try:
                view.set_pull_progress({"phase": "start", "model": "qwen3-vl", "index": 1, "total": 1})
                view.set_pull_progress({"phase": "cancelled"})
                self.assertIsNone(view._active_pull_model)
            finally:
                view.deleteLater()


if __name__ == "__main__":
    unittest.main()
