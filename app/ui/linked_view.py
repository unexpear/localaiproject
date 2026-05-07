"""LinkedView · Qt UI · planner / task-queue / builders side-by-side.

Three-column layout:

    +-------------------+----------------+--------------------------+
    |   PLANNER pane    |   task queue   |    BUILDER tabs          |
    |   transcript      |   (filterable) |   one tab per linked     |
    |   input + Send    |   refresh      |   builder, each with its |
    |   + Dispatch      |                |   own transcript + input |
    +-------------------+----------------+--------------------------+

Wires straight into the existing dispatch_input() so every action goes
through the same code path the CLI uses — no duplicated business logic.
The view re-loads transcripts / queue after each action; there is no
streaming notification system in this round.
"""
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.memory import local_memory
from app.services.command_dispatcher import dispatch_input
from app.services.gateway import GatewayEvent, default_bus


_HISTORY_LIMIT = 30
_TASK_LIMIT = 100
_FILTER_OPTIONS = ("all", "queued", "in_progress", "done", "failed", "skipped")


class _ChatPane(QWidget):
    """Reusable chat surface: transcript + input row.

    Emits `submit(text)` on Send. The dialog wires this to the right
    dispatcher invocation depending on whether this is the planner or a
    builder pane.
    """

    submit = Signal(str)

    def __init__(self, role_label: str, session_id: str, parent=None) -> None:
        super().__init__(parent)
        self._session_id = session_id
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        head = QLabel(role_label)
        head.setProperty("role", "pane-head")
        head_font = QFont()
        head_font.setBold(True)
        head.setFont(head_font)
        outer.addWidget(head)

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Consolas", 10))
        outer.addWidget(self.transcript, 1)

        input_row = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Message this session…")
        self.input_field.returnPressed.connect(self._on_submit)
        input_row.addWidget(self.input_field, 1)
        send_btn = QPushButton("Send")
        send_btn.clicked.connect(self._on_submit)
        input_row.addWidget(send_btn)
        outer.addLayout(input_row)

    @property
    def session_id(self) -> str:
        return self._session_id

    def set_session(self, session_id: str) -> None:
        self._session_id = session_id

    def _on_submit(self) -> None:
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self.submit.emit(text)

    def render_history(self, history: List[dict]) -> None:
        self.transcript.clear()
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.End)
        for msg in history:
            role = (msg.get("role") or "").strip() or "?"
            content = (msg.get("content") or "").strip()
            speaker = role.capitalize()
            cursor.insertHtml(f"<b>{speaker}</b><br>")
            cursor.insertText(content + "\n\n")
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()


class _BuilderTab(QWidget):
    """One tab in the builder column — chat pane + per-builder task controls."""

    chat_submit = Signal(str, str)        # (builder_session, text)
    run_next = Signal(str)                # (builder_session)
    run_all = Signal(str)                 # (builder_session)
    skip = Signal(str)                    # (builder_session)

    def __init__(self, builder_id: str, parent=None) -> None:
        super().__init__(parent)
        self._builder_id = builder_id
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self.chat_pane = _ChatPane(f"BUILDER · {builder_id}", builder_id)
        self.chat_pane.submit.connect(lambda text: self.chat_submit.emit(self._builder_id, text))
        outer.addWidget(self.chat_pane, 1)

        # Task control row
        button_row = QHBoxLayout()
        self.queue_label = QLabel("Inbox: 0 queued")
        self.queue_label.setStyleSheet("color: palette(mid); font-size: 10pt;")
        button_row.addWidget(self.queue_label)
        button_row.addStretch(1)

        self.run_next_btn = QPushButton("Run next")
        self.run_next_btn.clicked.connect(lambda: self.run_next.emit(self._builder_id))
        button_row.addWidget(self.run_next_btn)

        self.run_all_btn = QPushButton("Run all")
        self.run_all_btn.setProperty("role", "primary")
        self.run_all_btn.clicked.connect(lambda: self.run_all.emit(self._builder_id))
        button_row.addWidget(self.run_all_btn)

        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setProperty("role", "danger")
        self.skip_btn.clicked.connect(lambda: self.skip.emit(self._builder_id))
        button_row.addWidget(self.skip_btn)
        outer.addLayout(button_row)

    @property
    def builder_id(self) -> str:
        return self._builder_id

    def render_history(self, history: List[dict]) -> None:
        self.chat_pane.render_history(history)

    def update_queue_label(self, queued_count: int, in_progress_count: int) -> None:
        bits = [f"Inbox: {queued_count} queued"]
        if in_progress_count:
            bits.append(f"{in_progress_count} in progress")
        self.queue_label.setText(" · ".join(bits))


class _TaskQueuePane(QWidget):
    """Middle column · filterable task list across all builders for this planner."""

    refresh_requested = Signal()
    task_clicked = Signal(str)  # task_id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        head = QLabel("TASKS")
        head_font = QFont()
        head_font.setBold(True)
        head.setFont(head_font)
        head.setProperty("role", "pane-head")
        outer.addWidget(head)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(_FILTER_OPTIONS)
        self.filter_combo.currentTextChanged.connect(lambda _t: self.refresh_requested.emit())
        filter_row.addWidget(self.filter_combo, 1)
        outer.addLayout(filter_row)

        self.task_list = QListWidget()
        self.task_list.setFont(QFont("Consolas", 9))
        self.task_list.itemSelectionChanged.connect(self._on_selection)
        outer.addWidget(self.task_list, 1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(lambda: self.refresh_requested.emit())
        outer.addWidget(refresh_btn)

    def current_filter(self) -> Optional[str]:
        sel = self.filter_combo.currentText()
        return None if sel == "all" else sel

    def render(self, tasks: List[dict]) -> None:
        self.task_list.clear()
        if not tasks:
            QListWidgetItem("(no tasks match this filter)", self.task_list)
            return
        for task in tasks:
            short = (task.get("task") or "").replace("\n", " ").strip()
            if len(short) > 50:
                short = short[:47] + "..."
            label = (
                f"{task.get('id', '?')[-6:]}  "
                f"{task.get('status', '?'):<11}  "
                f"-> {task.get('builder_session', '?'):<10}  "
                f"{short}"
            )
            item = QListWidgetItem(label, self.task_list)
            item.setData(Qt.UserRole, task.get("id"))

    def _on_selection(self) -> None:
        item = self.task_list.currentItem()
        if not item:
            return
        task_id = item.data(Qt.UserRole)
        if task_id:
            self.task_clicked.emit(task_id)


class LinkedView(QDialog):
    """Planner / task-queue / builders side-by-side surface."""

    closed = Signal()
    bus_event = Signal(object)  # marshals GatewayEvent from any thread to UI thread

    def __init__(self, parent=None, initial_planner: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Linked Sessions")
        self.setObjectName("linked_view")
        self.resize(1280, 760)
        self.setModal(False)
        self._current_planner = ""
        self._bus_subscription_ids: list[int] = []
        self._build_ui()
        self._populate_planner_combo(prefer=initial_planner)
        self.bus_event.connect(self._on_bus_event)
        # Listen to ALL events; we filter in _on_bus_event so that re-subscribing
        # on planner change is unnecessary.
        self._bus_subscription_ids.append(
            default_bus().subscribe(default_bus().ANY, self._enqueue_bus_event)
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        # ---- Top bar: planner picker + refresh + close ----
        top = QHBoxLayout()
        top.addWidget(QLabel("Planner session:"))
        self.planner_combo = QComboBox()
        self.planner_combo.currentTextChanged.connect(self._on_planner_changed)
        top.addWidget(self.planner_combo)

        self.builders_summary = QLabel("Builders: —")
        self.builders_summary.setStyleSheet("color: palette(mid); font-size: 10pt; padding-left: 12px;")
        top.addWidget(self.builders_summary, 1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)
        outer.addLayout(top)

        # ---- Three columns ----
        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        # Planner pane
        self.planner_pane = _ChatPane("PLANNER · —", "")
        self.planner_pane.submit.connect(self._on_planner_send)

        # Planner-specific dispatch row (sits below the planner pane's input)
        planner_box = QWidget()
        planner_layout = QVBoxLayout(planner_box)
        planner_layout.setContentsMargins(8, 8, 8, 8)
        planner_layout.setSpacing(6)
        planner_layout.addWidget(self.planner_pane, 1)

        dispatch_row = QHBoxLayout()
        self.dispatch_input = QLineEdit()
        self.dispatch_input.setPlaceholderText("Task to dispatch (will be queued, not sent as chat)…")
        self.dispatch_input.returnPressed.connect(self._on_dispatch_clicked)
        dispatch_row.addWidget(self.dispatch_input, 1)

        self.dispatch_target = QComboBox()
        self.dispatch_target.setMinimumWidth(180)
        self.dispatch_target.setEditable(False)
        dispatch_row.addWidget(self.dispatch_target)

        self.dispatch_btn = QPushButton("Dispatch")
        self.dispatch_btn.setProperty("role", "primary")
        self.dispatch_btn.clicked.connect(self._on_dispatch_clicked)
        dispatch_row.addWidget(self.dispatch_btn)
        planner_layout.addLayout(dispatch_row)

        splitter.addWidget(planner_box)

        # Task queue pane
        self.task_pane = _TaskQueuePane()
        self.task_pane.refresh_requested.connect(self._refresh_tasks_only)
        splitter.addWidget(self.task_pane)

        # Builder tabs
        self.builder_tabs = QTabWidget()
        self.builder_tabs.setMovable(False)
        self.builder_tabs.setTabsClosable(False)
        splitter.addWidget(self.builder_tabs)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 5)

        # ---- Footer ----
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        outer.addWidget(close_box)

    # ------------------------------------------------------------------
    # State / population
    # ------------------------------------------------------------------
    def _populate_planner_combo(self, prefer: Optional[str] = None) -> None:
        self.planner_combo.blockSignals(True)
        self.planner_combo.clear()
        sessions = local_memory.list_sessions()
        for sid in sessions:
            self.planner_combo.addItem(sid)
        self.planner_combo.blockSignals(False)
        if prefer and prefer in sessions:
            self.planner_combo.setCurrentText(prefer)
        elif "default" in sessions:
            self.planner_combo.setCurrentText("default")
        elif sessions:
            self.planner_combo.setCurrentText(sessions[0])
        # currentTextChanged is now signalled — kicks the refresh path.
        if self.planner_combo.currentText():
            self._on_planner_changed(self.planner_combo.currentText())

    def _on_planner_changed(self, planner: str) -> None:
        planner = (planner or "").strip()
        if not planner:
            return
        self._current_planner = planner
        self.planner_pane.set_session(planner)
        # Re-label the planner pane head
        head = self.planner_pane.findChild(QLabel)
        if head is not None:
            head.setText(f"PLANNER · {planner}")
        self.refresh()

    def refresh(self) -> None:
        planner = self._current_planner
        if not planner:
            return
        builders = local_memory.get_builders(planner)
        self._refresh_summary(planner, builders)
        self._refresh_dispatch_target_combo(builders)
        self._rebuild_builder_tabs(builders)
        self._refresh_planner_history(planner)
        for builder in builders:
            self._refresh_builder_history(builder)
            self._refresh_builder_queue_label(builder)
        self._refresh_tasks_only()

    def _refresh_tasks_only(self) -> None:
        if not self._current_planner:
            return
        status = self.task_pane.current_filter()
        tasks = local_memory.list_tasks(self._current_planner, status=status)[:_TASK_LIMIT]
        self.task_pane.render(tasks)

    def _refresh_summary(self, planner: str, builders: List[str]) -> None:
        if not builders:
            self.builders_summary.setText(
                f"Builders: none — use `link <builder>` in main chat to set one up."
            )
        else:
            self.builders_summary.setText(
                f"Builders: {len(builders)} — {', '.join(builders)}"
            )

    def _refresh_dispatch_target_combo(self, builders: List[str]) -> None:
        self.dispatch_target.blockSignals(True)
        self.dispatch_target.clear()
        if builders:
            if len(builders) > 1:
                self.dispatch_target.addItem("(fan out to all)")
            for b in builders:
                self.dispatch_target.addItem(f"@{b}")
        else:
            self.dispatch_target.addItem("(no builders linked)")
        self.dispatch_target.blockSignals(False)

    def _rebuild_builder_tabs(self, builders: List[str]) -> None:
        # Remove tabs whose builder is no longer in the list, add missing ones.
        existing = {self.builder_tabs.tabText(i): i for i in range(self.builder_tabs.count())}
        # Remove stale
        for label, idx in list(existing.items())[::-1]:
            if label not in builders:
                self.builder_tabs.removeTab(idx)
        # Add missing
        present = {self.builder_tabs.tabText(i) for i in range(self.builder_tabs.count())}
        for b in builders:
            if b in present:
                continue
            tab = _BuilderTab(b)
            tab.chat_submit.connect(self._on_builder_send)
            tab.run_next.connect(self._on_run_next)
            tab.run_all.connect(self._on_run_all)
            tab.skip.connect(self._on_skip)
            self.builder_tabs.addTab(tab, b)
        if self.builder_tabs.count() == 0:
            placeholder = QLabel(
                "No builders linked.\n\nUse `link <builder>` in the main chat to set one up,\n"
                "then come back to dispatch tasks here."
            )
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setStyleSheet("color: palette(mid); font-size: 11pt;")
            self.builder_tabs.addTab(placeholder, "(none)")

    def _refresh_planner_history(self, planner: str) -> None:
        history = local_memory.get_history(session_id=planner, limit=_HISTORY_LIMIT)
        self.planner_pane.render_history(history)

    def _refresh_builder_history(self, builder: str) -> None:
        tab = self._tab_for_builder(builder)
        if tab is None:
            return
        history = local_memory.get_history(session_id=builder, limit=_HISTORY_LIMIT)
        tab.render_history(history)

    def _refresh_builder_queue_label(self, builder: str) -> None:
        tab = self._tab_for_builder(builder)
        if tab is None:
            return
        queued = len(local_memory.get_inbox(builder, status="queued"))
        in_prog = 1 if local_memory.in_progress_task_for(builder) else 0
        tab.update_queue_label(queued, in_prog)

    def _tab_for_builder(self, builder: str) -> Optional[_BuilderTab]:
        for i in range(self.builder_tabs.count()):
            if self.builder_tabs.tabText(i) == builder:
                widget = self.builder_tabs.widget(i)
                if isinstance(widget, _BuilderTab):
                    return widget
        return None

    # ------------------------------------------------------------------
    # Action handlers — every one routes through dispatch_input so the
    # business logic stays in one place.
    # ------------------------------------------------------------------
    def _run_dispatcher(self, text: str, session_id: str) -> dict:
        try:
            return dispatch_input(text, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Action failed", str(exc))
            return {"action": "error", "session_id": session_id, "output": str(exc)}

    def _on_planner_send(self, text: str) -> None:
        self._run_dispatcher(text, self._current_planner)
        self._refresh_planner_history(self._current_planner)

    def _on_dispatch_clicked(self) -> None:
        text = self.dispatch_input.text().strip()
        if not text:
            return
        target = self.dispatch_target.currentText().strip()
        if target.startswith("@"):
            command = f"dispatch {target} {text}"
        else:
            command = f"dispatch {text}"
        ev = self._run_dispatcher(command, self._current_planner)
        if ev.get("output"):
            # Show the dispatcher's confirmation in the planner transcript so the
            # user sees something happened.
            local_memory.append_message(
                role="system",
                content=ev["output"],
                session_id=self._current_planner,
            )
            self._refresh_planner_history(self._current_planner)
        self.dispatch_input.clear()
        self._refresh_tasks_only()
        for builder in local_memory.get_builders(self._current_planner):
            self._refresh_builder_queue_label(builder)

    def _on_builder_send(self, builder: str, text: str) -> None:
        self._run_dispatcher(text, builder)
        self._refresh_builder_history(builder)

    def _on_run_next(self, builder: str) -> None:
        self._run_dispatcher("inbox next", builder)
        self._refresh_builder_history(builder)
        self._refresh_planner_history(self._current_planner)
        self._refresh_builder_queue_label(builder)
        self._refresh_tasks_only()

    def _on_run_all(self, builder: str) -> None:
        self._run_dispatcher("inbox auto", builder)
        self._refresh_builder_history(builder)
        self._refresh_planner_history(self._current_planner)
        self._refresh_builder_queue_label(builder)
        self._refresh_tasks_only()

    def _on_skip(self, builder: str) -> None:
        self._run_dispatcher("inbox skip", builder)
        self._refresh_builder_queue_label(builder)
        self._refresh_tasks_only()

    # ------------------------------------------------------------------
    # Bus integration — refresh on any dispatch event involving sessions
    # we're currently displaying (planner or any of its builders).
    # ------------------------------------------------------------------
    def _enqueue_bus_event(self, event: GatewayEvent) -> None:
        """Called from arbitrary threads (HTTP gateway / other clients).
        Queue-drop into the Qt main thread via the signal."""
        self.bus_event.emit(event)

    def _on_bus_event(self, event: GatewayEvent) -> None:
        if not self._current_planner:
            return
        watched = {self._current_planner, *local_memory.get_builders(self._current_planner)}
        if event.session_id not in watched:
            return
        # Coalesce bursts: if many events arrive in quick succession only
        # refresh once on the next event-loop tick.
        QTimer.singleShot(50, self.refresh)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        for sub_id in self._bus_subscription_ids:
            try:
                default_bus().unsubscribe(sub_id)
            except Exception:
                pass
        self._bus_subscription_ids.clear()
        self.closed.emit()
        super().closeEvent(event)
