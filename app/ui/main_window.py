import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

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

WINDOW_TITLE = "Local AI Control Layer"
GRAPH_NODE_WIDTH = 200
GRAPH_NODE_HEIGHT = 96
GRAPH_PADDING = 24
GRAPH_H_GAP = 36
GRAPH_V_GAP = 56
GRAPH_DEFAULT_DETAIL = "Select an execution node to inspect it."
DETAIL_ANCHOR_PREFIXES = {
    "input": "- Message:",
    "route": "- Route:",
    "model": "- Model:",
    "workspace": "- Approval:",
    "result": "- Status:",
    "memory": "- Memory:",
}
GRAPH_KIND_COLORS = {
    "input": "#dbeafe",
    "route": "#e9d5ff",
    "model": "#dcfce7",
    "workspace": "#fef3c7",
    "approval": "#fde68a",
    "result": "#fecaca",
    "memory": "#e5e7eb",
}


def _clip_text(text, limit=58):
    cleaned = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _input_prompt(session_id):
    context = local_memory.get_session_context(session_id=session_id)
    workspace_path = context.get("workspace_path")
    if not workspace_path:
        return f"\nYou ({session_id}): "

    workspace_name = os.path.basename(workspace_path.rstrip("\\/")) or workspace_path
    return f"\nYou ({session_id} @ {workspace_name}): "


def _execution_row_label(execution):
    status = str(execution.get("status") or "unknown").replace("_", " ")
    lane = execution.get("resolved_lane") or execution.get("requested_lane") or "unknown"
    preview = _clip_text(execution.get("message_preview") or execution.get("message"), limit=64)
    return f"{execution.get('id', '(no id)')} | {status} | {lane} | {preview}"


def _detail_anchor_prefix(node_id):
    return DETAIL_ANCHOR_PREFIXES.get(node_id)


def _default_graph_positions(nodes, canvas_width):
    positions = {}
    max_cols = max(1, min(3, canvas_width // (GRAPH_NODE_WIDTH + GRAPH_H_GAP)))
    for index, node in enumerate(nodes):
        row = index // max_cols
        col = index % max_cols
        x1 = GRAPH_PADDING + col * (GRAPH_NODE_WIDTH + GRAPH_H_GAP)
        y1 = GRAPH_PADDING + row * (GRAPH_NODE_HEIGHT + GRAPH_V_GAP)
        positions[node["id"]] = (x1, y1)
    return positions


def _run_cli():
    print(startup_summary())
    session_id = "default"

    status = get_status()
    missing_required = status.get("missing", [])
    if missing_required:
        if not status.get("runtime_available"):
            print("Missing required models detected, but Ollama is not reachable, so downloads are unavailable right now.")
        else:
            model_list = ", ".join(entry["model"] for entry in missing_required)
            print(f"Missing required models: {model_list}")
            try:
                answer = input("Download the missing required models now? [Y/n]: ").strip().lower()
            except EOFError:
                print("Skipping downloads because startup input is unavailable.")
                answer = "n"
            except KeyboardInterrupt:
                print("\nSkipping downloads.")
                answer = "n"
            if answer in {"", "y", "yes"}:
                print(format_install_results(ensure_models_installed()))
            else:
                print("Skipping model downloads for now.")

    while True:
        try:
            cmd = input(_input_prompt(session_id))
        except EOFError:
            print("\nShutting down.")
            break
        except KeyboardInterrupt:
            print("\nShutting down.")
            break

        event = dispatch_input(cmd, session_id=session_id)
        session_id = event.get("session_id", session_id)
        if event.get("output"):
            print(event["output"])
        if event.get("action") == "exit":
            break


class LocalAIWindow:
    def __init__(self, root):
        self.root = root
        self.root.title(WINDOW_TITLE)
        self.root.geometry("1440x920")
        self.root.minsize(1120, 720)

        self.session_id = "default"
        self.execution_rows = []
        self.current_execution = None
        self.current_graph = {"nodes": [], "edges": []}
        self.graph_node_items = {}
        self.graph_layouts = {}
        self.selected_graph_node_id = None
        self.detail_anchor_ranges = {}
        self.drag_state = None

        self.status_var = tk.StringVar()
        self.node_detail_var = tk.StringVar(value=GRAPH_DEFAULT_DETAIL)
        self.input_var = tk.StringVar()
        self.progress_var = tk.StringVar(value="")
        self._install_in_flight = False
        self._install_cancel_event = None

        self._build_ui()
        self._append_transcript("Control", startup_summary())
        self._refresh_status_banner()
        self._refresh_execution_list()
        self.root.after(250, self._maybe_prompt_for_missing_models)

    def _build_ui(self):
        container = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        container.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(container, padding=12)
        right = ttk.Frame(container, padding=12)
        container.add(left, weight=3)
        container.add(right, weight=2)

        ttk.Label(left, text=WINDOW_TITLE, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(left, textvariable=self.status_var, wraplength=760).pack(anchor="w", pady=(4, 10))

        transcript_frame = ttk.Frame(left)
        transcript_frame.pack(fill=tk.BOTH, expand=True)
        transcript_scroll = ttk.Scrollbar(transcript_frame)
        transcript_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.transcript = tk.Text(
            transcript_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            yscrollcommand=transcript_scroll.set,
            font=("Consolas", 10),
            background="#f8fafc",
        )
        self.transcript.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        transcript_scroll.config(command=self.transcript.yview)
        self.transcript.tag_configure("speaker", font=("Segoe UI", 10, "bold"))

        quick_actions = ttk.Frame(left)
        quick_actions.pack(fill=tk.X, pady=(10, 4))
        for label, command in (
            ("Status", "status"),
            ("Models", "models"),
            ("Doctor", "doctor"),
            ("Onboard", "onboard"),
            ("Executions", "executions"),
            ("Approve", "approve"),
            ("Deny", "deny"),
        ):
            ttk.Button(quick_actions, text=label, command=lambda value=command: self._dispatch_text(value)).pack(
                side=tk.LEFT, padx=(0, 6)
            )

        install_row = ttk.Frame(left)
        install_row.pack(fill=tk.X, pady=(0, 8))
        for label, command in (
            ("Install Vision", "install vision"),
            ("Install Unfiltered", "install unfiltered"),
            ("Uninstall Vision", "uninstall vision"),
            ("Uninstall Unfiltered", "uninstall unfiltered"),
        ):
            ttk.Button(install_row, text=label, command=lambda value=command: self._dispatch_text(value)).pack(
                side=tk.LEFT, padx=(0, 6)
            )

        progress_row = ttk.Frame(left)
        progress_row.pack(fill=tk.X, pady=(0, 8))
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=100, value=0)
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(progress_row, textvariable=self.progress_var, width=44, anchor="w").pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.cancel_button = ttk.Button(progress_row, text="Cancel", command=self._cancel_install, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))

        input_row = ttk.Frame(left)
        input_row.pack(fill=tk.X)
        entry = ttk.Entry(input_row, textvariable=self.input_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", self._on_submit)
        entry.focus_set()
        ttk.Button(input_row, text="Send", command=self._on_submit).pack(side=tk.LEFT, padx=(8, 0))

        exec_header = ttk.Frame(right)
        exec_header.pack(fill=tk.X)
        ttk.Label(exec_header, text="Executions", font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        ttk.Button(exec_header, text="Refresh", command=self._refresh_execution_list).pack(side=tk.RIGHT)

        exec_frame = ttk.Frame(right)
        exec_frame.pack(fill=tk.BOTH, expand=False, pady=(8, 10))
        exec_scroll = ttk.Scrollbar(exec_frame)
        exec_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.execution_list = tk.Listbox(exec_frame, height=10, yscrollcommand=exec_scroll.set, font=("Consolas", 9))
        self.execution_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.execution_list.bind("<<ListboxSelect>>", self._on_execution_selected)
        exec_scroll.config(command=self.execution_list.yview)

        ttk.Label(right, text="Execution Detail", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        detail_frame = ttk.Frame(right)
        detail_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 10))
        detail_scroll = ttk.Scrollbar(detail_frame)
        detail_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.detail_text = tk.Text(
            detail_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            height=16,
            yscrollcommand=detail_scroll.set,
            font=("Consolas", 9),
            background="#ffffff",
        )
        self.detail_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        detail_scroll.config(command=self.detail_text.yview)
        self.detail_text.tag_configure("section_focus", background="#e0f2fe")

        ttk.Label(right, text="Execution Flow", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.graph_canvas = tk.Canvas(right, background="#f8fafc", highlightthickness=1, highlightbackground="#d0d7de")
        self.graph_canvas.pack(fill=tk.BOTH, expand=True, pady=(6, 8))
        self.graph_canvas.bind("<ButtonPress-1>", self._on_graph_press)
        self.graph_canvas.bind("<B1-Motion>", self._on_graph_drag)
        self.graph_canvas.bind("<ButtonRelease-1>", self._on_graph_release)
        self.graph_canvas.bind("<Configure>", self._on_graph_resize)
        ttk.Label(right, textvariable=self.node_detail_var, wraplength=520, justify=tk.LEFT).pack(anchor="w")

    def _append_transcript(self, speaker, text):
        rendered = str(text or "").strip()
        if not rendered:
            return

        self.transcript.configure(state=tk.NORMAL)
        self.transcript.insert(tk.END, f"{speaker}\n", ("speaker",))
        self.transcript.insert(tk.END, f"{rendered}\n\n")
        self.transcript.configure(state=tk.DISABLED)
        self.transcript.see(tk.END)

    def _refresh_status_banner(self):
        status = get_status()
        context = local_memory.get_session_context(session_id=self.session_id)
        workspace_path = context.get("workspace_path")
        workspace_label = os.path.basename(workspace_path.rstrip("\\/")) if workspace_path else "none"
        missing = ", ".join(entry["model"] for entry in status.get("missing", [])) or "none"
        skills = ", ".join(context.get("active_skills", [])) or "none"
        runtime = "connected" if status.get("runtime_available") else "offline"
        self.status_var.set(
            f"Session: {self.session_id} | Workspace: {workspace_label} | Skills: {skills} | "
            f"Ollama: {runtime} | Missing defaults: {missing}"
        )
        self.root.title(f"{WINDOW_TITLE} - {self.session_id}")

    def _maybe_prompt_for_missing_models(self):
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
        should_download = messagebox.askyesno(
            "Download missing models?",
            f"Missing required models: {model_list}\n\nDownload them now?",
            parent=self.root,
        )
        if not should_download:
            self._append_transcript("Control", f"Skipping model downloads for now: {model_list}")
            return

        self._append_transcript("Control", f"Downloading missing required models: {model_list}")
        self._run_async_install("install-defaults")

    def _run_async_install(self, command):
        """Run install-defaults / install <target> / uninstall <target> off the UI thread."""
        if self._install_in_flight:
            self._append_transcript("Control", "An install is already running; please wait for it to finish.")
            return

        self._install_in_flight = True
        self._install_cancel_event = threading.Event()
        cancel_event = self._install_cancel_event
        self._set_progress(0, "Starting...")
        cancellable = command == "install-defaults" or command.startswith("install ")
        self.cancel_button.configure(state=tk.NORMAL if cancellable else tk.DISABLED)

        def worker():
            try:
                if command == "install-defaults":
                    results = ensure_models_installed(
                        progress_callback=self._on_pull_event, cancel_event=cancel_event
                    )
                    output = format_install_results(results)
                elif command.startswith("install "):
                    target = command[len("install "):].strip()
                    result = install_single_model(
                        target, progress_callback=self._on_pull_event, cancel_event=cancel_event
                    )
                    output = format_single_install_result(result)
                elif command.startswith("uninstall "):
                    target = command[len("uninstall "):].strip()
                    result = uninstall_single_model(target)
                    output = format_uninstall_result(result)
                else:
                    output = f"Unknown install command: {command}"
            except Exception as exc:
                output = f"Install error: {exc}"
            self.root.after(0, lambda: self._finish_async_install(output))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_install(self):
        if not self._install_in_flight:
            return
        if self._install_cancel_event is None:
            return
        self._install_cancel_event.set()
        self.cancel_button.configure(state=tk.DISABLED)
        self._set_progress(None, "Cancelling...")

    def _finish_async_install(self, output):
        self._install_in_flight = False
        self._install_cancel_event = None
        self._set_progress(0, "")
        self.cancel_button.configure(state=tk.DISABLED)
        self._append_transcript("Control", output)
        self._refresh_status_banner()

    def _on_pull_event(self, event):
        # Invoked from a worker thread; marshal to the UI thread.
        self.root.after(0, lambda e=event: self._apply_pull_event(e))

    def _apply_pull_event(self, event):
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
        if phase == "done":
            state = "ok" if event.get("success") else "failed"
            self._set_progress(100 if event.get("success") else 0, f"{model}: {state}")
            return

        total = event.get("total")
        completed = event.get("completed")
        status_text = event.get("status") or ""
        if isinstance(total, int) and total > 0 and isinstance(completed, int):
            percent = max(0, min(100, int(completed / total * 100)))
            self._set_progress(percent, f"{model}: {status_text} {percent}%")
        elif status_text:
            self._set_progress(None, f"{model}: {status_text}")

    def _set_progress(self, value, message):
        if value is not None:
            try:
                self.progress_bar.configure(mode="determinate")
                self.progress_bar["value"] = value
            except tk.TclError:
                pass
        self.progress_var.set(message)

    def _on_submit(self, *_args):
        text = self.input_var.get().strip()
        if not text:
            return
        self.input_var.set("")
        self._dispatch_text(text)

    def _dispatch_text(self, text):
        self._append_transcript(_input_prompt(self.session_id).strip(": "), text)

        stripped = text.strip()
        lowered = stripped.lower().lstrip("/")
        if lowered == "install-defaults" or lowered.startswith("install ") or lowered.startswith("uninstall "):
            self._run_async_install(lowered)
            return

        event = dispatch_input(text, session_id=self.session_id)
        self.session_id = event.get("session_id", self.session_id)

        if event.get("output"):
            self._append_transcript("Control", event["output"])

        self._refresh_status_banner()

        preferred_id = None
        if event.get("action") == "execution" and isinstance(event.get("result"), dict):
            preferred_id = event["result"].get("id")
        self._refresh_execution_list(select_latest=event.get("action") == "chat", preferred_id=preferred_id)

        if event.get("action") == "exit":
            self.root.after(50, self.root.destroy)

    def _refresh_execution_list(self, select_latest=False, preferred_id=None):
        previous_id = None
        current = self.current_execution or {}
        if current.get("id"):
            previous_id = current["id"]

        executions = list(reversed(local_memory.get_executions(session_id=self.session_id)))
        self.execution_rows = executions
        self.execution_list.delete(0, tk.END)
        for execution in executions:
            self.execution_list.insert(tk.END, _execution_row_label(execution))

        if not executions:
            self.current_execution = None
            self._render_execution(None)
            return

        target_id = preferred_id or (executions[0].get("id") if select_latest else previous_id)
        if not target_id:
            target_id = executions[0].get("id")
        self._select_execution_by_id(target_id)

    def _select_execution_by_id(self, execution_id):
        if not self.execution_rows:
            return

        index = 0
        for row_index, execution in enumerate(self.execution_rows):
            if execution.get("id") == execution_id:
                index = row_index
                break

        self.execution_list.selection_clear(0, tk.END)
        self.execution_list.selection_set(index)
        self.execution_list.activate(index)
        self.execution_list.see(index)
        self._render_execution(self.execution_rows[index])

    def _on_execution_selected(self, _event=None):
        selection = self.execution_list.curselection()
        if not selection:
            return
        self._render_execution(self.execution_rows[selection[0]])

    def _render_execution(self, execution):
        self.current_execution = execution
        detail = format_execution_detail(execution) if execution else "No executions recorded for this session yet."
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, detail)
        self._rebuild_detail_anchors()
        self.detail_text.configure(state=tk.DISABLED)

        self.current_graph = build_execution_graph(execution)
        self._draw_execution_graph()

    def _graph_layout_store(self):
        execution_id = (self.current_execution or {}).get("id")
        if not execution_id:
            return None
        return self.graph_layouts.setdefault(execution_id, {})

    def _rebuild_detail_anchors(self):
        self.detail_anchor_ranges = {}
        self.detail_text.tag_remove("section_focus", "1.0", tk.END)
        for node_id in DETAIL_ANCHOR_PREFIXES:
            prefix = _detail_anchor_prefix(node_id)
            if not prefix:
                continue
            index = self.detail_text.search(prefix, "1.0", stopindex=tk.END)
            if index:
                self.detail_anchor_ranges[node_id] = (index, f"{index} lineend")

    def _focus_detail_section(self, node_id):
        anchor = self.detail_anchor_ranges.get(node_id)
        if not anchor:
            return
        start, end = anchor
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.tag_remove("section_focus", "1.0", tk.END)
        self.detail_text.tag_add("section_focus", start, end)
        self.detail_text.see(start)
        self.detail_text.configure(state=tk.DISABLED)

    def _draw_execution_graph(self, selected_node_id=None, focus_detail=False):
        self.graph_canvas.delete("all")
        self.graph_node_items = {}
        self.selected_graph_node_id = None

        nodes = self.current_graph.get("nodes", [])
        if not nodes:
            self.node_detail_var.set(GRAPH_DEFAULT_DETAIL)
            return

        canvas_width = max(self.graph_canvas.winfo_width(), 560)
        positions = {}
        layout_store = self._graph_layout_store()
        default_positions = _default_graph_positions(nodes, canvas_width)
        for node in nodes:
            top_left = default_positions[node["id"]]
            if layout_store is not None and node["id"] in layout_store:
                top_left = layout_store[node["id"]]
            x1, y1 = top_left
            x2 = x1 + GRAPH_NODE_WIDTH
            y2 = y1 + GRAPH_NODE_HEIGHT
            positions[node["id"]] = (x1, y1, x2, y2)

        for start_id, end_id in self.current_graph.get("edges", []):
            if start_id not in positions or end_id not in positions:
                continue
            x1, y1, x2, y2 = positions[start_id]
            nx1, ny1, nx2, ny2 = positions[end_id]
            self.graph_canvas.create_line(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                (nx1 + nx2) / 2,
                (ny1 + ny2) / 2,
                arrow=tk.LAST,
                fill="#64748b",
                width=2,
                smooth=True,
            )

        for node in nodes:
            x1, y1, x2, y2 = positions[node["id"]]
            tags = ("node", f"node:{node['id']}")
            color = GRAPH_KIND_COLORS.get(node.get("kind"), "#e5e7eb")
            rectangle = self.graph_canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=color,
                outline="#94a3b8",
                width=2,
                tags=tags,
            )
            title = self.graph_canvas.create_text(
                x1 + 10,
                y1 + 10,
                anchor="nw",
                text=node.get("title", ""),
                font=("Segoe UI", 10, "bold"),
                width=GRAPH_NODE_WIDTH - 20,
                tags=tags,
            )
            body = self.graph_canvas.create_text(
                x1 + 10,
                y1 + 34,
                anchor="nw",
                text=node.get("body", ""),
                font=("Segoe UI", 9),
                width=GRAPH_NODE_WIDTH - 20,
                tags=tags,
            )
            self.graph_node_items[node["id"]] = {
                "node": node,
                "rect": rectangle,
                "bounds": (x1, y1, x2, y2),
                "items": [rectangle, title, body],
            }

        max_x = max(bounds[2] for bounds in positions.values()) + GRAPH_PADDING
        max_y = max(bounds[3] for bounds in positions.values()) + GRAPH_PADDING
        self.graph_canvas.configure(scrollregion=(0, 0, max_x, max_y))

        self._select_graph_node(selected_node_id or nodes[0]["id"], focus_detail=focus_detail)

    def _node_id_from_canvas_event(self):
        current = self.graph_canvas.find_withtag("current")
        if not current:
            return None
        for tag in self.graph_canvas.gettags(current[0]):
            if tag.startswith("node:"):
                return tag.split(":", 1)[1]
        return None

    def _on_graph_press(self, event):
        node_id = self._node_id_from_canvas_event()
        if not node_id:
            return

        self._select_graph_node(node_id, focus_detail=True)
        bounds = self.graph_node_items.get(node_id, {}).get("bounds")
        if not bounds:
            return
        x1, y1, _x2, _y2 = bounds
        self.drag_state = {
            "node_id": node_id,
            "offset_x": event.x - x1,
            "offset_y": event.y - y1,
        }

    def _on_graph_drag(self, event):
        if not self.drag_state:
            return

        layout_store = self._graph_layout_store()
        if layout_store is None:
            return

        node_id = self.drag_state["node_id"]
        new_x = max(12, event.x - self.drag_state["offset_x"])
        new_y = max(12, event.y - self.drag_state["offset_y"])
        layout_store[node_id] = (new_x, new_y)
        self._draw_execution_graph(selected_node_id=node_id, focus_detail=False)

    def _on_graph_release(self, _event):
        self.drag_state = None

    def _on_graph_resize(self, _event):
        if self.current_execution:
            self._draw_execution_graph(selected_node_id=self.selected_graph_node_id, focus_detail=False)

    def _select_graph_node(self, node_id, focus_detail=True):
        for item_id, item in self.graph_node_items.items():
            self.graph_canvas.itemconfigure(item["rect"], outline="#94a3b8", width=2)

        selected = self.graph_node_items.get(node_id)
        if not selected:
            self.node_detail_var.set(GRAPH_DEFAULT_DETAIL)
            return

        self.selected_graph_node_id = node_id
        self.graph_canvas.itemconfigure(selected["rect"], outline="#0f172a", width=3)
        node = selected["node"]
        self.node_detail_var.set(f"{node.get('title', '')}: {node.get('body', '')}\nDrag to rearrange this node.")
        if focus_detail:
            self._focus_detail_section(node_id)


def main():
    try:
        root = tk.Tk()
    except tk.TclError:
        _run_cli()
        return

    app = LocalAIWindow(root)
    try:
        root.mainloop()
    finally:
        del app


if __name__ == "__main__":
    main()
