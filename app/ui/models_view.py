"""ModelsView · PySide6 dialog rendering the registered lanes in Foundry style.

Reads from app.services.model_service and app.models.model_registry to build
each row. Per-lane Install / Uninstall buttons emit a string command
("install vision" / "uninstall vision") via the `lane_action` signal — the
main window wires it to its existing async install pipeline.

Visual language is QSS-driven (foundry.qss); this file only sets object names
and role properties. Use the same widget for any other theme.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.hardware import summary as hardware_summary
from app.models import model_registry
from app.services.model_service import get_status


_FIT_LABEL = {
    "safe": "safe",
    "tight": "tight",
    "too_big": "too big",
    "unknown": "unknown",
}


def _format_ram(ram_bytes):
    if not ram_bytes:
        return "unknown"
    return f"{ram_bytes / (1024 ** 3):.1f} GB"


class ModelsView(QDialog):
    """Lane registry inspector. Emit lane_action with 'install <role>' / 'uninstall <role>'.

    When a pull is in flight, the main window forwards progress events via
    `set_pull_progress(event)` and clears them via `clear_pull_progress()`.
    The relevant lane row swaps its Install button for a Cancel button and
    the state label streams the percentage live; clicking Cancel emits
    `cancel_requested`, which the main window wires to its cancel_event.
    """

    lane_action = Signal(str)
    cancel_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Models")
        self.setObjectName("models_view")
        self.resize(920, 700)
        self.setModal(False)

        # Live pull state — only one install runs at a time.
        self._active_pull_model: Optional[str] = None
        self._active_pull_status: str = ""
        self._active_pull_percent: Optional[int] = None

        self._build_ui()
        self.refresh()

    # ---------- layout ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Hardware banner
        self.banner_label = QLabel("")
        self.banner_label.setObjectName("models_banner")
        self.banner_label.setProperty("role", "banner")
        self.banner_label.setWordWrap(False)
        outer.addWidget(self.banner_label)

        # Section header: Registered Lanes
        head = QLabel("Registered Lanes")
        head.setProperty("role", "pane-head")
        head.setObjectName("section_lanes")
        outer.addWidget(head)

        # Scrollable lane container
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self.lanes_container = QWidget()
        self.lanes_layout = QVBoxLayout(self.lanes_container)
        self.lanes_layout.setContentsMargins(0, 0, 0, 0)
        self.lanes_layout.setSpacing(0)
        scroll.setWidget(self.lanes_container)
        outer.addWidget(scroll, 1)

        # Other models section
        self.extras_head = QLabel("Other Local Models")
        self.extras_head.setProperty("role", "pane-head")
        self.extras_head.setObjectName("section_extras")
        outer.addWidget(self.extras_head)

        self.extras_label = QLabel("")
        self.extras_label.setObjectName("extras_label")
        self.extras_label.setWordWrap(True)
        self.extras_label.setStyleSheet(
            # fallback styling for default theme; foundry.qss can override
            "padding: 12px 24px; font-family: 'JetBrains Mono', 'Consolas', monospace; "
            "font-size: 11pt; color: palette(text);"
        )
        outer.addWidget(self.extras_label)

        # Footer
        footer_box = QWidget()
        footer = QHBoxLayout(footer_box)
        footer.setContentsMargins(18, 10, 18, 10)
        footer.setSpacing(8)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        footer.addWidget(self.refresh_button)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("models_summary")
        self.summary_label.setStyleSheet(
            "font-family: 'JetBrains Mono', monospace; font-size: 10pt; color: palette(mid);"
        )
        footer.addWidget(self.summary_label, 1)

        self.install_defaults_button = QPushButton("Install Defaults")
        self.install_defaults_button.setProperty("role", "primary")
        self.install_defaults_button.clicked.connect(
            lambda: self.lane_action.emit("install-defaults")
        )
        footer.addWidget(self.install_defaults_button)

        outer.addWidget(footer_box)

    # ---------- data ----------
    def refresh(self) -> None:
        status = get_status()
        hardware = hardware_summary()
        ram_bytes = hardware.get("ram")
        fits = model_registry.get_hardware_lane_fit(ram_bytes=ram_bytes)
        installed = {entry["model"] for entry in status.get("installed", [])}

        # Banner
        runtime = "connected" if status.get("runtime_available") else "offline"
        version = status.get("version") or "unknown"
        ready = [r for r, info in model_registry.MODEL_REGISTRY.items() if info["model"] in installed]
        missing = [r for r, info in model_registry.MODEL_REGISTRY.items()
                   if info["required"] and info["model"] not in installed]
        optional = [r for r, info in model_registry.MODEL_REGISTRY.items()
                    if not info["required"] and info["model"] not in installed]
        gpu = hardware.get("gpu") or "not detected"
        self.banner_label.setText(
            f"  Ollama: {runtime} · {version}     "
            f"RAM: {_format_ram(ram_bytes)}     "
            f"GPU: {gpu}     "
            f"Lanes: {len(ready)} ready · {len(missing)} missing · {len(optional)} optional"
        )

        # Lane rows
        self._clear_layout(self.lanes_layout)
        for role, info in model_registry.MODEL_REGISTRY.items():
            row = self._build_lane_row(role, info, installed, fits.get(role, {}))
            self.lanes_layout.addWidget(row)
        self.lanes_layout.addStretch(1)

        # Other (unmanaged) models
        registered = {info["model"] for info in model_registry.MODEL_REGISTRY.values()}
        extras = [m for m in status.get("detected", []) if m not in registered]
        if extras:
            self.extras_head.setVisible(True)
            self.extras_label.setVisible(True)
            self.extras_label.setText("\n".join(f"  · {m}" for m in extras))
        else:
            self.extras_head.setVisible(False)
            self.extras_label.setVisible(False)

        # Footer summary
        total = len(model_registry.MODEL_REGISTRY)
        installed_count = sum(1 for info in model_registry.MODEL_REGISTRY.values() if info["model"] in installed)
        self.summary_label.setText(
            f"  {installed_count} / {total} lanes installed · {len(extras)} other local models"
        )

    # ---------- rows ----------
    def _build_lane_row(self, role, info, installed, fit) -> QWidget:
        wrapper = QWidget()
        h = QHBoxLayout(wrapper)
        h.setContentsMargins(20, 12, 20, 12)
        h.setSpacing(14)

        # Role + required/optional
        role_box = QWidget()
        role_layout = QVBoxLayout(role_box)
        role_layout.setContentsMargins(0, 0, 0, 0)
        role_layout.setSpacing(2)
        role_label = QLabel(role.upper())
        role_label.setStyleSheet(
            "font-family: 'Inter', 'Segoe UI', sans-serif; font-weight: 600; letter-spacing: 2px;"
        )
        req_label = QLabel("required" if info["required"] else "optional")
        req_label.setStyleSheet(
            "font-family: 'Inter', sans-serif; font-size: 9pt; letter-spacing: 2px; color: palette(mid);"
        )
        role_layout.addWidget(role_label)
        role_layout.addWidget(req_label)
        role_box.setMinimumWidth(110)
        role_box.setMaximumWidth(110)
        h.addWidget(role_box)

        # Model name
        model_label = QLabel(info["model"])
        model_label.setObjectName("model_name")
        model_label.setFont(QFont("JetBrains Mono", 11))
        model_label.setMinimumWidth(220)
        h.addWidget(model_label)

        # Size
        size_label = QLabel(f"{info['size_gb']:.1f} GB")
        size_label.setFont(QFont("JetBrains Mono", 11))
        size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_label.setMinimumWidth(70)
        h.addWidget(size_label)

        # Fit chip
        fit_tier = fit.get("tier", "unknown")
        fit_chip = QLabel(_FIT_LABEL[fit_tier].upper())
        fit_chip.setStyleSheet(self._fit_chip_qss(fit_tier))
        fit_chip.setAlignment(Qt.AlignCenter)
        fit_chip.setMinimumWidth(80)
        fit_chip.setMaximumWidth(100)
        h.addWidget(fit_chip)

        # State
        state_text = self._state_text(role, info, installed)
        is_active_pull = info["model"] == self._active_pull_model
        if is_active_pull:
            state_text = self._active_pull_status or state_text
        state_label = QLabel(state_text)
        state_label.setObjectName("lane_state")
        state_label.setFont(QFont("Inter", 10))
        state_label.setMinimumWidth(180)
        if is_active_pull:
            # Tint state text orange to signal active pull; QSS can override via objectName.
            state_label.setStyleSheet("color: #F56600;")
        h.addWidget(state_label, 1)

        # Action button
        is_installed = info["model"] in installed
        if is_active_pull:
            btn = QPushButton("Cancel")
            btn.setObjectName("lane_cancel")
            btn.setStyleSheet(
                "QPushButton { color: #F56600; border: 1px solid #F56600;"
                " font-family: 'Inter', sans-serif; font-weight: 600;"
                " letter-spacing: 2px; text-transform: uppercase;"
                " padding: 4px 14px; border-radius: 2px; background: transparent; }"
                "QPushButton:hover { background: rgba(245, 102, 0, 0.12); }"
            )
            btn.clicked.connect(lambda _checked=False: self.cancel_requested.emit())
        elif is_installed:
            btn = QPushButton("Uninstall")
            btn.setProperty("role", "danger")
            btn.clicked.connect(lambda _checked=False, r=role: self.lane_action.emit(f"uninstall {r}"))
        else:
            btn = QPushButton("Install")
            btn.setProperty("role", "primary")
            btn.clicked.connect(lambda _checked=False, r=role: self.lane_action.emit(f"install {r}"))
        btn.setMinimumWidth(96)
        h.addWidget(btn)

        return wrapper

    def _state_text(self, role, info, installed) -> str:
        if info["model"] in installed:
            return "Installed"
        if info["required"]:
            return "Missing required"
        warn = "no safety filter — local use only" if role == "unfiltered" else "Not installed"
        return warn

    def _fit_chip_qss(self, tier: str) -> str:
        base = (
            "font-family: 'Inter', sans-serif; font-size: 9pt; letter-spacing: 2px;"
            " padding: 2px 8px; border-radius: 1px; border: 1px solid palette(mid);"
        )
        if tier == "safe":
            return base + "color: #93b88a; border-color: rgba(147,184,138,0.4);"
        if tier == "tight":
            return base + "color: #F56600; border-color: rgba(245,102,0,0.5);"
        if tier == "too_big":
            return base + "color: #B43A3A; border-color: rgba(180,58,58,0.5);"
        return base + "color: palette(mid);"

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    # ------------------------------------------------------------------
    # Live pull progress (driven by the main window's progress callback)
    # ------------------------------------------------------------------
    def set_pull_progress(self, event: dict) -> None:
        """Receive a single pull event from the main window and update state.

        Recognised event shapes:
          {phase: "queue", models: [...]}
          {phase: "start", model: name, index: i, total: n}
          {phase: "blocked", model: name, reason: str}
          {phase: "retry", model: name}
          {phase: "cancelled"}
          {phase: "done", model: name, success: bool, output: str}
          {status: "downloading", completed: int, total: int, model: name, ...}
        """
        if not isinstance(event, dict):
            return
        phase = event.get("phase")
        model = event.get("model")
        if phase == "start" and model:
            self._active_pull_model = model
            self._active_pull_status = "Starting…"
            self._active_pull_percent = 0
            self.refresh()
            return
        if phase == "retry" and model:
            self._active_pull_model = model
            self._active_pull_status = "Retrying…"
            self.refresh()
            return
        if phase == "blocked" and model:
            self._active_pull_status = f"Blocked: {event.get('reason', '')}"
            self.refresh()
            return
        if phase in {"done", "cancelled"}:
            self.clear_pull_progress()
            return
        # Raw Ollama stream event
        if model:
            self._active_pull_model = model
        total = event.get("total")
        completed = event.get("completed")
        status = event.get("status") or self._active_pull_status
        if isinstance(total, int) and total > 0 and isinstance(completed, int):
            percent = max(0, min(100, int(completed / total * 100)))
            self._active_pull_percent = percent
            mb_done = completed / (1024 ** 2)
            mb_total = total / (1024 ** 2)
            if mb_total >= 1024:
                self._active_pull_status = (
                    f"{status} · {percent}% · "
                    f"{mb_done / 1024:.2f}/{mb_total / 1024:.2f} GB"
                )
            else:
                self._active_pull_status = (
                    f"{status} · {percent}% · "
                    f"{mb_done:.0f}/{mb_total:.0f} MB"
                )
        elif status:
            self._active_pull_status = status
        self.refresh()

    def clear_pull_progress(self) -> None:
        self._active_pull_model = None
        self._active_pull_status = ""
        self._active_pull_percent = None
        self.refresh()
