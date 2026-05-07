"""PromptAssemblyView · Qt UI · prompt-assembly inspector.

Live readout of the layered prompt slots for a session's NEXT chat
turn — see ``docs/design/prompt-assembly.md`` Phase 4 (UI). Mirrors
the ``CoWorkView`` pattern: signal-marshalled bus events, refresh
debounce, deterministic-test-friendly via the offscreen Qt platform.

The view shows, per slot:

* Name, priority, char count, char_budget (or '∞' for unbounded)
* A simple bar chart showing chars-consumed-vs-budget when bounded,
  or chars-consumed-vs-total-budget when not.
* Truncated / Evicted indicators + an OVER_BUDGET banner when the
  pinned slots alone exceed total_budget.

Auto-refreshes 50 ms after any ``KIND_DISPATCH`` event for the
current session — chat turns + dispatcher commands both fire that
kind, so the trace stays current without manual reload.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.memory import local_memory
from app.services import chat_service, prompt_assembly as pa
from app.services.gateway import GatewayEvent, default_bus


class PromptAssemblyView(QDialog):
    """Read-only panel showing the slot trace for a session's next
    chat turn. Useful for debugging "why is the model ignoring my
    prompt" or "which slot got evicted".

    The panel is non-modal so it can stay open while the user issues
    chat turns — it auto-refreshes on every KIND_DISPATCH bus event
    for the selected session, so the slot bars update live as the
    user runs commands."""

    closed = Signal()
    bus_event = Signal(object)  # marshals GatewayEvent → Qt main thread

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        initial_session: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Prompt Assembly")
        self.setObjectName("prompt_assembly_view")
        self.resize(720, 540)
        self.setModal(False)
        self._current_session: str = ""
        self._bus_subscription_ids: list[int] = []
        self._build_ui()
        self._populate_session_combo(prefer=initial_session)
        self.bus_event.connect(self._on_bus_event)
        # Subscribe to ALL events; filter to KIND_DISPATCH + current
        # session in `_on_bus_event`. KIND_DISPATCH covers chat turns
        # (which most affect the trace) plus any other dispatcher
        # command (which can also change context: workspace pin,
        # skill toggle, prompt set-budget, etc.).
        self._bus_subscription_ids.append(
            default_bus().subscribe(default_bus().ANY, self._enqueue_bus_event)
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        # --- Top bar: session picker + refresh ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Session:"))
        self.session_combo = QComboBox()
        self.session_combo.currentTextChanged.connect(self._on_session_changed)
        top.addWidget(self.session_combo)

        top.addStretch(1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self.refresh_btn)
        outer.addLayout(top)

        # --- Total-budget readout + over-budget banner ---
        self.total_label = QLabel("Total: 0 / 0 chars")
        self.total_label.setStyleSheet("font-weight: bold; padding: 4px 0;")
        outer.addWidget(self.total_label)

        self.total_bar = QProgressBar()
        self.total_bar.setRange(0, 100)
        self.total_bar.setValue(0)
        self.total_bar.setTextVisible(True)
        self.total_bar.setFormat("%p% of total budget")
        outer.addWidget(self.total_bar)

        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet(
            "color: #c0392b; font-weight: bold; padding: 4px 0;"
        )
        self.warning_label.setVisible(False)
        outer.addWidget(self.warning_label)

        # --- Per-slot table ---
        self.slot_table = QTableWidget(0, 5)
        self.slot_table.setHorizontalHeaderLabels(
            ["Slot", "Priority", "Chars", "Budget", "Status"]
        )
        self.slot_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.slot_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.slot_table.verticalHeader().setVisible(False)
        self.slot_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.slot_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.slot_table.setFont(QFont("Consolas, monospace", 9))
        outer.addWidget(self.slot_table, 1)

        # --- Footer summary ---
        self.footer_label = QLabel("")
        self.footer_label.setStyleSheet("color: palette(mid); font-size: 9pt;")
        outer.addWidget(self.footer_label)

        # --- Close button ---
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.close)
        outer.addWidget(button_box)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def _populate_session_combo(self, prefer: Optional[str] = None) -> None:
        sessions = sorted(local_memory.list_sessions() or [])
        if not sessions:
            sessions = ["default"]
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItems(sessions)
        target = prefer if prefer in sessions else sessions[0]
        self.session_combo.setCurrentText(target)
        self.session_combo.blockSignals(False)
        self._current_session = target
        self.refresh()

    def _on_session_changed(self, session_id: str) -> None:
        self._current_session = session_id or "default"
        self.refresh()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the slot table for the current session. Cheap;
        no model call, just an in-memory ``build_runtime_assembly``
        with an empty user message."""
        sid = self._current_session
        if not sid:
            return
        try:
            context = local_memory.get_session_context(session_id=sid)
            summary = local_memory.get_session_summary(session_id=sid) or {}
            history = local_memory.get_history(session_id=sid, limit=0)
            _, trace = chat_service.build_runtime_assembly(
                summary, history, "", session_context=context, session_id=sid,
            )
        except Exception as exc:  # noqa: BLE001
            self.total_label.setText(f"Error building assembly: {exc}")
            self.slot_table.setRowCount(0)
            return

        # Strip the synthetic _diagnostics entry; surface its flag
        # via the warning banner.
        visible = [t for t in trace if t.get("name") != "_diagnostics"]
        over_budget = pa.trace_over_budget(trace)

        total_budget = (
            local_memory.get_prompt_total_budget(session_id=sid)
            or pa.DEFAULT_TOTAL_BUDGET
        )
        rendered = pa.live_total_chars(visible)
        pct = min(100, int(round(100 * rendered / total_budget))) if total_budget else 0

        self.total_label.setText(
            f"Total: {rendered:,} / {total_budget:,} chars  ·  "
            f"{len(visible)} slots"
        )
        self.total_bar.setValue(pct)
        # Tint the bar red when we're over 100% of budget.
        if over_budget:
            self.total_bar.setStyleSheet(
                "QProgressBar::chunk { background-color: #c0392b; }"
            )
        elif pct > 90:
            self.total_bar.setStyleSheet(
                "QProgressBar::chunk { background-color: #e67e22; }"
            )
        else:
            self.total_bar.setStyleSheet("")

        if over_budget:
            self.warning_label.setText(
                "⚠ prompt_budget_exceeded — pinned slots alone exceed "
                "total_budget. Raise the budget or trim a pinned slot."
            )
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

        # Repopulate the slot table.
        self.slot_table.setRowCount(len(visible))
        for row, entry in enumerate(visible):
            name_item = QTableWidgetItem(str(entry.get("name", "?")))
            prio_item = QTableWidgetItem(str(entry.get("priority", 0)))
            prio_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            chars = int(entry.get("chars", 0) or 0)
            chars_item = QTableWidgetItem(f"{chars:,}")
            chars_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            budget = int(entry.get("char_budget", 0) or 0)
            budget_item = QTableWidgetItem(f"{budget:,}" if budget else "∞")
            budget_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            flags = []
            if entry.get("truncated"):
                flags.append("TRUNCATED")
            if entry.get("evicted"):
                flags.append("EVICTED")
            status_item = QTableWidgetItem(" · ".join(flags) if flags else "")

            # Color rows that have pressure.
            if entry.get("evicted"):
                for it in (name_item, prio_item, chars_item, budget_item, status_item):
                    it.setForeground(Qt.GlobalColor.gray)
            elif entry.get("truncated"):
                for it in (name_item, prio_item, chars_item, budget_item, status_item):
                    it.setForeground(Qt.GlobalColor.darkRed)

            self.slot_table.setItem(row, 0, name_item)
            self.slot_table.setItem(row, 1, prio_item)
            self.slot_table.setItem(row, 2, chars_item)
            self.slot_table.setItem(row, 3, budget_item)
            self.slot_table.setItem(row, 4, status_item)

        footer = pa.render_trace_footer(trace)
        self.footer_label.setText(footer)
        self.footer_label.setVisible(bool(footer))

    # ------------------------------------------------------------------
    # Bus integration
    # ------------------------------------------------------------------

    def _enqueue_bus_event(self, event: GatewayEvent) -> None:
        """Called from arbitrary threads; just emit the signal so
        the Qt event loop picks it up on the main thread."""
        try:
            self.bus_event.emit(event)
        except RuntimeError:
            # The dialog may have been deleted between subscription
            # and publish; that's fine.
            pass

    def _on_bus_event(self, event: GatewayEvent) -> None:
        # Refresh on KIND_DISPATCH (chat turns + any other dispatcher
        # command) for the current session; ignore other event kinds.
        if not self._current_session:
            return
        if getattr(event, "kind", "") != "dispatch":
            return
        if event.session_id != self._current_session:
            return
        # Coalesce bursts.
        QTimer.singleShot(50, self.refresh)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API name)
        for sub_id in self._bus_subscription_ids:
            try:
                default_bus().unsubscribe(sub_id)
            except Exception:  # noqa: BLE001
                pass
        self._bus_subscription_ids.clear()
        self.closed.emit()
        super().closeEvent(event)
