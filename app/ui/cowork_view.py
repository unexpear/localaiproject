"""CoWorkView · Qt UI · live progress widget for cowork runs.

Shows the active run for a session prominently (status + per-sub-task
table + synthesis preview), plus a list of past runs that can be
clicked to inspect. Refreshes on every ``KIND_COWORK`` bus event so
the user sees sub-tasks transition from queued → done in real time
during execution.

Design: ``docs/design/cowork-mode.md`` Phase 4 (UI). Mirrors the
LinkedView pattern: signal-marshalled bus events, 50ms refresh
debounce, deterministic-test-friendly via the offscreen Qt platform.
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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.memory import local_memory
from app.services.command_dispatcher import dispatch_input
from app.services.gateway import GatewayEvent, default_bus


# Status icon glyphs — chosen to render identically in monospace fonts
# and not rely on emoji rendering (Qt offscreen test runs don't have
# the system emoji font loaded).
_STATUS_GLYPH = {
    "queued":     "·",
    "in_progress": "▶",
    "done":       "✓",
    "failed":     "✗",
    "skipped":    "—",
    "(pending)":  " ",
}


class CoWorkView(QDialog):
    """Live progress widget for cowork runs.

    Two stacked sections:
      - **Active run** at the top: header (id, status, goal), sub-task
        table with status icons, synthesis preview when present.
      - **Past runs** at the bottom: clickable list of historical runs;
        clicking one promotes it to the "active" pane (read-only — the
        actual active run is determined by ``local_memory.active_cowork_run``).

    The dialog can outlive any single run; closing it unsubscribes from
    the bus so we don't leak GatewayEvent callbacks.
    """

    closed = Signal()
    bus_event = Signal(object)  # marshals GatewayEvent → Qt main thread

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        initial_session: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("CoWork")
        self.setObjectName("cowork_view")
        self.resize(960, 720)
        self.setModal(False)
        self._current_session: str = ""
        self._displayed_run_id: Optional[str] = None
        self._bus_subscription_ids: list[int] = []
        self._build_ui()
        self._populate_session_combo(prefer=initial_session)
        self.bus_event.connect(self._on_bus_event)
        # Subscribe to ALL events; we filter to KIND_COWORK + the
        # current session in `_on_bus_event`.
        self._bus_subscription_ids.append(
            default_bus().subscribe(default_bus().ANY, self._enqueue_bus_event)
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        # --- Top bar: session picker + cancel + refresh ---
        top = QHBoxLayout()
        top.addWidget(QLabel("Session:"))
        self.session_combo = QComboBox()
        self.session_combo.currentTextChanged.connect(self._on_session_changed)
        top.addWidget(self.session_combo)

        self.status_label = QLabel("(no active run)")
        self.status_label.setStyleSheet(
            "color: palette(mid); font-size: 10pt; padding-left: 12px;"
        )
        top.addWidget(self.status_label, 1)

        self.cancel_btn = QPushButton("Cancel run")
        self.cancel_btn.setToolTip("Abort the active cowork run on this session")
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        self.cancel_btn.setEnabled(False)
        top.addWidget(self.cancel_btn)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)
        outer.addLayout(top)

        # --- Splitter: active run (top) + past runs (bottom) ---
        splitter = QSplitter(Qt.Vertical)
        outer.addWidget(splitter, 1)

        # Top: active run pane
        active_box = QWidget()
        active_layout = QVBoxLayout(active_box)
        active_layout.setContentsMargins(0, 0, 0, 0)
        active_layout.setSpacing(8)

        self.run_header = QLabel("(no run displayed)")
        font = QFont()
        font.setBold(True)
        self.run_header.setFont(font)
        active_layout.addWidget(self.run_header)

        self.subtask_table = QTableWidget(0, 4)
        self.subtask_table.setHorizontalHeaderLabels(
            ["", "ID", "Builder", "Task"]
        )
        self.subtask_table.verticalHeader().setVisible(False)
        self.subtask_table.setSelectionMode(QTableWidget.NoSelection)
        self.subtask_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.subtask_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch
        )
        self.subtask_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        active_layout.addWidget(self.subtask_table, 1)

        synth_label = QLabel("Synthesis")
        synth_label.setStyleSheet("color: palette(mid); font-size: 9pt;")
        active_layout.addWidget(synth_label)
        self.synthesis_view = QTextEdit()
        self.synthesis_view.setReadOnly(True)
        self.synthesis_view.setMaximumHeight(140)
        active_layout.addWidget(self.synthesis_view)

        splitter.addWidget(active_box)

        # Bottom: past runs list
        past_box = QWidget()
        past_layout = QVBoxLayout(past_box)
        past_layout.setContentsMargins(0, 8, 0, 0)
        past_layout.setSpacing(4)
        past_header = QLabel("Past runs (click to inspect)")
        past_header.setStyleSheet("color: palette(mid); font-size: 9pt;")
        past_layout.addWidget(past_header)
        self.past_runs_list = QListWidget()
        self.past_runs_list.itemActivated.connect(self._on_past_run_clicked)
        self.past_runs_list.itemClicked.connect(self._on_past_run_clicked)
        past_layout.addWidget(self.past_runs_list)
        splitter.addWidget(past_box)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        # --- Footer ---
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        outer.addWidget(close_box)

    # ------------------------------------------------------------------
    # Session selection
    # ------------------------------------------------------------------

    def _populate_session_combo(self, prefer: Optional[str] = None) -> None:
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        sessions = local_memory.list_sessions() or ["default"]
        for sid in sessions:
            self.session_combo.addItem(sid)
        self.session_combo.blockSignals(False)
        if prefer and prefer in sessions:
            self.session_combo.setCurrentText(prefer)
        elif "default" in sessions:
            self.session_combo.setCurrentText("default")
        elif sessions:
            self.session_combo.setCurrentText(sessions[0])
        self._on_session_changed(self.session_combo.currentText())

    def _on_session_changed(self, session_id: str) -> None:
        self._current_session = (session_id or "").strip()
        self._displayed_run_id = None
        self.refresh()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        if not self._current_session:
            self._render_empty()
            return
        active = local_memory.active_cowork_run(self._current_session)
        run_to_show = None
        if self._displayed_run_id:
            # User clicked a past run; keep showing it.
            run_to_show = local_memory.get_cowork_run(self._displayed_run_id)
        if run_to_show is None:
            # Prefer the active run; fall back to the most recent past
            # run so the user always has *something* useful to look at
            # (synthesis, sub-task outcomes) on a fresh open.
            run_to_show = active
        if run_to_show is None:
            past = local_memory.list_cowork_runs(session_id=self._current_session)
            if past:
                run_to_show = past[-1]  # newest last in storage order
        self._render_run(run_to_show, active)
        self._render_past_runs()

    def _render_empty(self) -> None:
        self.status_label.setText("(no session selected)")
        self.run_header.setText("(no run displayed)")
        self.subtask_table.setRowCount(0)
        self.synthesis_view.setPlainText("")
        self.past_runs_list.clear()
        self.cancel_btn.setEnabled(False)

    def _render_run(self, run: Optional[dict], active: Optional[dict]) -> None:
        if run is None:
            self.status_label.setText(
                f"No active run for '{self._current_session}'."
            )
            self.run_header.setText("(no run displayed)")
            self.subtask_table.setRowCount(0)
            self.synthesis_view.setPlainText("")
            self.cancel_btn.setEnabled(False)
            return

        # Header text
        is_active = active is not None and run["id"] == active["id"]
        header_label = "ACTIVE" if is_active else "Run"
        self.run_header.setText(
            f"{header_label} · {run['id']} · {run['status']} · {run['goal']}"
        )

        # Status banner
        plan_count = len(run.get("plan") or [])
        results = run.get("subtask_results") or {}
        done = sum(1 for r in results.values() if r.get("status") == "done")
        failed = sum(
            1 for r in results.values()
            if r.get("status") in {"failed", "skipped"}
        )
        self.status_label.setText(
            f"{run['status']} · {done}/{plan_count} done"
            + (f" · {failed} failed/skipped" if failed else "")
        )

        # Sub-task table
        plan = run.get("plan") or []
        self.subtask_table.setRowCount(len(plan))
        for row, sub in enumerate(plan):
            sub_id = sub["id"]
            result = results.get(sub_id, {})
            status = result.get("status") or "(pending)"
            glyph = _STATUS_GLYPH.get(status, "?")

            glyph_item = QTableWidgetItem(glyph)
            glyph_item.setTextAlignment(Qt.AlignCenter)
            glyph_item.setToolTip(status)
            self.subtask_table.setItem(row, 0, glyph_item)

            id_item = QTableWidgetItem(sub_id)
            self.subtask_table.setItem(row, 1, id_item)

            builder_item = QTableWidgetItem(sub.get("builder", ""))
            self.subtask_table.setItem(row, 2, builder_item)

            task_text = sub.get("task", "")
            if sub.get("acceptance"):
                task_text += f"  (acceptance: {sub['acceptance']})"
            task_item = QTableWidgetItem(task_text)
            task_item.setToolTip(task_text)
            self.subtask_table.setItem(row, 3, task_item)

        # Synthesis
        synth = run.get("synthesis") or ""
        self.synthesis_view.setPlainText(synth)

        # Cancel button: only enabled when we're showing the active run
        # AND it's still in flight.
        cancellable = is_active and run["status"] in {
            "planning", "approved", "executing", "synthesizing",
        }
        self.cancel_btn.setEnabled(cancellable)

    def _render_past_runs(self) -> None:
        self.past_runs_list.clear()
        runs = local_memory.list_cowork_runs(session_id=self._current_session)
        # Newest first
        for run in reversed(runs):
            label = (
                f"[{run['status']:<11}]  {run['id']}  ·  "
                f"{run['goal'][:80]}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, run["id"])
            self.past_runs_list.addItem(item)

    # ------------------------------------------------------------------
    # Past-run click → display
    # ------------------------------------------------------------------

    def _on_past_run_clicked(self, item: QListWidgetItem) -> None:
        run_id = item.data(Qt.UserRole) if item else None
        if not run_id:
            return
        self._displayed_run_id = run_id
        self.refresh()

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def _on_cancel_clicked(self) -> None:
        if not self._current_session:
            return
        # Use the dispatcher (single source of truth) so the cancel
        # event also goes through the bus.
        dispatch_input("cowork cancel", session_id=self._current_session)
        QTimer.singleShot(50, self.refresh)

    # ------------------------------------------------------------------
    # Bus integration
    # ------------------------------------------------------------------

    def _enqueue_bus_event(self, event: GatewayEvent) -> None:
        """Called from arbitrary threads; just emit the signal so the
        Qt event loop picks it up on the main thread."""
        try:
            self.bus_event.emit(event)
        except RuntimeError:
            # The dialog may have been deleted between subscription and
            # publish; that's fine — Qt would raise on emit.
            pass

    def _on_bus_event(self, event: GatewayEvent) -> None:
        # Filter: only refresh on KIND_COWORK events for the current
        # session. Other event kinds are ignored.
        if not self._current_session:
            return
        if getattr(event, "kind", "") != "cowork":
            return
        if event.session_id != self._current_session:
            return
        # Coalesce bursts: if many subtask_done events arrive in
        # sequence we still only refresh once on the next tick.
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
