"""MCP tool registry.

Each MCP tool is a thin wrapper around a dispatcher command. The tool
exposes a JSON Schema for typed args; the handler builds the
equivalent dispatcher input and invokes ``dispatch_input``. The
dispatcher's event dict becomes the tool's response.

Approval-gated commands return their normal pending-approval payload —
the MCP client sees ``isError: True`` with ``data.reason =
"approval_required"`` and a ``pending_action`` block; it follows up
with the ``approve`` tool when the user OKs it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


@dataclass
class Tool:
    """A single MCP tool definition + handler."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]

    def descriptor(self) -> Dict[str, Any]:
        """The dict returned to ``tools/list`` consumers."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# ============================================================
# Helpers — turn a dispatcher event into an MCP `tools/call` result
# ============================================================

def event_to_call_result(event: Dict[str, Any]) -> Dict[str, Any]:
    """Render a dispatcher event as an MCP ``tools/call`` result.

    MCP's tool-call response shape is ``{content: [...], isError: bool,
    structuredContent: {...}}``. We always populate ``structuredContent``
    with the full event dict (so MCP clients that grok structured
    output get the rich data) and ``content`` with a one-block text
    summary (so clients that only render text still see something
    useful).
    """
    text = event.get("output") or ""
    reason = event.get("reason")
    is_error = bool(reason)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "structuredContent": event,
    }


def _session_arg(args: Dict[str, Any], default: str = "default") -> str:
    sid = args.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return default


# ============================================================
# Tool handlers — each takes args dict, returns event dict
# ============================================================

def _dispatch_input():
    """Lazy import to avoid a circular at module-load time."""
    from app.services.command_dispatcher import dispatch_input
    return dispatch_input


# --- Inspect tools --------------------------------------------

def _status_handler(_args):
    return _dispatch_input()("status", session_id="default")


def _models_handler(_args):
    return _dispatch_input()("models", session_id="default")


def _doctor_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("doctor", session_id=sid)


def _executions_handler(args):
    sid = _session_arg(args)
    parts = []
    if "query" in args and args["query"]:
        parts.append(f"executions search {args['query']}")
    else:
        parts.append("executions")
    if "lane" in args and args["lane"]:
        parts.append(f"lane={args['lane']}")
    if "status" in args and args["status"]:
        parts.append(f"status={args['status']}")
    if "since" in args and args["since"]:
        parts.append(f"since={args['since']}")
    if "limit" in args and args["limit"]:
        parts.append(f"limit={args['limit']}")
    cmd = " ".join(parts)
    return _dispatch_input()(cmd, session_id=sid)


def _tasks_handler(args):
    sid = _session_arg(args)
    cmd = "tasks"
    if "status" in args and args["status"]:
        cmd = f"tasks {args['status']}"
    return _dispatch_input()(cmd, session_id=sid)


def _link_status_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("link status", session_id=sid)


def _get_session_state_handler(args):
    """Return the session-state snapshot the HTTP gateway also produces."""
    from app.memory import local_memory
    sid = _session_arg(args)
    context = local_memory.get_session_context(session_id=sid)
    summary = local_memory.get_session_summary(session_id=sid)
    history = local_memory.get_history(session_id=sid, limit=10)
    return {
        "action": "get_session_state",
        "session_id": sid,
        "output": (
            f"session={sid} "
            f"workspace={context.get('workspace_path') or '(none)'} "
            f"mode={context.get('mode')} "
            f"history={len(history)}"
        ),
        "result": {
            "context": context,
            "summary": summary,
            "history_preview": history,
        },
    }


def _get_prompt_assembly_handler(args):
    """Return the layered-prompt slot trace for a session's *next*
    chat turn (without dispatching the turn). Phase 4 of
    ``docs/design/prompt-assembly.md``.

    Lets external agents (or another localaiproject session)
    inspect *exactly* what slots / budgets / truncations the model
    would see. Optional ``message`` arg simulates how that user
    text would change the assembly (mostly via the trailing
    user-message slot, but also via mention expansion when the
    workspace is pinned)."""
    from app.memory import local_memory
    from app.services import chat_service, prompt_assembly as pa
    from app.services.mentions_service import expand_mentions

    sid = _session_arg(args)
    message = str(args.get("message") or "")

    context = local_memory.get_session_context(session_id=sid)
    summary = local_memory.get_session_summary(session_id=sid) or {}
    history = local_memory.get_history(session_id=sid, limit=0)

    # Mirror chat_service: when a workspace is pinned, expand
    # ``@file`` mentions so the trace reflects the same slot a real
    # turn would produce.
    mention_expansion = expand_mentions(message, context.get("workspace_path"))
    runtime_message = (
        mention_expansion["stripped"] if mention_expansion["expanded"] else message
    )

    messages, trace = chat_service.build_runtime_assembly(
        summary,
        history,
        runtime_message,
        session_context=context,
        approved_workspace_result=None,
        mentions_context_block=mention_expansion["context_block"],
        session_id=sid,
    )

    # Strip the synthetic _diagnostics entry from the visible
    # trace; surface its ``over_budget`` flag separately.
    visible_trace = [t for t in trace if t.get("name") != "_diagnostics"]
    over_budget = pa.trace_over_budget(trace)
    total_budget = (
        local_memory.get_prompt_total_budget(session_id=sid)
        or pa.DEFAULT_TOTAL_BUDGET
    )
    rendered_chars = pa.live_total_chars(visible_trace)

    summary_line = (
        f"session={sid} slots={len(visible_trace)} "
        f"chars={rendered_chars}/{total_budget}"
    )
    if over_budget:
        summary_line += " [OVER_BUDGET]"

    return {
        "action": "get_prompt_assembly",
        "session_id": sid,
        "output": summary_line,
        "result": {
            "trace": visible_trace,
            "total_budget": total_budget,
            "rendered_chars": rendered_chars,
            "over_budget": over_budget,
            "message_count": len(messages),
            "footer": pa.render_trace_footer(trace),
        },
    }


# --- Action tools (each routed through dispatch_input) --------

def _chat_handler(args):
    sid = _session_arg(args)
    text = args.get("text") or ""
    if not text:
        return {
            "action": "chat",
            "session_id": sid,
            "output": "text is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(text, session_id=sid)


def _dispatch_handler(args):
    sid = _session_arg(args)
    text = args.get("text") or ""
    builder = args.get("builder")
    if builder:
        cmd = f"dispatch @{builder} {text}"
    else:
        cmd = f"dispatch {text}"
    return _dispatch_input()(cmd, session_id=sid)


def _do_handler(args):
    sid = _session_arg(args)
    text = args.get("text") or ""
    builder = args.get("builder")
    if builder:
        cmd = f"do @{builder} {text}"
    else:
        cmd = f"do {text}"
    return _dispatch_input()(cmd, session_id=sid)


def _iterate_handler(args):
    sid = _session_arg(args)
    text = args.get("text") or ""
    builder = args.get("builder")
    parts = ["iterate"]
    if builder:
        parts.append(f"@{builder}")
    parts.append(text)
    if "max_iterations" in args and args["max_iterations"]:
        parts.append(f"--max {args['max_iterations']}")
    return _dispatch_input()(" ".join(parts), session_id=sid)


def _inbox_next_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("inbox next", session_id=sid)


def _link_handler(args):
    planner = args.get("planner_session") or _session_arg(args)
    builder = args.get("builder_session")
    if not builder:
        return {
            "action": "link",
            "session_id": planner,
            "output": "builder_session is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"link {builder}", session_id=planner)


def _set_workspace_handler(args):
    sid = _session_arg(args)
    path = args.get("path")
    if not path:
        return {
            "action": "workspace",
            "session_id": sid,
            "output": "path is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"workspace {path}", session_id=sid)


def _set_mode_handler(args):
    sid = _session_arg(args)
    mode = args.get("mode")
    if mode not in {"plan", "build"}:
        return {
            "action": "mode",
            "session_id": sid,
            "output": "mode must be 'plan' or 'build'",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"mode {mode}", session_id=sid)


def _approve_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("approve", session_id=sid)


def _deny_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("deny", session_id=sid)


def _read_file_handler(args):
    sid = _session_arg(args)
    path = args.get("path")
    if not path:
        return {
            "action": "read",
            "session_id": sid,
            "output": "path is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"read {path}", session_id=sid)


def _search_workspace_handler(args):
    sid = _session_arg(args)
    query = args.get("query")
    if not query:
        return {
            "action": "search",
            "session_id": sid,
            "output": "query is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"search {query}", session_id=sid)


def _notes_show_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("notes", session_id=sid)


def _notes_append_handler(args):
    sid = _session_arg(args)
    text = args.get("text") or ""
    if not text:
        return {
            "action": "notes",
            "session_id": sid,
            "output": "text is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"notes append {text}", session_id=sid)


# --- Custom instructions (custom-instructions.md Phase 1+2+2.5+3a) --
#
# Each handler dispatches into the corresponding ``instructions ...``
# subcommand. The dispatcher already enforces validation, so the MCP
# wrapper just shapes args into the dispatcher's positional + flag
# string format.

def _instructions_list_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("instructions list", session_id=sid)


def _build_applies_to_flags(args):
    """Translate MCP-shaped applies_to into dispatcher-flag form.
    Returns a list of additional argv tokens (already escaped).
    Empty list when no applies_to fields supplied.
    """
    parts = []
    mode = args.get("mode")
    if isinstance(mode, str) and mode:
        parts.extend(["--mode", mode])
    workspace = args.get("workspace")
    if isinstance(workspace, str) and workspace:
        parts.extend(["--workspace", workspace])
    tags = args.get("tags")
    if isinstance(tags, list) and tags:
        parts.extend(["--tags", ",".join(str(t) for t in tags)])
    elif isinstance(tags, str) and tags:
        parts.extend(["--tags", tags])
    lanes = args.get("lanes")
    if isinstance(lanes, list) and lanes:
        parts.extend(["--lanes", ",".join(str(l) for l in lanes)])
    elif isinstance(lanes, str) and lanes:
        parts.extend(["--lanes", lanes])
    return parts


def _instructions_add_handler(args):
    sid = _session_arg(args)
    kind = args.get("kind")
    body = args.get("body")
    if not isinstance(kind, str) or not kind.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "kind is required (constraint|style|persona|knowledge|override)",
            "reason": "invalid_argument",
        }
    if not isinstance(body, str) or not body.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "body is required",
            "reason": "invalid_argument",
        }
    parts = ["instructions", "add", kind.strip().lower()]
    label = args.get("label")
    if isinstance(label, str) and label.strip():
        parts.extend(["--label", label.strip().lower()])
    explicit_id = args.get("id")
    if isinstance(explicit_id, str) and explicit_id.strip():
        parts.extend(["--id", explicit_id.strip().lower()])
    priority = args.get("priority")
    if isinstance(priority, int):
        parts.extend(["--priority", str(priority)])
    char_limit = args.get("char_limit")
    if isinstance(char_limit, int):
        parts.extend(["--char-limit", str(char_limit)])
    parts.extend(_build_applies_to_flags(args))
    parts.append(body.strip())
    return _dispatch_input()(" ".join(parts), session_id=sid)


def _instructions_show_handler(args):
    sid = _session_arg(args)
    iid = args.get("id")
    if not isinstance(iid, str) or not iid.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "id is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"instructions show {iid.strip()}", session_id=sid)


def _instructions_enable_handler(args):
    sid = _session_arg(args)
    iid = args.get("id")
    if not isinstance(iid, str) or not iid.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "id is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"instructions enable {iid.strip()}", session_id=sid)


def _instructions_disable_handler(args):
    sid = _session_arg(args)
    iid = args.get("id")
    if not isinstance(iid, str) or not iid.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "id is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"instructions disable {iid.strip()}", session_id=sid)


def _instructions_hush_handler(args):
    sid = _session_arg(args)
    iid = args.get("id")
    if not isinstance(iid, str) or not iid.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "id is required",
            "reason": "invalid_argument",
        }
    parts = ["instructions", "hush", iid.strip()]
    turns = args.get("turns")
    if isinstance(turns, int):
        parts.extend(["--turns", str(turns)])
    return _dispatch_input()(" ".join(parts), session_id=sid)


def _instructions_remove_handler(args):
    sid = _session_arg(args)
    iid = args.get("id")
    if not isinstance(iid, str) or not iid.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "id is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"instructions remove {iid.strip()}", session_id=sid)


def _instructions_clear_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("instructions clear", session_id=sid)


def _instructions_edit_handler(args):
    sid = _session_arg(args)
    iid = args.get("id")
    if not isinstance(iid, str) or not iid.strip():
        return {
            "action": "instructions", "session_id": sid,
            "output": "id is required",
            "reason": "invalid_argument",
        }
    parts = ["instructions", "edit", iid.strip()]
    body = args.get("body")
    if isinstance(body, str) and body.strip():
        # --body consumes everything to the next flag — emit it last
        # to avoid the dispatcher's parser swallowing subsequent flags
        # into the body.
        body_tokens = ["--body", body.strip()]
    else:
        body_tokens = []
    label = args.get("label")
    if isinstance(label, str) and label.strip():
        parts.extend(["--label", label.strip().lower()])
    kind = args.get("kind")
    if isinstance(kind, str) and kind.strip():
        parts.extend(["--kind", kind.strip().lower()])
    priority = args.get("priority")
    if isinstance(priority, int):
        parts.extend(["--priority", str(priority)])
    char_limit = args.get("char_limit")
    if isinstance(char_limit, int):
        parts.extend(["--char-limit", str(char_limit)])
    parts.extend(_build_applies_to_flags(args))
    # Booleans for clearing applies_to keys.
    if args.get("clear_mode") is True:
        parts.append("--no-mode")
    if args.get("clear_workspace") is True:
        parts.append("--no-workspace")
    if args.get("clear_tags") is True:
        parts.append("--no-tags")
    if args.get("clear_lanes") is True:
        parts.append("--no-lanes")
    parts.extend(body_tokens)
    if len(parts) == 3:
        return {
            "action": "instructions", "session_id": sid,
            "output": "no changes specified",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(" ".join(parts), session_id=sid)


def _instructions_import_persona_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("instructions import-persona", session_id=sid)


def _cowork_plan_handler(args):
    sid = _session_arg(args)
    goal = args.get("goal") or ""
    if not goal:
        return {
            "action": "cowork",
            "session_id": sid,
            "output": "goal is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f'cowork plan "{goal}"', session_id=sid)


def _cowork_run_handler(args):
    sid = _session_arg(args)
    run_id = args.get("run_id")
    if not run_id:
        return {
            "action": "cowork",
            "session_id": sid,
            "output": "run_id is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"cowork run {run_id}", session_id=sid)


# --- Browser tools (Phase 1: read-only) -----------------------

def _browser_open_handler(args):
    sid = _session_arg(args)
    url = args.get("url")
    if not url:
        return {
            "action": "browser",
            "session_id": sid,
            "output": "url is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"browser open {url}", session_id=sid)


def _browser_navigate_handler(args):
    sid = _session_arg(args)
    url = args.get("url")
    if not url:
        return {
            "action": "browser",
            "session_id": sid,
            "output": "url is required",
            "reason": "invalid_argument",
        }
    return _dispatch_input()(f"browser navigate {url}", session_id=sid)


def _browser_screenshot_handler(args):
    sid = _session_arg(args)
    selector = args.get("selector")
    cmd = "browser screenshot" + (f" {selector}" if selector else "")
    return _dispatch_input()(cmd, session_id=sid)


def _browser_get_text_handler(args):
    sid = _session_arg(args)
    selector = args.get("selector")
    cmd = "browser get-text" + (f" {selector}" if selector else "")
    return _dispatch_input()(cmd, session_id=sid)


def _browser_get_html_handler(args):
    sid = _session_arg(args)
    selector = args.get("selector")
    cmd = "browser get-html" + (f" {selector}" if selector else "")
    return _dispatch_input()(cmd, session_id=sid)


def _browser_close_handler(args):
    sid = _session_arg(args)
    return _dispatch_input()("browser close", session_id=sid)


def _recall_handler(args):
    sid = _session_arg(args)
    query = (args.get("query") or "").strip()
    if not query:
        return {
            "action": "recall",
            "session_id": sid,
            "output": "query is required",
            "reason": "invalid_argument",
        }
    parts = ["recall", query]
    if args.get("since"):
        parts.extend(["--since", str(args["since"])])
    if args.get("session"):
        parts.extend(["--session", str(args["session"])])
    if args.get("limit"):
        parts.extend(["--limit", str(args["limit"])])
    return _dispatch_input()(" ".join(parts), session_id=sid)


# ============================================================
# Tool definitions — exported as the registry
# ============================================================

#: The full set of tools exposed by the MCP server. Each entry is a
#: ``Tool`` instance with name, description, input schema, and handler.
TOOLS: List[Tool] = [
    # -- Inspect (read-only) --
    Tool(
        name="status",
        description=(
            "Inspect the runtime: which lanes are installed, hardware fit, "
            "Ollama version. Read-only."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_status_handler,
    ),
    Tool(
        name="models",
        description="Show lane / model state with hardware fit in one view. Read-only.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_models_handler,
    ),
    Tool(
        name="doctor",
        description=(
            "Full control-plane snapshot for a session: workspace, mode, links, "
            "skills, pending approvals, recent executions. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_doctor_handler,
    ),
    Tool(
        name="executions",
        description=(
            "List recent dispatcher executions for a session. Optional filters: "
            "query (text), lane, status, since (e.g. '24h'), limit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "query": {"type": "string"},
                "lane": {"type": "string"},
                "status": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        handler=_executions_handler,
    ),
    Tool(
        name="tasks",
        description="List coordination tasks for a session. Optional status filter.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "status": {"type": "string"},
            },
        },
        handler=_tasks_handler,
    ),
    Tool(
        name="link_status",
        description="Show the planner→builder linking topology for a session.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_link_status_handler,
    ),
    Tool(
        name="get_session_state",
        description=(
            "Return a session's state snapshot: pinned workspace, mode, "
            "compact summary, last 10 history entries. Same shape as the "
            "HTTP gateway's /sessions/<id>/state endpoint."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_get_session_state_handler,
    ),
    Tool(
        name="get_prompt_assembly",
        description=(
            "Inspect what the layered prompt assembly would produce for "
            "a session's NEXT chat turn — without dispatching it. Returns "
            "the per-slot trace (name, chars, char_budget, priority, "
            "truncated, evicted), the active total_budget, the rendered "
            "byte count, and a one-line footer. Use this to debug 'why "
            "is my prompt so big' or 'which slot got evicted'. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "message": {
                    "type": "string",
                    "description": (
                        "Simulate the assembly for this user message. "
                        "Mostly affects mention expansion when a workspace "
                        "is pinned. Default empty."
                    ),
                },
            },
        },
        handler=_get_prompt_assembly_handler,
    ),
    # -- Action --
    Tool(
        name="chat",
        description=(
            "Send a chat turn to a session. The dispatcher's router picks "
            "the lane; the response is the assistant's reply."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "text": {"type": "string", "description": "the message to send"},
            },
            "required": ["text"],
        },
        handler=_chat_handler,
    ),
    Tool(
        name="dispatch",
        description=(
            "Queue a task to one or more linked builders. Returns immediately. "
            "Use `do` if you want to wait for the result."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "text": {"type": "string"},
                "builder": {"type": "string", "description": "Optional explicit builder; omit to fan out"},
            },
            "required": ["text"],
        },
        handler=_dispatch_handler,
    ),
    Tool(
        name="do",
        description=(
            "Dispatch a task and BLOCK until each linked builder's task is "
            "terminal. Use this when you need the result before proceeding."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "text": {"type": "string"},
                "builder": {"type": "string"},
            },
            "required": ["text"],
        },
        handler=_do_handler,
    ),
    Tool(
        name="iterate",
        description=(
            "Re-dispatch a task on the same builder until it emits "
            "`<task-complete>` or `max_iterations` is reached. Use for "
            "multi-turn 'until done' loops."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "text": {"type": "string"},
                "builder": {"type": "string"},
                "max_iterations": {"type": "integer", "default": 5},
            },
            "required": ["text"],
        },
        handler=_iterate_handler,
    ),
    Tool(
        name="inbox_next",
        description="Pop the next queued task from a builder's inbox and run it.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_inbox_next_handler,
    ),
    Tool(
        name="link",
        description="Link a builder to a planner so dispatches fan out to it.",
        input_schema={
            "type": "object",
            "properties": {
                "planner_session": {"type": "string", "default": "default"},
                "builder_session": {"type": "string"},
            },
            "required": ["builder_session"],
        },
        handler=_link_handler,
    ),
    Tool(
        name="set_workspace",
        description="Pin a workspace directory to a session.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
        handler=_set_workspace_handler,
    ),
    Tool(
        name="set_mode",
        description="Switch a session between Plan (read-only context) and Build (full edit) modes.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "mode": {"type": "string", "enum": ["plan", "build"]},
            },
            "required": ["mode"],
        },
        handler=_set_mode_handler,
    ),
    Tool(
        name="approve",
        description=(
            "Apply the pending approval for a session. Workspace reads/writes/"
            "searches and cowork plan approvals all flow through this."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_approve_handler,
    ),
    Tool(
        name="deny",
        description="Cancel the pending approval for a session.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_deny_handler,
    ),
    Tool(
        name="read_file",
        description=(
            "Request approval to read a file inside the pinned workspace. "
            "Returns isError=True with reason=approval_required; follow up "
            "with `approve`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
        handler=_read_file_handler,
    ),
    Tool(
        name="search_workspace",
        description=(
            "Request approval to grep the pinned workspace. Returns "
            "isError=True with reason=approval_required; follow up with "
            "`approve`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
        handler=_search_workspace_handler,
    ),
    Tool(
        name="notes_show",
        description="Show the per-session notes scratchpad.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_notes_show_handler,
    ),
    Tool(
        name="notes_append",
        description="Append text to the per-session notes scratchpad.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
        handler=_notes_append_handler,
    ),
    Tool(
        name="cowork_plan",
        description=(
            "Plan a multi-step cowork run. Produces a JSON plan and queues "
            "an approval gate; follow up with `approve` then `cowork_run`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "goal": {"type": "string"},
            },
            "required": ["goal"],
        },
        handler=_cowork_plan_handler,
    ),
    Tool(
        name="cowork_run",
        description="Execute an approved cowork plan by id.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
        handler=_cowork_run_handler,
    ),
    # -- Custom instructions (custom-instructions.md Phase 1+2+2.5+3a) --
    Tool(
        name="instructions_list",
        description=(
            "List the session's typed/conditional behavioural rules. "
            "Each entry has id, kind, label, enabled, applies_to "
            "(mode/workspace/tags/lanes), priority, hushed_remaining."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_instructions_list_handler,
    ),
    Tool(
        name="instructions_add",
        description=(
            "Add a new custom instruction. Required: kind "
            "(constraint|style|persona|knowledge|override), body. "
            "Optional applies_to keys (mode, workspace, tags, lanes) "
            "filter when the rule fires."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "kind": {
                    "type": "string",
                    "enum": ["constraint", "style", "persona", "knowledge", "override"],
                },
                "body": {"type": "string", "description": "Rule text the model will see."},
                "label": {"type": "string", "description": "Optional display label."},
                "id": {"type": "string", "description": "Optional explicit id (else auto)."},
                "mode": {"type": "string", "enum": ["plan", "build"]},
                "workspace": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "lanes": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                "char_limit": {"type": "integer"},
            },
            "required": ["kind", "body"],
        },
        handler=_instructions_add_handler,
    ),
    Tool(
        name="instructions_show",
        description="Show one instruction's full body, condition, and budgets.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "id": {"type": "string"},
            },
            "required": ["id"],
        },
        handler=_instructions_show_handler,
    ),
    Tool(
        name="instructions_enable",
        description="Re-arm a disabled instruction.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "id": {"type": "string"},
            },
            "required": ["id"],
        },
        handler=_instructions_enable_handler,
    ),
    Tool(
        name="instructions_disable",
        description="Mute an instruction without losing its body.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "id": {"type": "string"},
            },
            "required": ["id"],
        },
        handler=_instructions_disable_handler,
    ),
    Tool(
        name="instructions_hush",
        description=(
            "Temporarily mute an instruction for N effective chat turns. "
            "Default 1; turns=0 clears any active hush."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "id": {"type": "string"},
                "turns": {"type": "integer", "minimum": 0, "default": 1},
            },
            "required": ["id"],
        },
        handler=_instructions_hush_handler,
    ),
    Tool(
        name="instructions_remove",
        description="Drop an instruction entirely.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "id": {"type": "string"},
            },
            "required": ["id"],
        },
        handler=_instructions_remove_handler,
    ),
    Tool(
        name="instructions_clear",
        description="Drop every instruction. Returns the count removed.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_instructions_clear_handler,
    ),
    Tool(
        name="instructions_edit",
        description=(
            "Edit an existing instruction in place. Preserves the id; only "
            "fields you supply change. applies_to keys deep-merge against "
            "the existing entry. Use clear_mode/clear_workspace/clear_tags/"
            "clear_lanes booleans to drop a single applies_to key."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "id": {"type": "string"},
                "body": {"type": "string"},
                "label": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["constraint", "style", "persona", "knowledge", "override"],
                },
                "mode": {"type": "string", "enum": ["plan", "build"]},
                "workspace": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "lanes": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                "char_limit": {"type": "integer"},
                "clear_mode": {"type": "boolean"},
                "clear_workspace": {"type": "boolean"},
                "clear_tags": {"type": "boolean"},
                "clear_lanes": {"type": "boolean"},
            },
            "required": ["id"],
        },
        handler=_instructions_edit_handler,
    ),
    Tool(
        name="instructions_import_persona",
        description=(
            "Bulk-create one instruction per heading in the workspace's "
            "PERSONA.md. Heading text becomes the label; a heuristic maps "
            "the heading to one of the five kinds."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_instructions_import_persona_handler,
    ),
    # -- Browser (Phase 1: read-only Playwright skill) --
    Tool(
        name="browser_open",
        description=(
            "Open a URL in the session's browser context. The first hit "
            "to a new origin returns isError=True with "
            "reason=approval_required; follow up with `approve`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "url": {"type": "string", "description": "Absolute URL to load"},
            },
            "required": ["url"],
        },
        handler=_browser_open_handler,
    ),
    Tool(
        name="browser_navigate",
        description=(
            "Navigate within the active browser context. Same approval "
            "semantics as `browser_open`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
        handler=_browser_navigate_handler,
    ),
    Tool(
        name="browser_screenshot",
        description=(
            "Capture a screenshot of the active page (or a CSS selector "
            "within it). Returns the file path of the saved PNG."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "selector": {"type": "string", "description": "Optional CSS selector"},
            },
        },
        handler=_browser_screenshot_handler,
    ),
    Tool(
        name="browser_get_text",
        description=(
            "Extract innerText from the active page (or a selector). "
            "Output is clipped at ~4 KB by the dispatcher."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "selector": {"type": "string"},
            },
        },
        handler=_browser_get_text_handler,
    ),
    Tool(
        name="browser_get_html",
        description=(
            "Extract outerHTML from the active page (or a selector). "
            "Output is clipped at ~4 KB."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
                "selector": {"type": "string"},
            },
        },
        handler=_browser_get_html_handler,
    ),
    Tool(
        name="browser_close",
        description="Tear down the session's active browser context.",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default"},
            },
        },
        handler=_browser_close_handler,
    ),
    Tool(
        name="recall",
        description=(
            "Search chat history across sessions. Returns a list of "
            "citation hits (session_id + message_index + content + "
            "follow_up). Read-only. ``since`` accepts a relative "
            "spec like ``\"24h\"`` or ``\"7d\"``, or an ISO timestamp."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "default": "default",
                               "description": "Session id used for "
                                              "the dispatcher event "
                                              "envelope (search target "
                                              "is set by the optional "
                                              "`session` field below)."},
                "query": {"type": "string", "description": "Substring(s) to match"},
                "since": {"type": "string", "description": "Recency window: '24h' / '7d' / ISO ts"},
                "session": {"type": "string", "description": "Restrict search to one session id"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        handler=_recall_handler,
    ),
]


def get_tool(name: str) -> Optional[Tool]:
    for tool in TOOLS:
        if tool.name == name:
            return tool
    return None


def list_tool_descriptors() -> List[Dict[str, Any]]:
    return [t.descriptor() for t in TOOLS]


# ============================================================
# Resource handlers (Phase 1: just sessions://list)
# ============================================================

def list_resource_descriptors() -> List[Dict[str, Any]]:
    return [
        {
            "uri": "sessions://list",
            "name": "Session list",
            "description": "All sessions known to the dispatcher.",
            "mimeType": "application/json",
        },
    ]


def read_resource(uri: str) -> Dict[str, Any]:
    """Resolve an MCP resource URI to its content blocks."""
    from app.memory import local_memory

    if uri == "sessions://list":
        sessions = local_memory.list_sessions()
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": _json_dump({"sessions": sessions}),
                }
            ]
        }

    raise KeyError(f"unknown resource uri: {uri}")


def _json_dump(obj):
    import json
    return json.dumps(obj, default=str)
