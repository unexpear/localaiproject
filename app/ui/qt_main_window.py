"""PySide6 main window for the Local AI Control Layer.

Feature parity with the legacy tkinter window (`app/ui/main_window.py`):
  - Transcript pane + input row
  - Quick-action buttons (status/models/doctor/onboard/executions/approve/deny)
  - Install/uninstall row with async pull + cancel
  - Progress bar + status text
  - Executions list + detail
  - Status banner
  - Startup prompt for missing required models

The flow graph is a simple read-only card list in this version (no dragging).
"""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.memory import local_memory
from app.services.command_dispatcher import dispatch_input
from app.services.execution_service import build_execution_graph, format_execution_detail
from app.services.model_service import (
    ensure_models_installed,
    format_install_results,
    format_single_install_result,
    format_uninstall_result,
    get_status,
    install_single_model,
    startup_summary,
    uninstall_single_model,
)
from app.services.gateway import GatewayEvent, default_bus
from app.ui import themes as themes_module
from app.ui.linked_view import LinkedView
from app.ui.cowork_view import CoWorkView
from app.ui.prompt_assembly_view import PromptAssemblyView
from app.ui.models_view import ModelsView
from app.ui.settings_dialog import SettingsDialog
from app.ui.image_drop_input import ImageDropInput


WINDOW_TITLE = "Local AI Control Layer"


def _input_label(session_id: str) -> str:
    context = local_memory.get_session_context(session_id=session_id)
    workspace_path = context.get("workspace_path")
    if not workspace_path:
        return f"You ({session_id}):"
    workspace_name = os.path.basename(workspace_path.rstrip("\\/")) or workspace_path
    return f"You ({session_id} @ {workspace_name}):"


def _execution_row_label(execution: dict) -> str:
    status = str(execution.get("status") or "unknown").replace("_", " ")
    lane = execution.get("resolved_lane") or execution.get("requested_lane") or "unknown"
    preview = execution.get("message_preview") or execution.get("message") or ""
    preview = preview.replace("\n", " ").replace("\r", " ").strip()
    if len(preview) > 64:
        preview = preview[:61].rstrip() + "..."
    return f"{execution.get('id', '(no id)')} | {status} | {lane} | {preview}"


class LocalAIQtWindow(QMainWindow):
    pull_event_signal = Signal(dict)
    install_finished_signal = Signal(str)
    bus_event_signal = Signal(object)  # marshals GatewayEvent from any thread to UI thread

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1440, 920)

        self.session_id = "default"
        self.execution_rows: list[dict] = []
        self.current_execution: dict | None = None
        self._install_in_flight = False
        self._install_cancel_event: threading.Event | None = None
        self._models_view: ModelsView | None = None
        self._linked_view: LinkedView | None = None
        self._cowork_view: CoWorkView | None = None
        self._prompt_assembly_view: PromptAssemblyView | None = None

        # Bus-driven live refresh state.
        # `_last_rendered_history_count` tracks how many history entries we've
        # already painted into the transcript so the bus refresh can append
        # only the new ones (instead of re-rendering everything every time).
        self._bus_subscription_id: int | None = None
        self._last_rendered_history_count = 0
        self._refresh_pending = False

        self._build_ui()
        self._wire_signals()

        self._append_transcript("Control", startup_summary())
        self._refresh_status_banner()
        self._refresh_execution_list()

        # Defer the missing-models prompt slightly so the window is shown first.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(250, self._maybe_prompt_for_missing_models)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(0)

        # ---- chassis bar (brand + LED + session strip + settings) ----
        chassis = QWidget()
        chassis.setObjectName("chassis_bar")
        chassis.setFixedHeight(36)
        chassis_layout = QHBoxLayout(chassis)
        chassis_layout.setContentsMargins(18, 0, 8, 0)
        chassis_layout.setSpacing(10)

        self.led = QLabel()
        self.led.setObjectName("led")
        self.led.setFixedSize(8, 8)
        self.led.setStyleSheet(self._led_qss("idle"))
        chassis_layout.addWidget(self.led)

        brand = QLabel("LOCAL AI CONTROL LAYER")
        brand.setObjectName("brand")
        brand.setStyleSheet(
            "font-family: 'Inter', 'Segoe UI', sans-serif; font-weight: 600;"
            " font-size: 10pt; letter-spacing: 3px; color: palette(text);"
        )
        chassis_layout.addWidget(brand)

        chassis_layout.addStretch(1)

        self.session_strip = QLabel("")
        self.session_strip.setObjectName("session_strip")
        self.session_strip.setStyleSheet(
            "font-family: 'JetBrains Mono', 'Consolas', monospace;"
            " font-size: 10pt; color: palette(mid);"
        )
        chassis_layout.addWidget(self.session_strip)

        chassis_layout.addStretch(1)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("settings_button")
        self.settings_button.clicked.connect(self._open_settings)
        chassis_layout.addWidget(self.settings_button)
        outer.addWidget(chassis)

        # ---- segmented status banner ----
        banner = QWidget()
        banner.setObjectName("banner")
        banner.setProperty("role", "banner")
        banner.setFixedHeight(32)
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(18, 0, 18, 0)
        banner_layout.setSpacing(0)

        self.banner_segments: dict[str, QLabel] = {}
        for key in ("ollama", "lanes", "missing", "skills", "approval"):
            seg, value = self._make_banner_segment(key)
            self.banner_segments[key] = value
            banner_layout.addWidget(seg)
            if key != "approval":
                sep = QLabel("·")
                sep.setStyleSheet("color: #4a4d52; padding: 0 12px;")
                banner_layout.addWidget(sep)
        banner_layout.addStretch(1)
        outer.addWidget(banner)

        # Keep the legacy status_label name pointing at a hidden composite for
        # backward compatibility with existing _refresh_status_banner callers.
        self.status_label = QLabel("")
        self.status_label.setVisible(False)

        # spacer between banner and splitter
        spacer = QWidget()
        spacer.setFixedHeight(8)
        outer.addWidget(spacer)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        # ------ Left: chat ------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.transcript = QTextEdit()
        self.transcript.setObjectName("transcript")
        self.transcript.setReadOnly(True)
        self.transcript.setFont(QFont("Consolas", 10))
        left_layout.addWidget(self.transcript, 1)

        # Quick actions
        quick_row = QHBoxLayout()
        quick_row.addWidget(self._row_label("Quick"))
        for label, command in (
            ("Status", "status"),
            ("Models", "models"),
            ("Doctor", "doctor"),
            ("Onboard", "onboard"),
            ("Executions", "executions"),
            ("Approve", "approve"),
            ("Deny", "deny"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, c=command: self._dispatch_text(c))
            quick_row.addWidget(btn)

        linked_btn = QPushButton("Linked")
        linked_btn.setToolTip("Open the planner / task-queue / builders side-by-side view")
        linked_btn.clicked.connect(self._open_linked_view)
        quick_row.addWidget(linked_btn)

        cowork_btn = QPushButton("CoWork")
        cowork_btn.setToolTip(
            "Open the cowork progress view — live sub-task states for "
            "the active multi-step run on this session."
        )
        cowork_btn.clicked.connect(self._open_cowork_view)
        quick_row.addWidget(cowork_btn)

        prompt_btn = QPushButton("Prompt")
        prompt_btn.setToolTip(
            "Open the prompt-assembly inspector — see exactly which slots "
            "the model would receive on the next chat turn, with budgets "
            "and truncation/eviction indicators."
        )
        prompt_btn.clicked.connect(self._open_prompt_assembly_view)
        quick_row.addWidget(prompt_btn)

        quick_row.addStretch(1)
        left_layout.addLayout(quick_row)

        # Install / uninstall
        install_row = QHBoxLayout()
        install_row.addWidget(self._row_label("Models"))
        for label, command in (
            ("Install Vision", "install vision"),
            ("Install Unfiltered", "install unfiltered"),
            ("Uninstall Vision", "uninstall vision"),
            ("Uninstall Unfiltered", "uninstall unfiltered"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, c=command: self._dispatch_text(c))
            if command.startswith("uninstall "):
                btn.setProperty("role", "danger")
            install_row.addWidget(btn)
        install_row.addStretch(1)
        left_layout.addLayout(install_row)

        # Progress + Cancel
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progress_bar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_row.addWidget(self.progress_bar, 1)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("progress_label")
        self.progress_label.setMinimumWidth(360)
        progress_row.addWidget(self.progress_label)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancel_button")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_install)
        progress_row.addWidget(self.cancel_button)
        left_layout.addLayout(progress_row)

        # Input row — drag-drop image attachments turn into
        # ``@image:<rel-path>`` tokens (vision-input.md Phase 2).
        input_row = QHBoxLayout()
        self.input_field = ImageDropInput(
            workspace_path_provider=self._current_workspace_path,
        )
        self.input_field.setObjectName("input_field")
        self.input_field.returnPressed.connect(self._on_submit)
        self.input_field.image_drop_rejected.connect(self._on_image_drop_rejected)
        input_row.addWidget(self.input_field, 1)
        send = QPushButton("Send")
        send.setProperty("role", "primary")
        send.clicked.connect(self._on_submit)
        input_row.addWidget(send)
        left_layout.addLayout(input_row)

        splitter.addWidget(left)

        # ------ Right: executions ------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_header = QHBoxLayout()
        ex_label = QLabel("Executions")
        ex_label.setProperty("role", "pane-head")
        ex_font = QFont()
        ex_font.setBold(True)
        ex_font.setPointSize(11)
        ex_label.setFont(ex_font)
        right_header.addWidget(ex_label)
        right_header.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_execution_list)
        right_header.addWidget(refresh_btn)
        right_layout.addLayout(right_header)

        self.execution_list = QListWidget()
        self.execution_list.setObjectName("execution_list")
        self.execution_list.setFont(QFont("Consolas", 9))
        self.execution_list.itemSelectionChanged.connect(self._on_execution_selected)
        right_layout.addWidget(self.execution_list, 1)

        detail_label = QLabel("Execution Detail")
        detail_label.setProperty("role", "pane-head")
        detail_label.setFont(ex_font)
        right_layout.addWidget(detail_label)
        self.detail_text = QTextEdit()
        self.detail_text.setObjectName("detail_text")
        self.detail_text.setReadOnly(True)
        self.detail_text.setFont(QFont("Consolas", 9))
        right_layout.addWidget(self.detail_text, 1)

        flow_label = QLabel("Execution Flow")
        flow_label.setProperty("role", "pane-head")
        flow_label.setFont(ex_font)
        right_layout.addWidget(flow_label)
        self.flow_list = QListWidget()
        self.flow_list.setObjectName("flow_list")
        right_layout.addWidget(self.flow_list, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.setStatusBar(QStatusBar())

    def _wire_signals(self) -> None:
        # Cross-thread updates use queued connections by default for inter-thread signals.
        self.pull_event_signal.connect(self._apply_pull_event)
        self.install_finished_signal.connect(self._finish_async_install)

        # Live refresh: subscribe to every bus event; filter in the slot so we
        # automatically pick up session changes (`session <name>`) without
        # needing to resubscribe.
        self.bus_event_signal.connect(self._on_bus_event)
        self._bus_subscription_id = default_bus().subscribe(
            default_bus().ANY, self._enqueue_bus_event
        )

    # ------------------------------------------------------------------
    # Layout helpers (LED, segmented banner, row labels)
    # ------------------------------------------------------------------
    _LED_COLORS = {
        # state -> (fill, glow_alpha)
        "active":    ("#F56600", 0.45),   # install in flight
        "online":    ("#93B88A", 0.0),    # ollama connected, idle
        "offline":   ("#B43A3A", 0.0),    # ollama unreachable
        "idle":      ("#6B6457", 0.0),    # unknown / startup
    }

    def _led_qss(self, state: str) -> str:
        fill, _ = self._LED_COLORS.get(state, self._LED_COLORS["idle"])
        return (
            f"background: {fill}; border-radius: 4px; min-width: 8px; min-height: 8px;"
        )

    def _set_led(self, state: str) -> None:
        if hasattr(self, "led"):
            self.led.setStyleSheet(self._led_qss(state))
            self.led.setToolTip({
                "active":  "install in flight",
                "online":  "Ollama connected",
                "offline": "Ollama unreachable",
                "idle":    "starting up",
            }.get(state, state))

    def _make_banner_segment(self, key: str):
        """Return (container, value_label) — caller drives value text via banner_segments[key]."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        k_label = QLabel({
            "ollama":   "OLLAMA",
            "lanes":    "LANES",
            "missing":  "MISSING",
            "skills":   "SKILLS",
            "approval": "PENDING",
        }.get(key, key.upper()))
        k_label.setStyleSheet(
            "font-family: 'Inter', sans-serif; font-weight: 500;"
            " font-size: 9pt; letter-spacing: 2px; color: palette(mid);"
        )
        layout.addWidget(k_label)
        v_label = QLabel("")
        v_label.setStyleSheet(
            "font-family: 'JetBrains Mono', 'Consolas', monospace;"
            " font-size: 10pt; color: palette(text);"
        )
        layout.addWidget(v_label)
        return container, v_label

    def _row_label(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setProperty("role", "actions-label")
        label.setStyleSheet(
            "font-family: 'Inter', sans-serif; font-weight: 500;"
            " font-size: 9.5pt; letter-spacing: 2.5px; color: palette(mid);"
            " padding-right: 8px;"
        )
        return label

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------
    def _open_settings(self) -> None:
        dialog = SettingsDialog(self)
        dialog.theme_changed.connect(self._apply_theme_now)
        dialog.exec()

    def _apply_theme_now(self) -> None:
        state = themes_module.load_state()
        _, preset = themes_module.get_active_preset(state)
        app = QApplication.instance()
        if app is not None:
            themes_module.apply_to_app(
                app, preset, typography=themes_module.get_typography(state)
            )

    # ------------------------------------------------------------------
    # Models view
    # ------------------------------------------------------------------
    def _open_models_view(self) -> None:
        if self._models_view is None or not self._models_view.isVisible():
            self._models_view = ModelsView(self)
            self._models_view.lane_action.connect(self._handle_models_action)
            self._models_view.cancel_requested.connect(self._cancel_install)
        else:
            self._models_view.refresh()
        self._models_view.show()
        self._models_view.raise_()
        self._models_view.activateWindow()

    def _open_cowork_view(self) -> None:
        """Open or focus the CoWork live-progress dialog."""
        if self._cowork_view is None or not self._cowork_view.isVisible():
            self._cowork_view = CoWorkView(self, initial_session=self.session_id)
        else:
            self._cowork_view.refresh()
        self._cowork_view.show()
        self._cowork_view.raise_()
        self._cowork_view.activateWindow()

    def _open_prompt_assembly_view(self) -> None:
        """Open or focus the layered-prompt inspector dialog. Phase 4
        of `docs/design/prompt-assembly.md`."""
        if (
            self._prompt_assembly_view is None
            or not self._prompt_assembly_view.isVisible()
        ):
            self._prompt_assembly_view = PromptAssemblyView(
                self, initial_session=self.session_id
            )
        else:
            self._prompt_assembly_view.refresh()
        self._prompt_assembly_view.show()
        self._prompt_assembly_view.raise_()
        self._prompt_assembly_view.activateWindow()

    def _open_linked_view(self) -> None:
        if self._linked_view is None or not self._linked_view.isVisible():
            self._linked_view = LinkedView(self, initial_planner=self.session_id)
        else:
            self._linked_view.refresh()
        self._linked_view.show()
        self._linked_view.raise_()
        self._linked_view.activateWindow()

    @Slot(str)
    def _handle_models_action(self, command: str) -> None:
        # Mirror the user typing the install/uninstall command.
        self._append_transcript("You", command)
        self._run_async_install(command)
        if self._models_view is not None:
            # Refresh the view after the worker enqueues; the lane state will
            # update again when the install finishes via _refresh_status_banner.
            self._models_view.refresh()

    # ------------------------------------------------------------------
    # Transcript / status
    # ------------------------------------------------------------------
    def _append_transcript(self, speaker: str, text: str) -> None:
        rendered = (text or "").strip()
        if not rendered:
            return
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(f"<b>{speaker}</b><br>")
        cursor.insertText(rendered + "\n\n")
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    # ------------------------------------------------------------------
    # Bus-driven live refresh
    # ------------------------------------------------------------------
    def _enqueue_bus_event(self, event: GatewayEvent) -> None:
        """Bus subscriber — runs on whatever thread published. Marshal to UI thread."""
        try:
            self.bus_event_signal.emit(event)
        except RuntimeError:
            # Window already destroyed, signal not deliverable — bus will be
            # cleaned up when closeEvent fires.
            pass

    @Slot(object)
    def _on_bus_event(self, event: GatewayEvent) -> None:
        # Only react to events for the session we're currently watching.
        # Events with no session_id (system-wide) are also picked up because
        # they're often things like gateway lifecycle that don't affect us.
        if event is None or event.session_id is None:
            return
        if event.session_id != self.session_id:
            return
        # Coalesce bursts: if many events arrive in a 50 ms window, do one
        # refresh rather than thrashing.
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QTimer.singleShot(50, self._do_bus_refresh)

    def _do_bus_refresh(self) -> None:
        self._refresh_pending = False
        try:
            self._sync_history_from_storage()
        except Exception:
            pass
        try:
            self._refresh_execution_list()
        except Exception:
            pass
        try:
            self._refresh_status_banner()
        except Exception:
            pass

    def _sync_history_from_storage(self) -> None:
        """Append any history entries written since the last paint.

        Tracking by message-count works because history is append-only — the
        existing entries never shift indices, so once we've painted entries
        [0, _last_rendered_history_count) we can skip them on the next pass.
        """
        history = local_memory.get_history(session_id=self.session_id)
        total = len(history)
        if total <= self._last_rendered_history_count:
            return
        new_entries = history[self._last_rendered_history_count:]
        for entry in new_entries:
            role = (entry.get("role") or "").strip() or "?"
            content = (entry.get("content") or "").strip()
            if not content:
                continue
            speaker = role.capitalize()
            model = entry.get("model")
            if model and role == "assistant":
                speaker = f"{speaker} · {model}"
            self._append_transcript(speaker, content)
        self._last_rendered_history_count = total

    def _switch_session(self, new_session_id: str) -> None:
        """Re-render transcript + reset history counter for a new session."""
        self.session_id = new_session_id
        self.transcript.clear()
        self._last_rendered_history_count = 0
        self._sync_history_from_storage()
        self._refresh_execution_list()
        self._refresh_status_banner()

    def _refresh_status_banner(self) -> None:
        status = get_status()
        context = local_memory.get_session_context(session_id=self.session_id)
        workspace_path = context.get("workspace_path")
        workspace_label = os.path.basename(workspace_path.rstrip("\\/")) if workspace_path else "none"
        missing_list = [entry["model"] for entry in status.get("missing", [])]
        skills_list = context.get("active_skills", []) or []
        runtime_ok = bool(status.get("runtime_available"))
        version = status.get("version") or "unknown"
        pending = context.get("pending_approval")

        # LED precedence: install in flight > offline > online > idle
        if self._install_in_flight:
            self._set_led("active")
        elif not runtime_ok:
            self._set_led("offline")
        elif runtime_ok:
            self._set_led("online")
        else:
            self._set_led("idle")

        # Session strip in the chassis bar
        if hasattr(self, "session_strip"):
            self.session_strip.setText(
                f"session  {self.session_id}   ·   workspace  {workspace_label}"
                + (f"   ·   skills  {','.join(skills_list)}" if skills_list else "")
            )

        # Segmented banner
        if hasattr(self, "banner_segments"):
            self.banner_segments["ollama"].setText(
                f"connected · {version}" if runtime_ok else "offline"
            )
            installed_roles = [entry["role"] for entry in status.get("installed", [])]
            self.banner_segments["lanes"].setText(
                ", ".join(installed_roles) if installed_roles else "none"
            )
            self.banner_segments["missing"].setText(
                ", ".join(missing_list) if missing_list else "none"
            )
            self.banner_segments["skills"].setText(
                ", ".join(skills_list) if skills_list else "none"
            )
            self.banner_segments["approval"].setText(
                "approval queued" if pending else "none"
            )
            # Pending approval text gets the warning tint
            tint = "#F56600" if pending else "palette(text)"
            self.banner_segments["approval"].setStyleSheet(
                "font-family: 'JetBrains Mono', monospace;"
                f" font-size: 10pt; color: {tint};"
            )

        # Legacy single-line label preserved for any external callers/tests
        self.status_label.setText(
            f"Session: {self.session_id} | Workspace: {workspace_label} | Skills: {','.join(skills_list) or 'none'} | "
            f"Ollama: {'connected' if runtime_ok else 'offline'} | Missing defaults: {','.join(missing_list) or 'none'}"
        )
        self.setWindowTitle(f"{WINDOW_TITLE} - {self.session_id}")

    # ------------------------------------------------------------------
    # Submission flow
    # ------------------------------------------------------------------
    def _on_submit(self) -> None:
        text = self.input_field.text().strip()
        if not text:
            return
        self.input_field.clear()
        self._dispatch_text(text)

    # ------------------------------------------------------------------
    # Image drop helpers (vision-input.md Phase 2)
    # ------------------------------------------------------------------
    def _current_workspace_path(self) -> str | None:
        """Best-effort lookup of the active session's pinned
        workspace path. Returns None if local_memory is in a
        weird state — the drop widget treats that as
        ``REJECT_NO_WORKSPACE``."""
        try:
            ctx = local_memory.get_session_context(self.session_id)
            return ctx.get("workspace_path") if isinstance(ctx, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _on_image_drop_rejected(self, absolute_path: str, reason: str) -> None:
        """Surface a one-line rejection message inline. Phase 2
        renders into the existing transcript area; Phase 4 (Qt
        thumbnail strip) will replace this with a richer surface."""
        # Pretty messages mirror the REJECT_* constants in
        # image_drop_input.
        labels = {
            "no_workspace": "drop refused (no workspace pinned — use `workspace <path>` first)",
            "outside_workspace": "drop refused (file is outside the pinned workspace)",
            "unsupported_format": "drop refused (not a supported image format)",
            "not_a_file": "drop refused (not a file)",
        }
        label = labels.get(reason, f"drop refused ({reason})")
        # Append a hint into the transcript so the user sees why
        # nothing happened. Defensive — if transcript got torn
        # down mid-dispatch, just no-op.
        try:
            self._append_transcript(
                "[image drop]",
                f"{os.path.basename(absolute_path)}: {label}",
            )
        except Exception:  # noqa: BLE001
            pass

    def _dispatch_text(self, text: str) -> None:
        stripped = text.strip()
        lowered = stripped.lower().lstrip("/")

        # GUI shortcut: the Models quick-action opens the ModelsView dialog
        # instead of dumping a text overview into the transcript.
        if lowered == "models":
            self._open_models_view()
            return

        # Async install path: synthetic local echo only, runs in a worker.
        if lowered == "install-defaults" or lowered.startswith("install ") or lowered.startswith("uninstall "):
            self._append_transcript(_input_label(self.session_id), text)
            self._run_async_install(lowered)
            return

        # Synchronous dispatch.
        event = dispatch_input(text, session_id=self.session_id)

        # Detect server-side session change (`session <name>`) and re-render.
        new_session = event.get("session_id", self.session_id)
        if new_session != self.session_id:
            self._switch_session(new_session)

        action = event.get("action")
        if action == "chat":
            # The user message + the assistant reply are both in storage now.
            # The bus event handler will paint them on the next tick. Skip
            # the local echo so we don't render the user message twice.
            pass
        else:
            # Non-chat: nothing was written to history, so emit synthetic echoes
            # for both the user input and the dispatcher's output.
            self._append_transcript(_input_label(self.session_id), text)
            if event.get("output"):
                self._append_transcript("Control", event["output"])

        self._refresh_status_banner()

        preferred_id = None
        if action == "execution" and isinstance(event.get("result"), dict):
            preferred_id = event["result"].get("id")
        self._refresh_execution_list(
            select_latest=action == "chat",
            preferred_id=preferred_id,
        )

        if action == "exit":
            self.close()

    # ------------------------------------------------------------------
    # Async install / uninstall
    # ------------------------------------------------------------------
    def _run_async_install(self, command: str) -> None:
        if self._install_in_flight:
            self._append_transcript("Control", "An install is already running; please wait for it to finish.")
            return
        self._install_in_flight = True
        self._install_cancel_event = threading.Event()
        cancel_event = self._install_cancel_event
        self._set_progress(0, "Starting...")
        cancellable = command == "install-defaults" or command.startswith("install ")
        self.cancel_button.setEnabled(cancellable)
        self._refresh_status_banner()  # flip LED to active

        def emit_event(event: dict) -> None:
            self.pull_event_signal.emit(dict(event))

        def worker() -> None:
            try:
                if command == "install-defaults":
                    results = ensure_models_installed(progress_callback=emit_event, cancel_event=cancel_event)
                    output = format_install_results(results)
                elif command.startswith("install "):
                    target = command[len("install "):].strip()
                    result = install_single_model(target, progress_callback=emit_event, cancel_event=cancel_event)
                    output = format_single_install_result(result)
                elif command.startswith("uninstall "):
                    target = command[len("uninstall "):].strip()
                    result = uninstall_single_model(target)
                    output = format_uninstall_result(result)
                else:
                    output = f"Unknown install command: {command}"
            except Exception as exc:
                output = f"Install error: {exc}"
            self.install_finished_signal.emit(output)

        threading.Thread(target=worker, daemon=True).start()

    @Slot()
    def _cancel_install(self) -> None:
        if not self._install_in_flight or self._install_cancel_event is None:
            return
        self._install_cancel_event.set()
        self.cancel_button.setEnabled(False)
        self._set_progress(None, "Cancelling...")

    @Slot(str)
    def _finish_async_install(self, output: str) -> None:
        self._install_in_flight = False
        self._install_cancel_event = None
        self._set_progress(0, "")
        self.cancel_button.setEnabled(False)
        self._append_transcript("Control", output)
        self._refresh_status_banner()
        if self._models_view is not None and self._models_view.isVisible():
            self._models_view.clear_pull_progress()

    @Slot(dict)
    def _apply_pull_event(self, event: dict) -> None:
        # Forward to the open ModelsView so its row updates live.
        if self._models_view is not None and self._models_view.isVisible():
            self._models_view.set_pull_progress(event)

        phase = event.get("phase")
        model = event.get("model") or ""
        if phase == "queue":
            models = event.get("models") or []
            self._set_progress(0, f"Queued {len(models)} model(s)")
            return
        if phase == "start":
            self._set_progress(0, f"Starting {model} ({event.get('index')}/{event.get('total')})")
            return
        if phase == "blocked":
            self._set_progress(0, f"Blocked {model}: {event.get('reason')}")
            return
        if phase == "retry":
            self._set_progress(0, f"Retrying {model}...")
            return
        if phase == "cancelled":
            self._set_progress(0, "Cancelled")
            return
        if phase == "done":
            ok = event.get("success")
            self._set_progress(100 if ok else 0, f"{model}: {'ok' if ok else 'failed'}")
            return

        total = event.get("total")
        completed = event.get("completed")
        status_text = event.get("status") or ""
        if isinstance(total, int) and total > 0 and isinstance(completed, int):
            percent = max(0, min(100, int(completed / total * 100)))
            self._set_progress(percent, f"{model}: {status_text} {percent}%")
        elif status_text:
            self._set_progress(None, f"{model}: {status_text}")

    def _set_progress(self, value, message: str) -> None:
        if value is not None:
            self.progress_bar.setValue(int(value))
        self.progress_label.setText(message)

    # ------------------------------------------------------------------
    # Executions
    # ------------------------------------------------------------------
    def _refresh_execution_list(self, select_latest: bool = False, preferred_id: str | None = None) -> None:
        previous_id = (self.current_execution or {}).get("id")
        executions = list(reversed(local_memory.get_executions(session_id=self.session_id)))
        self.execution_rows = executions
        self.execution_list.blockSignals(True)
        self.execution_list.clear()
        for execution in executions:
            QListWidgetItem(_execution_row_label(execution), self.execution_list)
        self.execution_list.blockSignals(False)

        if not executions:
            self.current_execution = None
            self._render_execution(None)
            return

        target_id = preferred_id or (executions[0].get("id") if select_latest else previous_id)
        if not target_id:
            target_id = executions[0].get("id")
        self._select_execution_by_id(target_id)

    def _select_execution_by_id(self, execution_id: str) -> None:
        if not self.execution_rows:
            return
        index = 0
        for row_index, execution in enumerate(self.execution_rows):
            if execution.get("id") == execution_id:
                index = row_index
                break
        self.execution_list.blockSignals(True)
        self.execution_list.setCurrentRow(index)
        self.execution_list.blockSignals(False)
        self._render_execution(self.execution_rows[index])

    def _on_execution_selected(self) -> None:
        row = self.execution_list.currentRow()
        if row < 0 or row >= len(self.execution_rows):
            return
        self._render_execution(self.execution_rows[row])

    def _render_execution(self, execution: dict | None) -> None:
        self.current_execution = execution
        detail = format_execution_detail(execution) if execution else "No executions recorded for this session yet."
        self.detail_text.setPlainText(detail)

        self.flow_list.clear()
        if not execution:
            return
        graph = build_execution_graph(execution)
        for node in graph.get("nodes", []):
            label = f"[{node.get('kind', '')}] {node.get('title', '')}: {node.get('body', '')}"
            QListWidgetItem(label, self.flow_list)

    # ------------------------------------------------------------------
    # Startup model prompt
    # ------------------------------------------------------------------
    def _maybe_prompt_for_missing_models(self) -> None:
        status = get_status()
        missing_required = status.get("missing", [])
        if not missing_required:
            return
        if not status.get("runtime_available"):
            self._append_transcript(
                "Control",
                "Missing required models detected, but Ollama is not reachable, so downloads are unavailable right now.",
            )
            self._refresh_status_banner()
            return

        model_list = ", ".join(entry["model"] for entry in missing_required)
        reply = QMessageBox.question(
            self,
            "Download missing models?",
            f"Missing required models: {model_list}\n\nDownload them now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            self._append_transcript("Control", f"Skipping model downloads for now: {model_list}")
            return

        self._append_transcript("Control", f"Downloading missing required models: {model_list}")
        self._run_async_install("install-defaults")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Unsubscribe from the bus so the app can exit cleanly even if the
        # bus singleton outlives this window.
        if self._bus_subscription_id is not None:
            try:
                default_bus().unsubscribe(self._bus_subscription_id)
            except Exception:
                pass
            self._bus_subscription_id = None
        super().closeEvent(event)


def run_qt_app() -> int:
    """Construct and run the Qt application. Returns the exit code."""
    app = QApplication.instance() or QApplication([])
    state = themes_module.load_state()
    _, preset = themes_module.get_active_preset(state)
    themes_module.apply_to_app(
        app, preset, typography=themes_module.get_typography(state)
    )

    window = LocalAIQtWindow()
    window.show()
    return app.exec()
