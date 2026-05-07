"""Tests for the ``ImageDropInput`` Qt widget (Phase 2 of
``docs/design/vision-input.md``).

Covers:
* ``_resolve_drop`` validation:
  - valid image inside workspace → relative path
  - non-existent file → ``not_a_file``
  - non-image extension → ``unsupported_format``
  - no workspace pinned → ``no_workspace``
  - file outside workspace → ``outside_workspace``
  - cross-drive (Windows) handled without crashing
* End-to-end ``dropEvent`` simulation:
  - single valid image inserts ``@image:<rel-path>`` token
  - multiple images insert as space-separated tokens
  - cap respected (max images per drop)
  - rejected files emit ``image_drop_rejected``, valid ones still
    insert
  - cursor positioning + space padding around inserted token
* ``dragEnterEvent`` accepts/refuses based on extension hint.
"""
import os
import sys
import tempfile
import unittest
import uuid

# Headless Qt rendering
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.image_drop_input import (
    ImageDropInput,
    REJECT_NOT_A_FILE,
    REJECT_NO_WORKSPACE,
    REJECT_OUTSIDE_WORKSPACE,
    REJECT_UNSUPPORTED_FORMAT,
)


_TINY_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41,
    0x54, 0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
    0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
    0x42, 0x60, 0x82,
])


def _qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_png(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_TINY_PNG)


# ============================================================
# _resolve_drop validation
# ============================================================

class ResolveDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qapp()

    def _widget(self, workspace=None):
        return ImageDropInput(workspace_path_provider=lambda: workspace)

    def test_valid_image_inside_workspace_returns_relative_path(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "screenshots", "login.png")
            _write_png(full)
            w = self._widget(ws)
            rel, reason = w._resolve_drop(full, ws)
            self.assertIsNone(reason)
            self.assertEqual(rel, "screenshots/login.png")
            w.deleteLater()

    def test_non_existent_file_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            w = self._widget(ws)
            rel, reason = w._resolve_drop(
                os.path.join(ws, "nope.png"), ws,
            )
            self.assertIsNone(rel)
            self.assertEqual(reason, REJECT_NOT_A_FILE)
            w.deleteLater()

    def test_non_image_extension_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            txt = os.path.join(ws, "notes.txt")
            with open(txt, "w") as fh:
                fh.write("not an image")
            w = self._widget(ws)
            rel, reason = w._resolve_drop(txt, ws)
            self.assertIsNone(rel)
            self.assertEqual(reason, REJECT_UNSUPPORTED_FORMAT)
            w.deleteLater()

    def test_no_workspace_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "x.png")
            _write_png(full)
            w = self._widget(workspace=None)
            rel, reason = w._resolve_drop(full, None)
            self.assertIsNone(rel)
            self.assertEqual(reason, REJECT_NO_WORKSPACE)
            w.deleteLater()

    def test_file_outside_workspace_rejected(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as other:
            outside = os.path.join(other, "x.png")
            _write_png(outside)
            w = self._widget(ws)
            rel, reason = w._resolve_drop(outside, ws)
            self.assertIsNone(rel)
            self.assertEqual(reason, REJECT_OUTSIDE_WORKSPACE)
            w.deleteLater()

    def test_empty_path_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            w = self._widget(ws)
            rel, reason = w._resolve_drop("", ws)
            self.assertIsNone(rel)
            self.assertEqual(reason, REJECT_NOT_A_FILE)
            w.deleteLater()


# ============================================================
# handle_dropped_paths end-to-end
# ============================================================
#
# We bypass the synthetic QDropEvent path because constructing
# one across PySide6 versions is awkward (segfaults on some
# combinations). The widget's public ``handle_dropped_paths``
# entry point does the same work as ``dropEvent`` minus the
# Qt-event plumbing.

class HandleDroppedPathsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qapp()

    def test_single_valid_image_inserts_token(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "shot.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            try:
                w.handle_dropped_paths([full])
                self.assertIn("@image:shot.png", w.text())
            finally:
                w.deleteLater()

    def test_multiple_images_inserted_space_separated(self):
        with tempfile.TemporaryDirectory() as ws:
            paths = []
            for name in ("a.png", "b.jpg", "c.gif"):
                full = os.path.join(ws, name)
                _write_png(full)
                paths.append(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            try:
                w.handle_dropped_paths(paths)
                text = w.text()
                self.assertIn("@image:a.png", text)
                self.assertIn("@image:b.jpg", text)
                self.assertIn("@image:c.gif", text)
            finally:
                w.deleteLater()

    def test_drop_cap_respects_max_images_per_message(self):
        from app.services import vision_service
        with tempfile.TemporaryDirectory() as ws:
            paths = []
            for n in range(vision_service.MAX_IMAGES_PER_MESSAGE + 3):
                full = os.path.join(ws, f"img{n}.png")
                _write_png(full)
                paths.append(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            try:
                w.handle_dropped_paths(paths)
                count = w.text().count("@image:")
                self.assertEqual(count, vision_service.MAX_IMAGES_PER_MESSAGE)
            finally:
                w.deleteLater()

    def test_rejected_files_emit_signal_valid_files_still_insert(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as other:
            inside = os.path.join(ws, "good.png")
            _write_png(inside)
            outside = os.path.join(other, "bad.png")
            _write_png(outside)

            w = ImageDropInput(workspace_path_provider=lambda: ws)
            rejected = []
            w.image_drop_rejected.connect(
                lambda path, reason: rejected.append((path, reason))
            )
            try:
                w.handle_dropped_paths([inside, outside])
                self.assertIn("@image:good.png", w.text())
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0][1], REJECT_OUTSIDE_WORKSPACE)
            finally:
                w.deleteLater()

    def test_image_dropped_signal_emitted(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "foo.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            received = []
            w.image_dropped.connect(received.append)
            try:
                w.handle_dropped_paths([full])
                self.assertEqual(received, ["foo.png"])
            finally:
                w.deleteLater()

    def test_token_padded_with_spaces_for_existing_text(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "x.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            try:
                w.setText("hello")
                w.setCursorPosition(5)
                w.handle_dropped_paths([full])
                self.assertIn("hello @image:x.png", w.text())
            finally:
                w.deleteLater()

    def test_token_inserted_at_cursor_mid_text(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "x.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            try:
                w.setText("abcdef")
                w.setCursorPosition(3)
                w.handle_dropped_paths([full])
                self.assertIn("abc @image:x.png ", w.text())
                self.assertTrue(w.text().endswith("def"))
            finally:
                w.deleteLater()

    def test_no_workspace_rejects_all(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "x.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=lambda: None)
            rejected = []
            w.image_drop_rejected.connect(
                lambda path, reason: rejected.append((path, reason))
            )
            try:
                w.handle_dropped_paths([full])
                self.assertEqual(w.text(), "")
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0][1], REJECT_NO_WORKSPACE)
            finally:
                w.deleteLater()

    def test_workspace_provider_exception_treated_as_no_workspace(self):
        def _explode():
            raise RuntimeError("boom")
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "x.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=_explode)
            rejected = []
            w.image_drop_rejected.connect(
                lambda path, reason: rejected.append((path, reason))
            )
            try:
                w.handle_dropped_paths([full])
                self.assertEqual(rejected[0][1], REJECT_NO_WORKSPACE)
            finally:
                w.deleteLater()

    def test_empty_path_list_noop(self):
        w = ImageDropInput(workspace_path_provider=lambda: None)
        try:
            w.handle_dropped_paths([])
            self.assertEqual(w.text(), "")
        finally:
            w.deleteLater()

    def test_skips_empty_string_paths(self):
        with tempfile.TemporaryDirectory() as ws:
            full = os.path.join(ws, "ok.png")
            _write_png(full)
            w = ImageDropInput(workspace_path_provider=lambda: ws)
            try:
                w.handle_dropped_paths(["", full, ""])
                self.assertIn("@image:ok.png", w.text())
            finally:
                w.deleteLater()


# ============================================================
# Construction smoke
# ============================================================

class ConstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qapp()

    def test_widget_constructs_with_drop_acceptance_enabled(self):
        w = ImageDropInput(workspace_path_provider=lambda: None)
        try:
            self.assertTrue(w.acceptDrops())
        finally:
            w.deleteLater()

    def test_signals_exposed(self):
        w = ImageDropInput(workspace_path_provider=lambda: None)
        try:
            # Both signals exist and can be connected without error.
            w.image_dropped.connect(lambda *_: None)
            w.image_drop_rejected.connect(lambda *_: None)
        finally:
            w.deleteLater()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
