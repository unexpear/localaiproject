"""Settings dialog with the theme editor.

Lets the user:
  - pick the active preset from a dropdown
  - create a new preset of any kind (default / qt_material / qdarkstyle / qss)
  - edit the kind-specific options
  - save / save-as / delete
  - apply changes live (emits theme_changed)

Built-in presets cannot be deleted but can be cloned via "Save as".
"""
from __future__ import annotations

from typing import Any, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui import themes as themes_module


_PALETTE_OPTIONS = ("dark", "light")


class SettingsDialog(QDialog):
    theme_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(720, 560)
        self.setAcceptDrops(True)  # drop a .qss file onto the dialog to import

        self._state: Dict[str, Any] = themes_module.load_state()
        self._suspend_signals = False
        self._build_ui()
        self._reload_preset_combo(select=self._state.get("active") or themes_module.DEFAULT_ACTIVE)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        outer.addWidget(QLabel("<b>Typography</b>"))
        typo_row = QHBoxLayout()
        typo_row.addWidget(QLabel("UI font:"))
        self.typo_family_combo = QComboBox()
        self.typo_family_combo.setEditable(True)
        for fam in themes_module.UI_FAMILY_SUGGESTIONS:
            self.typo_family_combo.addItem(fam if fam else "(native default)")
        typo_row.addWidget(self.typo_family_combo, 1)

        typo_row.addWidget(QLabel("Base size:"))
        self.typo_size_combo = QComboBox()
        lo, hi = themes_module.BASE_SIZE_RANGE
        self.typo_size_combo.addItem("(default)", 0)
        for pt in range(lo, hi + 1):
            self.typo_size_combo.addItem(f"{pt} pt", pt)
        typo_row.addWidget(self.typo_size_combo)

        self.typo_apply_button = QPushButton("Apply typography")
        self.typo_apply_button.clicked.connect(self._on_apply_typography)
        typo_row.addWidget(self.typo_apply_button)
        outer.addLayout(typo_row)

        # Initialize typography controls from current state
        current = themes_module.get_typography(self._state)
        family = current.get("ui_family") or ""
        if family:
            self.typo_family_combo.setEditText(family)
        else:
            self.typo_family_combo.setCurrentIndex(0)
        size = int(current.get("base_size_pt") or 0)
        idx = self.typo_size_combo.findData(size) if size else 0
        self.typo_size_combo.setCurrentIndex(max(0, idx))

        outer.addWidget(QLabel("<b>Theme Editor</b>"))

        active_row = QHBoxLayout()
        active_row.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.currentTextChanged.connect(self._on_preset_selected)
        active_row.addWidget(self.preset_combo, 1)
        self.set_active_button = QPushButton("Set as active")
        self.set_active_button.clicked.connect(self._on_set_active)
        active_row.addWidget(self.set_active_button)
        outer.addLayout(active_row)

        self.active_label = QLabel("")
        outer.addWidget(self.active_label)

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("Kind:"))
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["default", "qt_material", "qdarkstyle", "qss"])
        self.kind_combo.currentTextChanged.connect(self._on_kind_changed)
        kind_row.addWidget(self.kind_combo, 1)
        outer.addLayout(kind_row)

        self.options_stack = QStackedWidget()
        self.options_stack.addWidget(self._build_default_page())
        self.options_stack.addWidget(self._build_qt_material_page())
        self.options_stack.addWidget(self._build_qdarkstyle_page())
        self.options_stack.addWidget(self._build_qss_page())
        outer.addWidget(self.options_stack, 1)

        button_row = QHBoxLayout()
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self._on_preview)
        button_row.addWidget(self.preview_button)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._on_save)
        button_row.addWidget(self.save_button)
        self.save_as_button = QPushButton("Save as...")
        self.save_as_button.clicked.connect(self._on_save_as)
        button_row.addWidget(self.save_as_button)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self._on_rename)
        button_row.addWidget(self.rename_button)
        self.export_button = QPushButton("Export...")
        self.export_button.clicked.connect(self._on_export)
        button_row.addWidget(self.export_button)
        self.import_button = QPushButton("Import .qss...")
        self.import_button.clicked.connect(self._on_import)
        button_row.addWidget(self.import_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(self._on_delete)
        button_row.addWidget(self.delete_button)
        button_row.addStretch(1)
        outer.addLayout(button_row)

        # Hint about drag-drop
        hint = QLabel("Tip: drop a .qss file onto this window to import it as a new preset.")
        hint.setStyleSheet("color: palette(mid); font-size: 9pt;")
        outer.addWidget(hint)

        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        close_box.accepted.connect(self.accept)
        outer.addWidget(close_box)

    def _build_default_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Native Qt style. No additional stylesheet."))
        layout.addStretch(1)
        return page

    def _build_qt_material_page(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        self.qt_material_combo = QComboBox()
        themes = themes_module.list_qt_material_themes()
        if themes:
            self.qt_material_combo.addItems(themes)
        else:
            self.qt_material_combo.addItem("dark_teal.xml")
        layout.addRow("Theme XML:", self.qt_material_combo)
        return page

    def _build_qdarkstyle_page(self) -> QWidget:
        page = QWidget()
        layout = QFormLayout(page)
        self.qdarkstyle_combo = QComboBox()
        self.qdarkstyle_combo.addItems(_PALETTE_OPTIONS)
        layout.addRow("Palette:", self.qdarkstyle_combo)
        return page

    def _build_qss_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Raw QSS (Qt Style Sheets):"))
        self.qss_editor = QPlainTextEdit()
        self.qss_editor.setPlaceholderText(
            "QWidget { background: #1a1a1a; color: #e0e0e0; }\n"
            "QPushButton { padding: 6px 12px; border-radius: 4px; }\n"
        )
        layout.addWidget(self.qss_editor, 1)
        return page

    # ------------------------------------------------------------------
    # Preset combo / state plumbing
    # ------------------------------------------------------------------
    def _reload_preset_combo(self, select: str | None = None) -> None:
        self._suspend_signals = True
        self.preset_combo.clear()
        names = sorted((self._state.get("presets") or {}).keys())
        for name in names:
            self.preset_combo.addItem(name)
        self._suspend_signals = False
        if select and select in names:
            self.preset_combo.setCurrentText(select)
        elif names:
            self.preset_combo.setCurrentText(names[0])
        self._on_preset_selected(self.preset_combo.currentText())
        self._update_active_label()

    def _update_active_label(self) -> None:
        self.active_label.setText(f"Active preset: <b>{self._state.get('active', '')}</b>")

    def _current_name(self) -> str:
        return self.preset_combo.currentText().strip()

    def _on_preset_selected(self, name: str) -> None:
        if self._suspend_signals or not name:
            return
        preset = (self._state.get("presets") or {}).get(name)
        if not preset:
            return
        kind = preset.get("kind", "default")
        options = preset.get("options") or {}
        self._suspend_signals = True
        self.kind_combo.setCurrentText(kind)
        self._on_kind_changed(kind)
        if kind == "qt_material":
            theme = options.get("theme") or "dark_teal.xml"
            idx = self.qt_material_combo.findText(theme)
            if idx >= 0:
                self.qt_material_combo.setCurrentIndex(idx)
            else:
                self.qt_material_combo.addItem(theme)
                self.qt_material_combo.setCurrentText(theme)
        elif kind == "qdarkstyle":
            palette = options.get("palette") or "dark"
            self.qdarkstyle_combo.setCurrentText(palette)
        elif kind == "qss":
            self.qss_editor.setPlainText(options.get("qss") or "")
        self._suspend_signals = False
        self._refresh_buttons(name)

    def _on_kind_changed(self, kind: str) -> None:
        index = {"default": 0, "qt_material": 1, "qdarkstyle": 2, "qss": 3}.get(kind, 0)
        self.options_stack.setCurrentIndex(index)

    def _refresh_buttons(self, name: str) -> None:
        is_builtin = name in themes_module.DEFAULT_PRESETS
        self.delete_button.setEnabled(bool(name) and not is_builtin)
        self.save_button.setEnabled(not is_builtin)
        self.rename_button.setEnabled(bool(name) and not is_builtin)
        # Export is enabled whenever there's a QSS payload to extract.
        preset = (self._state.get("presets") or {}).get(name) or {}
        self.export_button.setEnabled(themes_module.read_qss(preset) is not None)

    # ------------------------------------------------------------------
    # Build a preset payload from the current editor state
    # ------------------------------------------------------------------
    def _editor_payload(self) -> Dict[str, Any]:
        kind = self.kind_combo.currentText()
        if kind == "qt_material":
            return {"kind": "qt_material", "options": {"theme": self.qt_material_combo.currentText() or "dark_teal.xml"}}
        if kind == "qdarkstyle":
            return {"kind": "qdarkstyle", "options": {"palette": self.qdarkstyle_combo.currentText() or "dark"}}
        if kind == "qss":
            return {"kind": "qss", "options": {"qss": self.qss_editor.toPlainText()}}
        return {"kind": "default", "options": {}}

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _on_preview(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        try:
            themes_module.apply_to_app(
                app,
                self._editor_payload(),
                typography=themes_module.get_typography(self._state),
            )
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Preview failed", str(exc))

    def _on_save(self) -> None:
        name = self._current_name()
        if not name:
            return
        if name in themes_module.DEFAULT_PRESETS:
            QMessageBox.information(
                self,
                "Built-in preset",
                "Built-in presets cannot be overwritten. Use 'Save as...' to save your changes under a new name.",
            )
            return
        try:
            self._state = themes_module.upsert_preset(self._state, name, self._editor_payload())
            themes_module.save_state(self._state)
            self._reload_preset_combo(select=name)
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    def _on_save_as(self) -> None:
        name, ok = QInputDialog.getText(self, "Save preset as", "Preset name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Save failed", "Preset name cannot be empty.")
            return
        if name in themes_module.DEFAULT_PRESETS:
            QMessageBox.warning(self, "Save failed", "That name collides with a built-in preset.")
            return
        try:
            self._state = themes_module.upsert_preset(self._state, name, self._editor_payload())
            themes_module.save_state(self._state)
            self._reload_preset_combo(select=name)
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Save failed", str(exc))

    def _on_delete(self) -> None:
        name = self._current_name()
        if not name or name in themes_module.DEFAULT_PRESETS:
            return
        confirm = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete preset '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self._state = themes_module.delete_preset(self._state, name)
            themes_module.save_state(self._state)
            self._reload_preset_combo(select=self._state.get("active"))
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Delete failed", str(exc))

    def _on_set_active(self) -> None:
        name = self._current_name()
        if not name:
            return
        try:
            self._state = themes_module.set_active(self._state, name)
            themes_module.save_state(self._state)
            self._update_active_label()
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Set active failed", str(exc))

    def _on_apply_typography(self) -> None:
        # Read editor controls into normalized state and persist.
        family_text = (self.typo_family_combo.currentText() or "").strip()
        if family_text == "(native default)":
            family_text = ""
        size_pt = self.typo_size_combo.currentData() or 0
        try:
            self._state = themes_module.set_typography(
                self._state, ui_family=family_text, base_size_pt=int(size_pt)
            )
            themes_module.save_state(self._state)
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Typography failed", str(exc))

    # ------------------------------------------------------------------
    # Rename / Export / Import (incl. drag-drop)
    # ------------------------------------------------------------------
    def _on_rename(self) -> None:
        old_name = self._current_name()
        if not old_name or old_name in themes_module.DEFAULT_PRESETS:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename preset", "New preset name:", text=old_name
        )
        if not ok:
            return
        new_name = (new_name or "").strip()
        if not new_name or new_name == old_name:
            return
        try:
            self._state = themes_module.rename_preset(self._state, old_name, new_name)
            themes_module.save_state(self._state)
            self._reload_preset_combo(select=new_name)
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Rename failed", str(exc))

    def _on_export(self) -> None:
        name = self._current_name()
        if not name:
            return
        preset = (self._state.get("presets") or {}).get(name) or {}
        qss = themes_module.read_qss(preset)
        if qss is None:
            QMessageBox.information(
                self,
                "Export",
                "This preset has no QSS payload to export. Only 'qss' and 'qss_file' kinds can be exported.",
            )
            return
        suggested = f"{name.lower().replace(' ', '-')}.qss"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export preset to .qss", suggested, "Qt Stylesheets (*.qss);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(qss)
            QMessageBox.information(self, "Export complete", f"Wrote {path}")
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import a .qss file", "", "Qt Stylesheets (*.qss);;All files (*)"
        )
        if not path:
            return
        self._import_qss_path(path)

    def _import_qss_path(self, path: str) -> None:
        suggested = self._suggested_name_from_path(path)
        name, ok = QInputDialog.getText(
            self, "Import .qss", "Preset name:", text=suggested
        )
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(self, "Import failed", "Preset name cannot be empty.")
            return
        try:
            self._state = themes_module.import_qss_from_file(self._state, path, name)
            themes_module.save_state(self._state)
            self._reload_preset_combo(select=name)
            self.theme_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))

    @staticmethod
    def _suggested_name_from_path(path: str) -> str:
        base = (path or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base.lower().endswith(".qss"):
            base = base[:-4]
        # Title-case the slug-ish remnant for friendliness.
        return " ".join(part.capitalize() for part in base.replace("_", "-").split("-") if part) or "Imported"

    # ---- drag-drop ----
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and path.lower().endswith(".qss"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt API)
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(".qss"):
                event.acceptProposedAction()
                self._import_qss_path(path)
                return
        event.ignore()
