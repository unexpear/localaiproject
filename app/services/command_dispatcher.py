import os

from app.memory import local_memory
from app.services import error_vocabulary as ER
from app.services.error_vocabulary import ERROR_REASONS  # re-exported; see error-vocabulary.md
from app.services.chat_service import handle_chat, retry_saved_execution
from app.services.compaction_service import (
    format_compaction_result,
    get_compact_summary,
    maybe_compact_session,
    render_compact_summary,
)
from app.services.execution_service import (
    EXECUTION_LIST_DEFAULT_LIMIT,
    EXECUTION_LIST_MAX_LIMIT,
    format_execution_detail,
    format_execution_list,
)
from app.services.model_service import (
    ensure_models_installed,
    format_install_results,
    format_models_overview,
    format_single_install_result,
    format_status,
    format_uninstall_result,
    install_single_model,
    uninstall_single_model,
)
from app.services.lsp import (
    available_servers as lsp_available_servers,
    get_diagnostics as lsp_get_diagnostics,
)
from app.services.lsp.service import format_diagnostics as lsp_format_diagnostics
from app.services.onboarding_service import format_doctor_report, format_onboarding_guide
from app.services.skill_service import format_skill_overview, get_skill, normalize_active_skills
from app.services.workspace_service import (
    apply_workspace_delete,
    apply_workspace_write,
    compute_unified_diff,
    describe_pending_action,
    format_workspace_read_result,
    format_workspace_search_result,
    read_existing_for_diff,
    read_workspace_files,
    render_workspace_status,
    resolve_workspace_path,
    resolve_workspace_write_path,
    search_workspace,
    sha256_text,
)

HELP_TEXT = "\n".join(
    [
        "Commands",
        "- help: show command list",
        "- status: inspect runtime and registered models",
        "- models: show lane/model state with hardware fit in one view",
        "- doctor: inspect the full local control-plane state for this session",
        "- onboard: show the guided local setup path and next moves",
        "- history [n]: show recent messages for the current session",
        "- executions [n]: show recent execution records for this session",
        "- executions search <text> [lane=...] [status=...] [since=...]: filter the log",
        "- execution <id|last>: inspect one execution in detail",
        "- retry <id|last>: rerun a saved execution from its recorded state",
        "- summary: show compact session memory",
        "- compact: summarize older turns into compact session memory",
        "- workspace: show the current workspace",
        "- workspace <path>: pin a workspace to the current session",
        "- workspace clear: remove the pinned workspace",
        "- read <path>: request approval to read a file inside the pinned workspace",
        "- search <query>: request approval to search inside the pinned workspace",
        "- write <path> <<EOF\\n<body>\\nEOF: request approval to write a file (Build mode only)",
        "- delete <path>: request approval to delete a file (Build mode only)",
        "- diff: show the diff for the currently pending write/delete proposal",
        "- workspace writes-bump <n>: raise the per-session write soft cap",
        "- approve: run the pending approved-or-denied workspace action",
        "- deny: cancel the pending workspace action",
        "- link <builder-session>: add a builder to this session's link list (planner -> builder fan-out)",
        "- link clear [<builder>] | link status: drop one builder, drop all, or inspect the current links",
        "- dispatch <task>: fan out the task to ALL linked builders (one queued task each)",
        "- dispatch @<builder> <task>: queue the task on a single specified builder",
        "- do <task> | do @<builder> <task>: dispatch + drain inbox + wait until done (durable goal verb)",
        "- iterate <task> | iterate @<builder> <task> [--max <n>]: re-dispatch until builder emits <task-complete> (default max 5, hard cap 20)",
        "- notes [show] | notes append <text> | notes set <text> | notes clear: free-form session scratchpad (working_memory block)",
        "- note list | note show <label> | note set <label> <text> | note describe <label> <desc> | note budget <label> <bytes> | note clear <label> | note remove <label>: structured memory blocks (Phase 2)",
        "- cowork \"<goal>\": plan + approve + execute + synthesize a multi-step goal",
        "- cowork plan \"<goal>\" | cowork run <id> | cowork status | cowork cancel: cowork lifecycle controls",
        "- template list | template show <id> | template run <id> [k=v ...] | template reload: saved + parameterised command sequences",
        "- rules [list] | rules show <id> | rules reload | rules disable|enable <id>: declarative dispatcher constraints (rules/*.toml)",
        "- tags [show] | tags add <tag> | tags remove <tag> | tags clear: per-session labels used by rule predicates",
        "- provider list | provider show | provider bind <lane> <provider:model> | provider unbind <lane>: opt-in cloud chat lanes (chat-providers.md)",
        "- mcp [info] | mcp tools | mcp config | mcp serve: MCP server surface (Claude Code / Cursor / OpenCode)",
        "- browser open <url> | browser navigate <url> | browser screenshot|get-text|get-html [<selector>] | browser close: read-only browser skill (Phase 1)",
        "- backlog [list] | backlog pull <source-id> [k=v ...]: pull tasks from external sources into the inbox",
        "- recall <query> [--since 24h] [--session <id>] [--limit N]: search chat history across sessions",
        "- wait inbox <session> [<seconds>]: block until a builder's inbox is empty",
        "- wait task <task-id> [<seconds>]: block until a specific task is terminal",
        "- inbox: list queued tasks waiting on this session (as builder)",
        "- inbox next: run the next queued task as a chat turn (auto-marks done)",
        "- inbox auto [<max>]: keep running queued tasks until empty / max reached / paused for approval",
        "- inbox skip: drop the next queued task without running it",
        "- tasks [status]: list all tasks involving this session (any status)",
        "- gateway start [host:port]: start the local HTTP+SSE gateway so external agents can drive this process",
        "- gateway stop | gateway status: control or inspect the gateway",
        "- lsp: show installed language servers",
        "- lsp diagnose <path>: run LSP diagnostics on a workspace file (Python/Go/Rust/TS/C++/Bash/etc.)",
        "- mode: show the current Plan/Build mode",
        "- mode plan|build: switch the session between Plan (read-only context) and Build (full edit) modes",
        "- undo: revert the last memory mutation, or restore the last approved file write/delete (whichever is most recent)",
        "- write undo: alias of `undo` for use mid-write-flow (rolls back the last approved write/delete on disk)",
        "- redo: re-apply the last undone mutation",
        "- cmd list | cmd add <name> <prompt> | cmd remove <name> | cmd show <name>: manage custom slash commands",
        "- skills: show available and active skills",
        "- skill add <name>: enable a skill for the current session",
        "- skill remove <name>: disable a skill for the current session",
        "- skill clear: clear active skills for the current session",
        "- sessions: list saved sessions",
        "- session <name>: switch to or create a session",
        "- install-defaults: pull missing required models",
        "- install <role|model>: pull one lane on demand (e.g. install vision, install unfiltered)",
        "- uninstall <role|model>: remove a locally installed model",
        "- exit: quit the app",
    ]
)


def _format_history(session_id, limit):
    context = local_memory.get_session_context(session_id=session_id)
    summary = render_compact_summary(get_compact_summary(session_id))
    history = local_memory.get_history(session_id=session_id, limit=limit)
    has_context = bool(
        context.get("workspace_path")
        or context.get("active_skills")
        or context.get("pending_approval")
        or context.get("approved_search")
    )
    if not history and not summary and not has_context:
        return f"No saved history for session '{session_id}'."

    lines = [f"Session: {session_id}"]
    if context.get("workspace_path"):
        lines.append(f"Workspace: {context['workspace_path']}")
    if context.get("active_skills"):
        lines.append(f"Skills: {', '.join(context['active_skills'])}")
    if context.get("pending_approval"):
        lines.append(f"Pending approval: {describe_pending_action(context['pending_approval'])}")
    if context.get("approved_search"):
        search = context["approved_search"]
        files = ", ".join(search.get("files", [])[:2]) or "none"
        lines.append(f"Last approved search: {search.get('query') or '(unknown query)'} -> {files}")
    if len(lines) > 1:
        lines.append("")
    if summary:
        lines.extend(["Compact memory:", summary, ""])

    for entry in history:
        role = entry.get("role", "unknown").capitalize()
        content = entry.get("content", "").replace("\r", " ").replace("\n", " ").strip()
        if len(content) > 160:
            content = content[:157] + "..."
        model = entry.get("model")
        suffix = f" [{model}]" if model else ""
        lines.append(f"{role}{suffix}: {content}")
    return "\n".join(lines)


def _parse_history_limit(text):
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit():
        return max(1, int(parts[1]))
    return 10


def _parse_executions_limit(text):
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit():
        return max(1, min(EXECUTION_LIST_MAX_LIMIT, int(parts[1])))
    return EXECUTION_LIST_DEFAULT_LIMIT


_EXEC_FILTER_KEYS = {"lane", "status", "since"}


def _parse_executions_args(text):
    """Parse the tail of an `executions` invocation.

    Recognised forms:
        executions
        executions 50                          -> limit=50
        executions search router.py            -> query
        executions lane=coding                 -> lane filter
        executions lane=coding status=ready    -> combined filters
        executions search router.py lane=coding limit=5
    """
    parts = text.split()
    if not parts or parts[0].lower() not in {"executions", "/executions"}:
        return {"limit": EXECUTION_LIST_DEFAULT_LIMIT, "filters_used": False}

    tokens = parts[1:]
    args = {
        "limit": EXECUTION_LIST_DEFAULT_LIMIT,
        "query": None,
        "lane": None,
        "status": None,
        "since": None,
        "filters_used": False,
    }
    consume_query = False
    query_tokens: list[str] = []

    for token in tokens:
        if token.isdigit():
            args["limit"] = max(1, min(EXECUTION_LIST_MAX_LIMIT, int(token)))
            continue
        lowered = token.lower()
        if lowered == "search":
            consume_query = True
            args["filters_used"] = True
            continue
        if "=" in token:
            key, _, val = token.partition("=")
            key = key.lower().strip()
            val = val.strip()
            if key == "limit" and val.isdigit():
                args["limit"] = max(1, min(EXECUTION_LIST_MAX_LIMIT, int(val)))
                continue
            if key in _EXEC_FILTER_KEYS and val:
                args[key] = val
                args["filters_used"] = True
                consume_query = False
                continue
        if consume_query:
            query_tokens.append(token)
            continue

    if query_tokens:
        args["query"] = " ".join(query_tokens)
    return args


def _handle_executions_command(text, session_id="default"):
    args = _parse_executions_args(text)
    if not args.get("filters_used"):
        # Legacy path: `executions` or `executions <n>` returns the latest N.
        executions = local_memory.get_executions(session_id=session_id, limit=args["limit"])
        return {
            "action": "executions",
            "session_id": session_id,
            "output": format_execution_list(executions, session_id=session_id, limit=args["limit"]),
        }
    matches = local_memory.search_executions(
        session_id=session_id,
        query=args.get("query"),
        lane=args.get("lane"),
        status=args.get("status"),
        since=args.get("since"),
        limit=args["limit"],
    )
    summary_bits = []
    if args.get("query"):
        summary_bits.append(f'query="{args["query"]}"')
    for k in ("lane", "status", "since"):
        if args.get(k):
            summary_bits.append(f'{k}={args[k]}')
    header = f"Executions search · {', '.join(summary_bits) if summary_bits else 'no filter'} · {len(matches)} match"
    if len(matches) != 1:
        header += "es"
    body = format_execution_list(matches, session_id=session_id, limit=args["limit"])
    return {
        "action": "executions",
        "session_id": session_id,
        "output": f"{header}\n{body}",
        "matches": matches,
    }


_RESERVED_CMD_NAMES = frozenset({
    "help", "status", "models", "doctor", "onboard", "history", "executions",
    "execution", "retry", "summary", "compact", "workspace", "read", "search",
    "approve", "deny", "skills", "skill", "sessions", "session",
    "install-defaults", "install", "uninstall", "mode", "lsp", "undo", "redo",
    "cmd", "link", "dispatch", "inbox", "tasks", "gateway",
    "do", "iterate", "wait", "notes", "note", "cowork", "template",
    "rules", "tags", "provider", "mcp", "browser", "backlog", "recall",
    "prompt", "instructions", "proactive", "classifier", "retrieve", "tool",
    "exit", "quit",
})


def _classify_install_result(result):
    """Map an install/uninstall result dict to a canonical reason tag.

    Returns None when the result represents success (or 'already
    installed'). Walks the same English strings install_manager emits;
    update both in lock-step.
    """
    if not result or result.get("success"):
        return None
    if result.get("model") is None:
        # `install_manager.install_model` returns model=None when the
        # target is an unknown role or model name.
        return ER.UNKNOWN_TARGET
    output = (result.get("output") or "").lower()
    if "too large for available ram" in output or "too big" in output:
        return ER.HARDWARE_TOO_SMALL
    if "required for this model" in output and "ollama" in output:
        return ER.VERSION_TOO_OLD
    if "connection" in output or "refused" in output or "unreachable" in output:
        return ER.RUNTIME_OFFLINE
    # Generic pull / install failure (network blip, server error, etc.)
    return ER.MODEL_UNAVAILABLE


def _classify_dispatch_value_error(message):
    """Map a `local_memory` ValueError message to a canonical reason tag.

    The dispatch helpers (`add_task`, `dispatch_to_all`, `add_builder`)
    all raise `ValueError` with English messages. This helper centralises
    the substring → tag mapping so each handler doesn't reinvent it.
    Brittle in the abstract, but bounded: when local_memory grows new
    error shapes, update this one function.
    """
    lower = (message or "").lower()
    if "is not linked to any builder" in lower:
        return ER.NO_LINK
    if "is not linked to" in lower:
        return ER.UNKNOWN_BUILDER
    return ER.INVALID_ARGUMENT


def _format_terminal_task(task):
    if not task:
        return "  (task vanished)"
    short = (task.get("task") or "").replace("\n", " ").strip()
    if len(short) > 80:
        short = short[:77] + "..."
    sym = {"done": "✓", "failed": "✗", "skipped": "·"}.get(task.get("status"), "!")
    suffix = ""
    if task.get("status") == "in_progress" and task.get("result") == "(awaiting workspace approval)":
        suffix = "  (paused — workspace approval required)"
    elif task.get("result"):
        result = str(task["result"]).replace("\n", " ")
        if len(result) > 100:
            result = result[:97] + "..."
        suffix = f"  → {result}"
    return f"  {sym} {task.get('id')}  {task.get('status', '?'):<11}  {short}{suffix}"


def _handle_iterate_command(text, session_id="default"):
    """`iterate <task>` (or `iterate @<builder> <task>`) — re-dispatch until
    the builder emits `<task-complete>` or `max_iterations` is reached.

    Optional trailing `--max <n>` token sets the iteration cap (default 5,
    hard cap 20). All other parsing matches `do`.
    """
    from app.services import goals

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return {
            "action": "iterate",
            "session_id": session_id,
            "output": (
                "Usage:\n"
                "  iterate <task>            (loop on every linked builder until <task-complete>)\n"
                "  iterate @<builder> <task> (loop on a single builder)\n"
                "  iterate <task> --max 10   (override the iteration cap; max 20)"
            ),
            "reason": ER.INVALID_ARGUMENT,
        }
    rest = parts[1].strip()

    # Strip optional trailing `--max <n>`.
    max_iterations = 5
    if "--max" in rest:
        head, _flag, tail = rest.rpartition("--max")
        tail = tail.strip()
        if tail:
            try:
                max_iterations = int(tail.split()[0])
            except ValueError:
                return {
                    "action": "iterate",
                    "session_id": session_id,
                    "output": f"Invalid --max value: {tail!r}. Pass a positive integer.",
                    "reason": ER.INVALID_ARGUMENT,
                }
            rest = head.strip()

    target_builder = None
    body = rest
    if rest.startswith("@"):
        sub = rest[1:].split(maxsplit=1)
        if not sub or not sub[0]:
            return {
                "action": "iterate",
                "session_id": session_id,
                "output": "Usage: iterate @<builder> <task>",
                "reason": ER.INVALID_ARGUMENT,
            }
        target_builder = sub[0]
        body = sub[1] if len(sub) >= 2 else ""
        if not body.strip():
            return {
                "action": "iterate",
                "session_id": session_id,
                "output": "Usage: iterate @<builder> <task>",
                "reason": ER.INVALID_ARGUMENT,
            }

    try:
        summary = goals.iterate_until_complete(
            session_id,
            body,
            builder=target_builder,
            max_iterations=max_iterations,
        )
    except ValueError as exc:
        return {
            "action": "iterate",
            "session_id": session_id,
            "output": f"Could not iterate: {exc}",
            "reason": _classify_dispatch_value_error(str(exc)),
        }

    sym = "✓" if summary["completed"] else "·"
    output_lines = [
        f"{sym} iterate · {summary['iterations']} iter(s) · "
        f"stopped: {summary['stopped_reason']} · last: {summary['last_status']}"
    ]
    if summary["last_result"]:
        preview = str(summary["last_result"]).replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:197] + "..."
        output_lines.append(f"  last_result: {preview}")
    output_lines.append(f"  task_ids: {', '.join(summary['task_ids']) or '(none)'}")

    event = {
        "action": "iterate",
        "session_id": session_id,
        "output": "\n".join(output_lines),
        "result": summary,
    }
    # Map non-completion reasons to the standard error vocabulary so
    # consumers can pattern-match.
    reason_map = {
        "cancelled": ER.CANCELLED,
        "timeout": ER.TIMEOUT,
        "paused": ER.APPROVAL_REQUIRED,
        "failed": ER.INTERNAL_ERROR,
    }
    if summary["stopped_reason"] in reason_map:
        event["reason"] = reason_map[summary["stopped_reason"]]
    return event


def _handle_do_command(text, session_id="default"):
    """`do <task>` (fan out to all linked builders + wait) or `do @<builder> <task>`."""
    from app.services import goals

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return {
            "action": "do",
            "session_id": session_id,
            "output": (
                "Usage:\n"
                "  do <task>            (fan out to ALL linked builders + wait until each is done)\n"
                "  do @<builder> <task> (single builder + wait)"
            ),
            "reason": ER.INVALID_ARGUMENT,
        }
    rest = parts[1].strip()

    target_builder = None
    body = rest
    if rest.startswith("@"):
        sub = rest[1:].split(maxsplit=1)
        if not sub or not sub[0]:
            return {
                "action": "do",
                "session_id": session_id,
                "output": "Usage: do @<builder> <task>",
                "reason": ER.INVALID_ARGUMENT,
            }
        target_builder = sub[0]
        body = sub[1] if len(sub) >= 2 else ""
        if not body.strip():
            return {
                "action": "do",
                "session_id": session_id,
                "output": "Usage: do @<builder> <task>",
                "reason": ER.INVALID_ARGUMENT,
            }

    if target_builder is not None:
        try:
            final = goals.dispatch_and_wait(session_id, body, builder=target_builder)
        except ValueError as exc:
            return {
                "action": "do",
                "session_id": session_id,
                "output": f"Could not dispatch: {exc}",
                "reason": _classify_dispatch_value_error(str(exc)),
            }
        return {
            "action": "do",
            "session_id": session_id,
            "output": (
                f"Dispatched + waited on '{target_builder}':\n"
                + _format_terminal_task(final)
            ),
            "result": final,
        }

    try:
        finals = goals.fan_out_and_wait(session_id, body)
    except ValueError as exc:
        return {
            "action": "do",
            "session_id": session_id,
            "output": f"Could not dispatch: {exc}",
            "reason": _classify_dispatch_value_error(str(exc)),
        }
    if not finals:
        return {
            "action": "do",
            "session_id": session_id,
            "output": "No builders linked — nothing to dispatch.",
            "reason": ER.NO_LINK,
        }
    done = sum(1 for t in finals if (t or {}).get("status") == "done")
    failed = sum(1 for t in finals if (t or {}).get("status") == "failed")
    paused = sum(1 for t in finals
                 if (t or {}).get("status") == "in_progress"
                 and (t or {}).get("result") == "(awaiting workspace approval)")
    header = f"Fan-out + wait · {len(finals)} task(s): {done} done, {failed} failed, {paused} paused"
    body_lines = "\n".join(_format_terminal_task(t) for t in finals)
    return {
        "action": "do",
        "session_id": session_id,
        "output": f"{header}\n{body_lines}",
        "result": finals,
    }


def _parse_wait_seconds(text):
    """Parse a trailing optional integer argument as a timeout. Returns None if absent."""
    text = text.strip()
    if not text:
        return None
    try:
        return max(1, int(text))
    except ValueError:
        return None


def _handle_wait_command(text, session_id="default"):
    from app.services import goals

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else ""

    if sub == "inbox":
        rest = parts[2].split() if len(parts) >= 3 else []
        if not rest:
            return {
                "action": "wait",
                "session_id": session_id,
                "output": "Usage: wait inbox <session> [<seconds>]",
                "reason": ER.INVALID_ARGUMENT,
            }
        target = rest[0]
        timeout = _parse_wait_seconds(rest[1]) if len(rest) >= 2 else None
        ok = goals.wait_for_inbox_empty(target, timeout=timeout)
        if ok:
            return {"action": "wait", "session_id": session_id,
                    "output": f"Inbox for '{target}' drained."}
        return {
            "action": "wait",
            "session_id": session_id,
            "output": f"Timed out waiting for '{target}' inbox to drain.",
            "reason": ER.TIMEOUT,
        }

    if sub == "task":
        rest = parts[2].split() if len(parts) >= 3 else []
        if not rest:
            return {
                "action": "wait",
                "session_id": session_id,
                "output": "Usage: wait task <task-id> [<seconds>]",
                "reason": ER.INVALID_ARGUMENT,
            }
        task_id = rest[0]
        timeout = _parse_wait_seconds(rest[1]) if len(rest) >= 2 else None
        final = goals.wait_for_task(task_id, timeout=timeout)
        if final is None:
            return {
                "action": "wait",
                "session_id": session_id,
                "output": f"No task with id '{task_id}'.",
                "reason": ER.UNKNOWN_TARGET,
            }
        if final.get("status") in goals.TERMINAL_STATUSES:
            return {"action": "wait", "session_id": session_id,
                    "output": f"Task {task_id} terminal:\n{_format_terminal_task(final)}"}
        return {
            "action": "wait",
            "session_id": session_id,
            "output": f"Timed out — task {task_id} still {final.get('status')}.",
            "reason": ER.TIMEOUT,
        }

    return {
        "action": "wait",
        "session_id": session_id,
        "output": "Usage: wait inbox <session> [<seconds>] | wait task <task-id> [<seconds>]",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_gateway_command(text, session_id="default"):
    """`gateway [start [host:port] | stop | status]`."""
    from app.services.gateway import (
        DEFAULT_HOST,
        DEFAULT_PORT,
        TOKEN_ENV,
        get_singleton,
    )

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "status"

    if sub == "status":
        srv = get_singleton()
        if srv.is_running():
            auth = "ENABLED (LOCALAI_GATEWAY_TOKEN set)" if os.environ.get(TOKEN_ENV) else "off (loopback only)"
            output = (
                f"Gateway running on http://{srv.host}:{srv.port}\n"
                f"  POST /dispatch       JSON {{session_id, text}}\n"
                f"  GET  /sessions/<id>/state\n"
                f"  GET  /events?session=<id>   (Server-Sent Events)\n"
                f"  Auth: {auth}"
            )
        else:
            output = "Gateway is not running. Use `gateway start` to launch it."
        return {"action": "gateway", "session_id": session_id, "output": output}

    if sub == "stop":
        srv = get_singleton()
        result = srv.stop()
        if result.get("ok"):
            return {"action": "gateway", "session_id": session_id, "output": "Gateway stopped."}
        return {
            "action": "gateway",
            "session_id": session_id,
            "output": f"Could not stop gateway: {result.get('error', 'unknown')}",
            "reason": ER.INTERNAL_ERROR,
        }

    if sub == "start":
        host = DEFAULT_HOST
        port = DEFAULT_PORT
        rest = parts[2].strip() if len(parts) >= 3 else ""
        if rest:
            if ":" in rest:
                host_str, port_str = rest.rsplit(":", 1)
                host = host_str.strip() or DEFAULT_HOST
                try:
                    port = int(port_str.strip())
                except ValueError:
                    return {
                        "action": "gateway",
                        "session_id": session_id,
                        "output": f"Invalid port in '{rest}'. Use host:port.",
                        "reason": ER.INVALID_ARGUMENT,
                    }
            else:
                # Single token treated as port number.
                try:
                    port = int(rest)
                except ValueError:
                    return {
                        "action": "gateway",
                        "session_id": session_id,
                        "output": f"Usage: gateway start [host:port]  (got '{rest}')",
                        "reason": ER.INVALID_ARGUMENT,
                    }
        # Always sandbox to loopback unless an explicit non-127 host is given.
        srv = get_singleton()
        srv.host = host
        srv.port = port
        result = srv.start()
        if not result.get("ok"):
            err_msg = str(result.get("error", "")).lower()
            # "already running" is its own canonical tag; everything else
            # is generic internal_error from the OS / socket layer.
            if "already running" in err_msg or "already in use" in err_msg:
                reason = ER.ALREADY_RUNNING
            else:
                reason = ER.INTERNAL_ERROR
            return {
                "action": "gateway",
                "session_id": session_id,
                "output": f"Could not start gateway: {result.get('error', 'unknown')}",
                "reason": reason,
            }
        auth = "ENABLED" if result.get("auth_required") else "disabled (loopback only)"
        return {
            "action": "gateway",
            "session_id": session_id,
            "output": (
                f"Gateway started on http://{result['host']}:{result['port']}\n"
                f"Auth: {auth}\n"
                f"Try:  curl -s http://{result['host']}:{result['port']}/health"
            ),
            "result": result,
        }

    return {
        "action": "gateway",
        "session_id": session_id,
        "output": "Usage: gateway [status | start [host:port] | stop]",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_lsp_status(session_id="default"):
    available = lsp_available_servers()
    if not available:
        from app.services.lsp.registry import all_known_servers
        lines = [
            "LSP · no language servers installed on PATH.",
            "",
            "Free, no-strings options you can install yourself:",
        ]
        for server in all_known_servers():
            lines.append(f"  · {server.label} [{server.license}] for {server.language_id} — {server.install_hint}")
        return {"action": "lsp", "session_id": session_id, "output": "\n".join(lines)}
    lines = [f"LSP · {len(available)} server(s) detected on PATH:"]
    for server in available:
        lines.append(f"  · {server.label} [{server.license}] — {server.language_id}")
    return {"action": "lsp", "session_id": session_id, "output": "\n".join(lines)}


def _handle_lsp_command(text, session_id="default"):
    parts = text.split(maxsplit=2)
    # parts[0] == "lsp"; parts[1] is the action; parts[2] is the rest.
    action = parts[1].lower() if len(parts) >= 2 else ""

    if action in ("", "status"):
        return _handle_lsp_status(session_id=session_id)

    if action in ("diagnose", "diag", "check"):
        if len(parts) < 3 or not parts[2].strip():
            return {
                "action": "lsp",
                "session_id": session_id,
                "output": "Usage: lsp diagnose <path>",
                "reason": ER.INVALID_ARGUMENT,
            }
        target = parts[2].strip().strip('"').strip("'")
        # Resolve relative to the pinned workspace if any.
        context = local_memory.get_session_context(session_id=session_id)
        workspace_root = context.get("workspace_path")
        if workspace_root and not os.path.isabs(target):
            target = os.path.join(workspace_root, target)
        result = lsp_get_diagnostics(target, workspace_root=workspace_root)
        return {
            "action": "lsp",
            "session_id": session_id,
            "output": lsp_format_diagnostics(result),
            "result": {
                "ok": result.ok,
                "language": result.language,
                "diagnostics": result.diagnostics,
                "error": result.error,
            },
        }

    return {
        "action": "lsp",
        "session_id": session_id,
        "output": "Usage: lsp [status] | lsp diagnose <path>",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_cmd_list(session_id="default"):
    commands = local_memory.list_custom_commands()
    if not commands:
        return {
            "action": "cmd",
            "session_id": session_id,
            "output": "No custom commands yet. Use `cmd add <name> <prompt>` to define one.",
        }
    lines = ["Custom commands:"]
    for name, entry in commands.items():
        prompt = entry.get("prompt") or ""
        if len(prompt) > 80:
            prompt = prompt[:77] + "..."
        lines.append(f"  /{name} -> {prompt}")
    return {"action": "cmd", "session_id": session_id, "output": "\n".join(lines)}


def _handle_cmd_command(text, session_id="default"):
    parts = text.split(maxsplit=2)
    # parts[0] == "cmd"; parts[1] is the action; parts[2] is the rest.
    if len(parts) < 2:
        return _handle_cmd_list(session_id=session_id)
    action = parts[1].lower()
    rest = parts[2] if len(parts) >= 3 else ""

    if action == "list":
        return _handle_cmd_list(session_id=session_id)

    if action == "add":
        if not rest.strip():
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": "Usage: cmd add <name> <prompt>",
                "reason": ER.INVALID_ARGUMENT,
            }
        sub = rest.split(maxsplit=1)
        if len(sub) != 2 or not sub[1].strip():
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": "Usage: cmd add <name> <prompt>",
                "reason": ER.INVALID_ARGUMENT,
            }
        name, prompt = sub[0].strip().lower(), sub[1].strip()
        if name in _RESERVED_CMD_NAMES:
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": f"Cannot use '{name}' — that name is reserved by a built-in command.",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            entry = local_memory.add_custom_command(name, prompt)
        except ValueError as exc:
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": f"Could not save: {exc}",
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "cmd",
            "session_id": session_id,
            "output": f"Saved /{entry['name']}.",
            "result": entry,
        }

    if action == "remove":
        name = rest.strip().lower()
        if not name:
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": "Usage: cmd remove <name>",
                "reason": ER.INVALID_ARGUMENT,
            }
        removed = local_memory.remove_custom_command(name)
        return {
            "action": "cmd",
            "session_id": session_id,
            "output": f"Removed /{name}." if removed else f"No custom command named /{name}.",
        }

    if action == "show":
        name = rest.strip().lower()
        if not name:
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": "Usage: cmd show <name>",
            }
        entry = local_memory.get_custom_command(name)
        if not entry:
            return {
                "action": "cmd",
                "session_id": session_id,
                "output": f"No custom command named /{name}.",
            }
        return {
            "action": "cmd",
            "session_id": session_id,
            "output": f"/{entry['name']}:\n{entry['prompt']}",
        }

    return {
        "action": "cmd",
        "session_id": session_id,
        "output": "Usage: cmd list | cmd add <name> <prompt> | cmd remove <name> | cmd show <name>",
    }


def _format_task_row(task):
    short = (task.get("task") or "").replace("\n", " ").strip()
    if len(short) > 70:
        short = short[:67] + "..."
    return f"  {task.get('id')}  {task.get('status'):<11}  {short}"


def _format_link_status(session_id):
    """Build a human-readable summary of the session's link state."""
    builders = local_memory.get_builders(session_id)
    planners = local_memory.get_planners(session_id)
    lines = []
    if builders:
        all_tasks = local_memory.list_tasks(session_id)
        per_builder_counts: dict = {b: {} for b in builders}
        for t in all_tasks:
            target = t.get("builder_session")
            if target in per_builder_counts:
                per_builder_counts[target][t["status"]] = per_builder_counts[target].get(t["status"], 0) + 1
        lines.append(
            f"As planner: '{session_id}' -> {len(builders)} builder(s): "
            f"{', '.join(builders)}"
        )
        for b in builders:
            counts = per_builder_counts[b]
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no tasks yet"
            lines.append(f"  -> {b}: {summary}")
    if planners:
        lines.append(f"As builder: receives tasks from {len(planners)} planner(s): {', '.join(planners)}")
    if not lines:
        lines.append(
            f"No links involving '{session_id}'. "
            "Use `link <builder-session>` to start fanning tasks out."
        )
    return "\n".join(lines)


def _handle_link_command(text, session_id):
    parts = text.split(maxsplit=2)
    # parts[0] == "link"; parts[1] is the action; parts[2] is the rest.
    if len(parts) < 2 or not parts[1].strip():
        return {
            "action": "link",
            "session_id": session_id,
            "output": _format_link_status(session_id),
        }
    sub = parts[1].strip().lower()
    rest = parts[2].strip() if len(parts) >= 3 else ""

    if sub == "status":
        return {"action": "link", "session_id": session_id, "output": _format_link_status(session_id)}

    if sub == "clear":
        if rest:
            removed = local_memory.remove_builder(session_id, rest)
            msg = (
                f"Removed builder '{rest}' from '{session_id}'."
                if removed else
                f"Builder '{rest}' was not linked to '{session_id}'."
            )
        else:
            removed = local_memory.clear_builders(session_id)
            msg = "Cleared all builders." if removed else "No builders to clear."
        return {"action": "link", "session_id": session_id, "output": msg}

    # First token after `link` is treated as a builder name.
    target = parts[1].strip()
    try:
        result = local_memory.add_builder(session_id, target)
    except ValueError as exc:
        return {
            "action": "link",
            "session_id": session_id,
            "output": f"Could not link: {exc}",
            "reason": ER.INVALID_ARGUMENT,
        }
    builders = result["builders"]
    blurb = (
        f"Linked '{result['planner']}' -> {len(builders)} builder(s): "
        f"{', '.join(builders)}. "
        + (
            "Use `dispatch <task>` to fan out, or `dispatch @<builder> <task>` to target one."
            if len(builders) > 1
            else "Use `dispatch <task>` to send work."
        )
    )
    return {"action": "link", "session_id": session_id, "output": blurb, "result": result}


def _handle_dispatch_command(text, session_id):
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return {
            "action": "dispatch",
            "session_id": session_id,
            "output": (
                "Usage:\n"
                "  dispatch <task>            (fan out to ALL linked builders)\n"
                "  dispatch @<builder> <task> (route to a specific builder)"
            ),
            "reason": ER.INVALID_ARGUMENT,
        }
    rest = parts[1].strip()

    target_builder = None
    body = rest
    if rest.startswith("@"):
        sub = rest[1:].split(maxsplit=1)
        if not sub or not sub[0]:
            return {
                "action": "dispatch",
                "session_id": session_id,
                "output": "Usage: dispatch @<builder> <task>",
                "reason": ER.INVALID_ARGUMENT,
            }
        target_builder = sub[0]
        body = sub[1] if len(sub) >= 2 else ""
        if not body.strip():
            return {
                "action": "dispatch",
                "session_id": session_id,
                "output": "Usage: dispatch @<builder> <task>",
                "reason": ER.INVALID_ARGUMENT,
            }

    if target_builder is not None:
        try:
            task = local_memory.add_task(session_id, body, builder=target_builder)
        except ValueError as exc:
            return {
                "action": "dispatch",
                "session_id": session_id,
                "output": f"Could not dispatch: {exc}",
                "reason": _classify_dispatch_value_error(str(exc)),
            }
        preview = task["task"][:80] + ("..." if len(task["task"]) > 80 else "")
        return {
            "action": "dispatch",
            "session_id": session_id,
            "output": f"Queued {task['id']} for '{task['builder_session']}': {preview}",
            "result": task,
        }

    # Bare form: fan out to all builders. If only one is linked, that's just one task.
    try:
        tasks = local_memory.dispatch_to_all(session_id, body)
    except ValueError as exc:
        return {
            "action": "dispatch",
            "session_id": session_id,
            "output": f"Could not dispatch: {exc}",
            "reason": _classify_dispatch_value_error(str(exc)),
        }
    preview = body[:80] + ("..." if len(body) > 80 else "")
    if len(tasks) == 1:
        msg = f"Queued {tasks[0]['id']} for '{tasks[0]['builder_session']}': {preview}"
    else:
        builder_list = ", ".join(t["builder_session"] for t in tasks)
        msg = (
            f"Fanned out '{preview}' to {len(tasks)} builder(s): {builder_list}\n"
            + "\n".join(f"  {t['id']} -> {t['builder_session']}" for t in tasks)
        )
    return {"action": "dispatch", "session_id": session_id, "output": msg, "result": tasks}


_INBOX_AUTO_DEFAULT_LIMIT = 50  # safety cap when no explicit count is given


def _post_task_feedback(planner, builder, task_id, response_text, marker="DONE"):
    """Append a `[task ... <marker>]` system note to the planner's history."""
    if not planner:
        return
    preview = (response_text or "").replace("\n", " ").strip()
    if len(preview) > 280:
        preview = preview[:277] + "..."
    local_memory.append_message(
        role="system",
        content=f"[task {task_id} from {builder}] {marker}: {preview}",
        session_id=planner,
    )


def _publish_task_event(task, outcome, *, planner_session=None, builder_session=None, error=None):
    """Best-effort publish of a task lifecycle event to the gateway bus."""
    try:
        from app.services.gateway.bus import GatewayEvent, KIND_TASK, default_bus

        bus = default_bus()
        # Publish on both planner and builder session_ids so subscribers on
        # either side see the activity. We do this with two separate
        # publishes so per-session filters work cleanly.
        for sid in {planner_session, builder_session}:
            if not sid:
                continue
            bus.publish(
                GatewayEvent(
                    kind=KIND_TASK,
                    session_id=sid,
                    payload={
                        "task_id": (task or {}).get("id"),
                        "outcome": outcome,
                        "status": (task or {}).get("status"),
                        "planner_session": planner_session,
                        "builder_session": builder_session,
                        "error": error,
                    },
                )
            )
    except Exception:  # noqa: BLE001
        # Same rule as publish_dispatch_event: never block runtime on bus issues.
        pass


def _run_one_inbox_task(session_id):
    """Pop one queued task off `session_id`'s inbox and run it as a chat turn.

    Returns a dict the caller can use to format the outcome:
      {
        "task": {...} | None,                  # None if inbox was empty
        "outcome": "done" | "failed" | "paused" | "empty",
        "response_text": str,
        "chat_event": {...} | None,
        "error": str | None,
      }

    Side effects per outcome:
      done    -> task marked done with response, planner gets DONE note
      failed  -> task marked failed with error, planner gets FAILED note
      paused  -> task left in_progress with "(awaiting workspace approval)";
                 pending_approval set on the session so `approve` can run it
      empty   -> nothing happened
    """
    task = local_memory.next_task(session_id)
    if not task:
        return {"task": None, "outcome": "empty", "response_text": "", "chat_event": None, "error": None}

    planner_id = task.get("planner_session")

    try:
        chat_event = handle_chat(task["task"], session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        updated = local_memory.mark_task(task["id"], status="failed", result=f"chat error: {exc}")
        _post_task_feedback(planner_id, session_id, task["id"], str(exc), marker="FAILED")
        _publish_task_event(updated or task, "failed",
                            planner_session=planner_id, builder_session=session_id, error=str(exc))
        return {
            "task": task,
            "outcome": "failed",
            "response_text": "",
            "chat_event": None,
            "error": str(exc),
        }

    if chat_event.get("approval_required") and chat_event.get("pending_action"):
        local_memory.set_pending_approval(
            session_id=session_id,
            pending_approval=chat_event["pending_action"],
        )
        updated = local_memory.mark_task(
            task["id"],
            status="in_progress",  # leave it; user must approve, then re-run
            result="(awaiting workspace approval)",
        )
        _publish_task_event(updated or task, "paused",
                            planner_session=planner_id, builder_session=session_id)
        return {
            "task": task,
            "outcome": "paused",
            "response_text": "",
            "chat_event": chat_event,
            "error": None,
        }

    response_text = chat_event.get("response") or ""
    if chat_event.get("ok") is False:
        # Treat any unsuccessful chat that didn't pause for approval as a task failure.
        err = chat_event.get("error") or "model returned no usable response"
        updated = local_memory.mark_task(task["id"], status="failed", result=err)
        _post_task_feedback(planner_id, session_id, task["id"], err, marker="FAILED")
        _publish_task_event(updated or task, "failed",
                            planner_session=planner_id, builder_session=session_id, error=err)
        return {
            "task": task,
            "outcome": "failed",
            "response_text": response_text,
            "chat_event": chat_event,
            "error": err,
        }

    updated = local_memory.mark_task(
        task["id"],
        status="done",
        result=response_text,
        execution_id=chat_event.get("execution_id"),
    )
    _post_task_feedback(planner_id, session_id, task["id"], response_text)
    _publish_task_event(updated or task, "done",
                        planner_session=planner_id, builder_session=session_id)
    return {
        "task": task,
        "outcome": "done",
        "response_text": response_text,
        "chat_event": chat_event,
        "error": None,
    }


def _format_inbox_outcome(outcome_result):
    """Render one outcome row for the inbox auto report."""
    task = outcome_result.get("task") or {}
    short = (task.get("task") or "").replace("\n", " ").strip()
    if len(short) > 60:
        short = short[:57] + "..."
    sym = {"done": "✓", "failed": "✗", "paused": "!"}.get(outcome_result["outcome"], "·")
    tag = outcome_result["outcome"]
    suffix = ""
    if outcome_result["outcome"] == "failed" and outcome_result.get("error"):
        err = outcome_result["error"].replace("\n", " ")
        if len(err) > 80:
            err = err[:77] + "..."
        suffix = f"  ({err})"
    elif outcome_result["outcome"] == "paused":
        suffix = "  (workspace approval required)"
    return f"  {sym} {task.get('id', '?')}  {tag:<8}  {short}{suffix}"


def _handle_inbox_command(text, session_id):
    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else ""

    if sub in ("", "list"):
        queued = local_memory.get_inbox(session_id, status="queued")
        in_progress = local_memory.in_progress_task_for(session_id)
        lines = []
        if in_progress:
            lines.append(f"In progress: {in_progress['id']}: {in_progress['task']}")
        if not queued and not in_progress:
            # Listing an empty inbox isn't an error per se (it's an
            # informational response) but consumers want to detect it
            # without parsing English. Mark it.
            return {
                "action": "inbox",
                "session_id": session_id,
                "output": "Inbox is empty.",
                "reason": ER.INBOX_EMPTY,
            }
        if queued:
            lines.append(f"Queued ({len(queued)}):")
            for task in queued:
                lines.append(_format_task_row(task))
        return {"action": "inbox", "session_id": session_id, "output": "\n".join(lines)}

    if sub == "next":
        result = _run_one_inbox_task(session_id)
        if result["outcome"] == "empty":
            return {
                "action": "inbox",
                "session_id": session_id,
                "output": "Inbox is empty — nothing to run.",
                "reason": ER.INBOX_EMPTY,
            }
        task = result["task"]
        chat_event = result.get("chat_event")
        if result["outcome"] == "paused":
            return {
                "action": "inbox",
                "session_id": session_id,
                "output": (
                    f"Task {task['id']} paused: workspace approval required.\n"
                    f"{_format_chat_result(chat_event)}"
                ),
                "result": chat_event,
                "reason": ER.APPROVAL_REQUIRED,
            }
        if result["outcome"] == "failed":
            return {
                "action": "inbox",
                "session_id": session_id,
                "output": f"Task {task['id']} failed: {result['error']}",
                "result": chat_event,
                "reason": ER.INTERNAL_ERROR,
            }
        # done
        return {
            "action": "inbox",
            "session_id": session_id,
            "output": (
                f"Task {task['id']} ran on '{session_id}'.\n"
                f"{_format_chat_result(chat_event)}"
            ),
            "result": chat_event,
        }

    if sub == "auto":
        # Optional limit: `inbox auto 5` runs at most 5 tasks before stopping.
        limit = _INBOX_AUTO_DEFAULT_LIMIT
        rest = parts[2].strip() if len(parts) >= 3 else ""
        if rest:
            try:
                limit = max(1, min(500, int(rest)))
            except ValueError:
                return {
                    "action": "inbox",
                    "session_id": session_id,
                    "output": "Usage: inbox auto [<max-tasks>]",
                    "reason": ER.INVALID_ARGUMENT,
                }

        outcomes = []
        stopped_reason = None
        for _ in range(limit):
            result = _run_one_inbox_task(session_id)
            if result["outcome"] == "empty":
                stopped_reason = "inbox empty"
                break
            outcomes.append(result)
            if result["outcome"] == "paused":
                stopped_reason = "workspace approval required"
                break

        if not outcomes:
            return {
                "action": "inbox",
                "session_id": session_id,
                "output": "Inbox is empty — nothing to run.",
                "reason": ER.INBOX_EMPTY,
            }

        if stopped_reason is None:
            stopped_reason = f"reached limit of {limit} tasks (queue may have more — run again)"

        done_count = sum(1 for r in outcomes if r["outcome"] == "done")
        failed_count = sum(1 for r in outcomes if r["outcome"] == "failed")
        paused_count = sum(1 for r in outcomes if r["outcome"] == "paused")
        header = (
            f"Auto-drained inbox for '{session_id}' · "
            f"{len(outcomes)} task(s): {done_count} done, {failed_count} failed, {paused_count} paused"
        )
        body = "\n".join(_format_inbox_outcome(r) for r in outcomes)
        footer = f"Stopped: {stopped_reason}."
        return {
            "action": "inbox",
            "session_id": session_id,
            "output": "\n".join([header, body, footer]),
            "result": {
                "outcomes": [
                    {
                        "task_id": (r["task"] or {}).get("id"),
                        "outcome": r["outcome"],
                        "error": r.get("error"),
                    }
                    for r in outcomes
                ],
                "stopped_reason": stopped_reason,
            },
        }

    if sub == "skip":
        task = local_memory.next_task(session_id)
        if not task:
            return {
                "action": "inbox",
                "session_id": session_id,
                "output": "Inbox is empty — nothing to skip.",
                "reason": ER.INBOX_EMPTY,
            }
        local_memory.mark_task(task["id"], status="skipped", result="(skipped by user)")
        return {
            "action": "inbox",
            "session_id": session_id,
            "output": f"Skipped {task['id']}: {task['task'][:80]}",
        }

    return {
        "action": "inbox",
        "session_id": session_id,
        "output": "Usage: inbox | inbox next | inbox auto [<max>] | inbox skip",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_tasks_command(text, session_id):
    parts = text.split(maxsplit=1)
    status = parts[1].strip().lower() if len(parts) >= 2 else None
    if status and status not in local_memory.VALID_TASK_STATUSES:
        return {
            "action": "tasks",
            "session_id": session_id,
            "output": (
                f"Unknown status '{status}'. "
                f"Choose from: {', '.join(local_memory.VALID_TASK_STATUSES)}"
            ),
            "reason": ER.INVALID_ARGUMENT,
        }
    tasks = local_memory.list_tasks(session_id, status=status)
    if not tasks:
        return {"action": "tasks", "session_id": session_id, "output": "No tasks involving this session yet."}
    lines = [f"Tasks involving '{session_id}'" + (f" (status={status})" if status else "") + ":"]
    for task in tasks:
        partner = task["builder_session"] if task["planner_session"] == session_id else task["planner_session"]
        direction = "->" if task["planner_session"] == session_id else "<-"
        short = (task.get("task") or "").replace("\n", " ").strip()
        if len(short) > 60:
            short = short[:57] + "..."
        lines.append(f"  {task['id']}  {task['status']:<11}  {direction} {partner}  {short}")
    return {"action": "tasks", "session_id": session_id, "output": "\n".join(lines)}


def _try_resolve_custom_command(normalized):
    """If `normalized` is a `/<name> [args]` invocation matching a saved custom
    command, return the substituted text (saved prompt + trailing args). Else None.
    """
    if not normalized.startswith("/"):
        return None
    body = normalized[1:].strip()
    if not body:
        return None
    parts = body.split(maxsplit=1)
    name = parts[0].lower()
    if not name or name in _RESERVED_CMD_NAMES:
        return None
    entry = local_memory.get_custom_command(name)
    if not entry:
        return None
    saved_prompt = entry["prompt"]
    if len(parts) == 2 and parts[1]:
        return f"{saved_prompt} {parts[1]}"
    return saved_prompt


def _format_cowork_run(run):
    """Render a cowork run for `cowork status` output."""
    if not run:
        return "(no run)"
    lines = [
        f"{run['id']} · {run['status']} · {run['goal']}",
        f"  session: {run['session_id']}",
        f"  sub_tasks: {len(run['plan'])}",
    ]
    for sub in run["plan"]:
        result = (run.get("subtask_results") or {}).get(sub["id"]) or {}
        sub_status = result.get("status", "(pending)")
        sym = {"done": "✓", "failed": "✗", "skipped": "·"}.get(sub_status, "?")
        lines.append(
            f"    {sym} {sub['id']:<6}  {sub['builder']:<10}  {sub['task'][:60]}"
            f"  [{sub_status}]"
        )
    if run.get("synthesis"):
        synth = run["synthesis"].replace("\n", " ").strip()
        if len(synth) > 200:
            synth = synth[:197] + "..."
        lines.append(f"  synthesis: {synth}")
    return "\n".join(lines)


def _handle_cowork_command(text, session_id):
    """`cowork "<goal>" | cowork plan "<goal>" | cowork run <id> | cowork status | cowork cancel`."""
    from app.services import cowork

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "status"

    # `cowork status` — list runs (default and explicit).
    if sub == "status":
        runs = local_memory.list_cowork_runs(session_id=session_id)
        if not runs:
            return {
                "action": "cowork",
                "session_id": session_id,
                "output": "No cowork runs in this session yet.",
            }
        active = local_memory.active_cowork_run(session_id)
        rendered = []
        if active is not None:
            rendered.append("Active run:")
            rendered.append(_format_cowork_run(active))
            rendered.append("")
        rendered.append(f"All runs ({len(runs)}):")
        for run in runs:
            rendered.append(
                f"  {run['id']}  {run['status']:<12}  {run['goal'][:60]}"
            )
        return {
            "action": "cowork",
            "session_id": session_id,
            "output": "\n".join(rendered),
            "result": {"runs": runs, "active": active},
        }

    if sub == "cancel":
        result = cowork.cancel_active_run(session_id)
        if not result.get("ok"):
            return {
                "action": "cowork",
                "session_id": session_id,
                "output": result.get("output", "Cannot cancel."),
                "reason": result.get("reason", ER.INVALID_ARGUMENT),
            }
        return {
            "action": "cowork",
            "session_id": session_id,
            "output": f"Cancelled cowork run {result['run']['id']}.",
            "result": result["run"],
        }

    if sub == "report":
        # `cowork report [<run_id>]` — render the typed StageReport
        # for a run. With no id, picks the active run; falls back to
        # the most recent run if none active. See docs/design/
        # cowork-mode.md and `app/services/reports.py`.
        from app.services import reports
        rest = parts[2].strip() if len(parts) >= 3 else ""
        if rest:
            run = local_memory.get_cowork_run(rest)
            if run is None:
                return {
                    "action": "cowork",
                    "session_id": session_id,
                    "output": f"No cowork run with id {rest!r}.",
                    "reason": ER.UNKNOWN_TARGET,
                }
        else:
            run = local_memory.active_cowork_run(session_id)
            if run is None:
                # Fall back to the most recent run for this session
                # so `cowork report` is useful right after a run
                # ends and is no longer "active".
                runs = local_memory.list_cowork_runs(session_id=session_id)
                if not runs:
                    return {
                        "action": "cowork",
                        "session_id": session_id,
                        "output": "No cowork runs in this session yet.",
                        "reason": ER.INVALID_ARGUMENT,
                    }
                run = runs[-1]
        report = reports.cowork_report_from_run(run)
        return {
            "action": "cowork",
            "session_id": session_id,
            "output": reports.format_report(report),
            "result": report.to_dict(),
        }

    if sub == "run":
        rest = parts[2].strip() if len(parts) >= 3 else ""
        if not rest:
            return {
                "action": "cowork",
                "session_id": session_id,
                "output": "Usage: cowork run <run-id> [--workers N]",
                "reason": ER.INVALID_ARGUMENT,
            }
        # Parse optional `--workers <n>` flag.
        max_workers = cowork.DEFAULT_MAX_WORKERS
        if "--workers" in rest:
            head, _flag, tail = rest.partition("--workers")
            tail = tail.strip()
            if not tail:
                return {
                    "action": "cowork",
                    "session_id": session_id,
                    "output": "Usage: cowork run <run-id> --workers N",
                    "reason": ER.INVALID_ARGUMENT,
                }
            tokens = tail.split()
            try:
                max_workers = int(tokens[0])
                if max_workers < 1:
                    raise ValueError
            except ValueError:
                return {
                    "action": "cowork",
                    "session_id": session_id,
                    "output": f"Invalid --workers value: {tokens[0]!r}",
                    "reason": ER.INVALID_ARGUMENT,
                }
            rest = head.strip()
        run_id = rest
        result = cowork.execute_run(run_id, max_workers=max_workers)
        if isinstance(result, dict) and result.get("ok") is False:
            return {
                "action": "cowork",
                "session_id": session_id,
                "output": result.get("output", "Could not execute run."),
                "reason": result.get("reason", ER.INVALID_ARGUMENT),
            }
        # Success: result IS the final run dict
        return {
            "action": "cowork",
            "session_id": session_id,
            "output": _format_cowork_run(result),
            "result": result,
        }

    # `cowork "<goal>"` and `cowork plan "<goal>"` are aliases in Phase 1.
    if sub == "plan":
        goal = parts[2].strip().strip('"').strip("'") if len(parts) >= 3 else ""
    else:
        # Treat the entire `parts[1] + parts[2]` as the goal text.
        rest_index = text.find(" ")
        goal = text[rest_index + 1:].strip().strip('"').strip("'") if rest_index >= 0 else ""

    if not goal:
        return {
            "action": "cowork",
            "session_id": session_id,
            "output": (
                "Usage:\n"
                "  cowork \"<goal>\"          (plan a goal; same as `cowork plan`)\n"
                "  cowork plan \"<goal>\"     (plan only; pause for approval)\n"
                "  cowork run <id>           (execute an approved plan)\n"
                "  cowork status             (list current + past runs)\n"
                "  cowork cancel             (abort the active run)"
            ),
            "reason": ER.INVALID_ARGUMENT,
        }

    result = cowork.plan_run(session_id=session_id, goal=goal)
    if not result.get("ok"):
        return {
            "action": "cowork",
            "session_id": session_id,
            "output": result.get("output", "Could not plan."),
            "reason": result.get("reason", ER.INVALID_PLAN_JSON),
        }

    run = result["run"]
    plan_summary = "\n".join(
        f"  {sub['id']:<6}  {sub['builder']:<10}  {sub['task'][:70]}"
        for sub in run["plan"]
    )
    return {
        "action": "cowork",
        "session_id": session_id,
        "output": (
            f"[approval required] cowork plan {run['id']}\n"
            f"Goal: {run['goal']}\n"
            f"Plan ({len(run['plan'])} sub-tasks):\n{plan_summary}\n"
            f"Type `approve` to start the run, or `deny` to cancel."
        ),
        "result": run,
        "pending_action": result["pending_action"],
        "reason": ER.APPROVAL_REQUIRED,
    }


def _handle_rules_command(text, session_id):
    """`rules` (list) | `rules show <id>` | `rules reload` | `rules disable|enable <id>`."""
    from app.services import rule_service as rs

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "list"

    if sub == "reload":
        new_registry = rs.reload_rules()
        return {
            "action": "rules",
            "session_id": session_id,
            "output": (
                f"Reloaded {len(new_registry)} rule(s)."
                if new_registry
                else "Reloaded — no rules found in rules/ directory."
            ),
            "result": {"count": len(new_registry), "ids": sorted(new_registry.keys())},
        }

    if sub in {"", "list", "status"}:
        rules = rs.list_rules()
        disabled = set(rs.list_disabled())
        if not rules:
            return {
                "action": "rules",
                "session_id": session_id,
                "output": (
                    "No rules registered. Drop a *.toml or *.json file "
                    "into the project's rules/ directory and run `rules reload`."
                ),
            }
        lines = [f"Rules ({len(rules)} registered, {len(disabled)} disabled):"]
        for rule in rules:
            sym = "✗" if rule.id in disabled else "✓"
            actions = ", ".join(sorted(rule.then.keys()))
            desc = rule.description or "(no description)"
            if len(desc) > 60:
                desc = desc[:57] + "..."
            lines.append(f"  {sym} {rule.id:<24}  [{actions}]  {desc}")
        return {
            "action": "rules",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": [
                {"id": r.id, "disabled": r.id in disabled, "actions": sorted(r.then.keys())}
                for r in rules
            ],
        }

    if sub == "show":
        rule_id = parts[2].strip() if len(parts) >= 3 else ""
        if not rule_id:
            return {
                "action": "rules",
                "session_id": session_id,
                "output": "Usage: rules show <id>",
                "reason": ER.INVALID_ARGUMENT,
            }
        rule = rs.get_rule(rule_id)
        if rule is None:
            return {
                "action": "rules",
                "session_id": session_id,
                "output": f"Unknown rule {rule_id!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        lines = [
            f"Rule: {rule.id}",
            f"Description: {rule.description or '(none)'}",
            f"Disabled: {rs.is_disabled(rule.id)}",
            "When:",
        ]
        for key, value in sorted(rule.when.items()):
            lines.append(f"  {key}: {value!r}")
        lines.append("Then:")
        for key, value in sorted(rule.then.items()):
            lines.append(f"  {key}: {value!r}")
        if rule.source_path:
            lines.append(f"Source: {rule.source_path}")
        return {
            "action": "rules",
            "session_id": session_id,
            "output": "\n".join(lines),
        }

    if sub in {"disable", "enable"}:
        rule_id = parts[2].strip() if len(parts) >= 3 else ""
        if not rule_id:
            return {
                "action": "rules",
                "session_id": session_id,
                "output": f"Usage: rules {sub} <id>",
                "reason": ER.INVALID_ARGUMENT,
            }
        if rs.get_rule(rule_id) is None:
            return {
                "action": "rules",
                "session_id": session_id,
                "output": f"Unknown rule {rule_id!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        changed = rs.disable_rule(rule_id) if sub == "disable" else rs.enable_rule(rule_id)
        verb = "Disabled" if sub == "disable" else "Enabled"
        if not changed:
            verb = "Already " + verb.lower()
        return {
            "action": "rules",
            "session_id": session_id,
            "output": f"{verb} rule {rule_id!r}.",
            "result": {"id": rule_id, "disabled": rs.is_disabled(rule_id)},
        }

    return {
        "action": "rules",
        "session_id": session_id,
        "output": (
            "Usage: rules [list] | rules show <id> | rules reload | "
            "rules disable <id> | rules enable <id>"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_provider_command(text, session_id):
    """`provider list / show / bind <lane> <provider:model> / unbind <lane>`.

    See `docs/design/chat-providers.md`. Default routing stays on
    Ollama; cloud lanes are opt-in per session.
    """
    from app.services import chat_providers as cp

    parts = text.split(maxsplit=3)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "show"

    if sub == "list":
        providers = cp.list_providers()
        lines = [f"Providers ({len(providers)} registered):"]
        for info in providers:
            cred = "ok" if cp.has_credentials(info.id) else "no creds"
            local = " (local)" if info.is_local else ""
            lines.append(f"  {info.id:<10}  [{cred:<8}]{local}  {info.description}")
        if not any(cp.has_credentials(p.id) for p in providers if not p.is_local):
            lines.append("")
            lines.append(
                "Tip: set OPENAI_API_KEY or ANTHROPIC_API_KEY in your env to "
                "enable a cloud provider, then `provider bind <lane> <id>:<model>`."
            )
        return {
            "action": "provider",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": [
                {
                    "id": p.id,
                    "is_local": p.is_local,
                    "has_credentials": cp.has_credentials(p.id),
                }
                for p in providers
            ],
        }

    if sub in {"", "show", "status"}:
        bindings = local_memory.get_lane_providers(session_id=session_id)
        if not bindings:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": (
                    "No lane bindings on this session — every lane goes to "
                    "Ollama. Use `provider bind <lane> <provider:model>` to "
                    "opt into a cloud lane."
                ),
                "result": {},
            }
        lines = ["Lane bindings on this session:"]
        for lane in sorted(bindings):
            lines.append(f"  {lane:<12}  {bindings[lane]}")
        return {
            "action": "provider",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": bindings,
        }

    if sub == "bind":
        if len(parts) < 4:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": "Usage: provider bind <lane> <provider:model>",
                "reason": ER.INVALID_ARGUMENT,
            }
        lane = parts[2].strip()
        spec = parts[3].strip().strip('"').strip("'")
        try:
            provider_id, model_id = cp.parse_lane_binding(spec)
        except ValueError as exc:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        if cp.get_provider(provider_id) is None:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": (
                    f"Unknown provider {provider_id!r}. "
                    f"Run `provider list` to see registered providers."
                ),
                "reason": ER.PROVIDER_UNKNOWN,
            }
        if not cp.has_credentials(provider_id):
            return {
                "action": "provider",
                "session_id": session_id,
                "output": (
                    f"Cannot bind {lane} to {provider_id}: no credentials in env. "
                    f"Set {cp.REGISTRY[provider_id].auth_env} and retry."
                ),
                "reason": ER.PROVIDER_UNAUTHENTICATED,
            }
        try:
            local_memory.set_lane_provider(session_id=session_id, lane=lane, binding=spec)
        except ValueError as exc:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "provider",
            "session_id": session_id,
            "output": f"Bound lane {lane!r} → {provider_id}:{model_id}.",
            "result": {"lane": lane, "binding": spec},
        }

    if sub == "unbind":
        lane = parts[2].strip() if len(parts) >= 3 else ""
        if not lane:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": "Usage: provider unbind <lane>",
                "reason": ER.INVALID_ARGUMENT,
            }
        removed = local_memory.clear_lane_provider(session_id=session_id, lane=lane)
        if not removed:
            return {
                "action": "provider",
                "session_id": session_id,
                "output": f"Lane {lane!r} had no binding.",
                "reason": ER.UNKNOWN_TARGET,
            }
        return {
            "action": "provider",
            "session_id": session_id,
            "output": f"Cleared lane {lane!r} (will fall back to Ollama).",
        }

    return {
        "action": "provider",
        "session_id": session_id,
        "output": (
            "Usage: provider list | provider show | "
            "provider bind <lane> <provider:model> | provider unbind <lane>"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_recall_command(text, session_id):
    """`recall <query> [--since 24h] [--session <id>] [--limit N]`.

    Searches chat history across one or all sessions; renders a
    citation list. See `docs/design/recall-memory.md`.
    """
    from app.services import recall_service as rc

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return {
            "action": "recall",
            "session_id": session_id,
            "output": (
                "Usage: recall <query> [--since 24h] [--session <id>] [--limit N]"
            ),
            "reason": ER.INVALID_ARGUMENT,
        }

    rest = parts[1].strip()

    # Parse trailing flags --since / --session / --limit. Anything left
    # over is the query text. Greedy: scan token-by-token from the end.
    tokens = rest.split()
    flags: Dict[str, str] = {}
    while len(tokens) >= 2 and tokens[-2].startswith("--"):
        flag = tokens[-2][2:]
        if flag not in {"since", "session", "limit"}:
            break
        flags[flag] = tokens[-1]
        tokens = tokens[:-2]
    query_text = " ".join(tokens).strip()
    if not query_text:
        return {
            "action": "recall",
            "session_id": session_id,
            "output": "recall <query> requires a non-empty query string",
            "reason": ER.INVALID_ARGUMENT,
        }

    # --since: convert relative-time spec to ISO timestamp.
    since_iso = None
    if "since" in flags:
        try:
            since_iso = rc.parse_relative_time(flags["since"])
        except rc.RecallTimeError as exc:
            return {
                "action": "recall",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }

    # --limit: bounded int.
    try:
        limit = int(flags.get("limit") or 10)
    except (TypeError, ValueError):
        return {
            "action": "recall",
            "session_id": session_id,
            "output": f"--limit must be an integer (got {flags.get('limit')!r})",
            "reason": ER.INVALID_ARGUMENT,
        }

    target_session = flags.get("session")  # None = search all sessions

    hits = rc.recall(
        query_text, since=since_iso, session=target_session, limit=limit,
    )

    return {
        "action": "recall",
        "session_id": session_id,
        "output": rc.format_hits(hits, query_text),
        "result": {
            "query": query_text,
            "since": since_iso,
            "session": target_session,
            "limit": limit,
            "hits": [
                {
                    "session_id": h.session_id,
                    "message_index": h.message_index,
                    "role": h.role,
                    "content": h.content,
                    "timestamp": h.timestamp,
                    "score": h.score,
                    "matched_terms": h.matched_terms,
                }
                for h in hits
            ],
        },
    }


def _handle_backlog_command(text, session_id):
    """`backlog list / sources / pull <source> [k=v ...]`.

    Backlog adapters pull tasks from external sources (JSON files,
    GitHub Issues, etc.) into a planner's inbox. See
    `docs/design/extensibility.md` §8.
    """
    from app.services import backlog_service as bl

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "info"

    if sub in {"info", "", "status", "list", "sources"}:
        sources = bl.list_sources()
        lines = [f"Backlog sources ({len(sources)} registered):"]
        for source in sources:
            lines.append(f"  {source.id:<16}  {source.description}")
        return {
            "action": "backlog",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": [{"id": s.id, "description": s.description} for s in sources],
        }

    if sub == "pull":
        rest = parts[2] if len(parts) >= 3 else ""
        tokens = rest.strip().split()
        if not tokens:
            return {
                "action": "backlog",
                "session_id": session_id,
                "output": "Usage: backlog pull <source-id> [k=v ...]",
                "reason": ER.INVALID_ARGUMENT,
            }
        source_id = tokens[0]
        # Parse `k=v` tokens into the source config.
        config = {}
        builder = None
        for kv in tokens[1:]:
            if "=" not in kv:
                return {
                    "action": "backlog",
                    "session_id": session_id,
                    "output": f"Bad config token {kv!r} — expected `name=value`.",
                    "reason": ER.INVALID_ARGUMENT,
                }
            name, _eq, value = kv.partition("=")
            value = value.strip().strip('"').strip("'")
            if name.strip() == "builder":
                builder = value
            else:
                config[name.strip()] = value

        if bl.get_source(source_id) is None:
            return {
                "action": "backlog",
                "session_id": session_id,
                "output": f"Unknown backlog source {source_id!r}. Run `backlog sources`.",
                "reason": ER.UNKNOWN_TARGET,
            }

        try:
            result = bl.pull_into_inbox(
                source_id, config,
                planner_session=session_id,
                builder=builder,
            )
        except (ValueError, FileNotFoundError) as exc:
            return {
                "action": "backlog",
                "session_id": session_id,
                "output": f"backlog pull failed: {exc}",
                "reason": ER.INVALID_ARGUMENT,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "action": "backlog",
                "session_id": session_id,
                "output": f"backlog pull failed: {exc}",
                "reason": ER.INTERNAL_ERROR,
            }

        lines = [
            f"backlog pull {source_id} · {result.requested} item(s) seen",
            f"  → {result.pulled} dispatched, {result.skipped_duplicate} duplicate, {result.skipped_failed} failed",
        ]
        if result.task_ids:
            lines.append(f"  task ids: {', '.join(result.task_ids)}")
        if result.errors:
            lines.append("  errors:")
            for err in result.errors[:5]:
                lines.append(f"    - {err}")
        event = {
            "action": "backlog",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": {
                "source_id": result.source_id,
                "requested": result.requested,
                "pulled": result.pulled,
                "skipped_duplicate": result.skipped_duplicate,
                "skipped_failed": result.skipped_failed,
                "task_ids": result.task_ids,
                "errors": result.errors,
            },
        }
        return event

    return {
        "action": "backlog",
        "session_id": session_id,
        "output": "Usage: backlog [list|sources] | backlog pull <source-id> [k=v ...]",
        "reason": ER.INVALID_ARGUMENT,
    }


def _publish_browser_event(event_name, session_id, **payload):
    """Best-effort publish of a `KIND_BROWSER` event."""
    try:
        from app.services.gateway.bus import GatewayEvent, KIND_BROWSER, default_bus

        default_bus().publish(
            GatewayEvent(
                kind=KIND_BROWSER,
                session_id=session_id,
                payload={"event": event_name, **payload},
            )
        )
    except Exception:  # noqa: BLE001
        pass


def _handle_browser_command(text, session_id):
    """`browser open|navigate|screenshot|get-text|get-html|close [<args>]`.

    Read-only Phase 1 surface. The first navigate to a new origin queues
    a ``kind: "browser_navigate"`` pending action; once approved, the
    origin is added to the session's whitelist and subsequent navigates
    on the same origin run immediately. See `browser-automation.md`.
    """
    from app.services import browser_service as bs

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "info"

    backend = bs.get_default_backend()

    if sub in {"info", "status", "", "help"}:
        approved = local_memory.get_approved_origins(session_id=session_id)
        snap = local_memory.get_browser_state(session_id=session_id).get("page_snapshot")
        lines = [
            f"Browser: backend={backend.name} (available={backend.available})",
            f"Approved origins ({len(approved)}): " + (", ".join(approved) if approved else "(none)"),
        ]
        if snap:
            lines.append(
                f"Last snapshot: {snap.get('url')!r} (title: {snap.get('title')!r})"
            )
        return {
            "action": "browser",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": {
                "backend": backend.name,
                "available": backend.available,
                "approved_origins": approved,
                "page_snapshot": snap,
            },
        }

    if sub == "list":
        sessions = backend.list_sessions()
        return {
            "action": "browser",
            "session_id": session_id,
            "output": (
                f"Active browser sessions: {', '.join(sessions)}"
                if sessions else "No active browser sessions."
            ),
            "result": sessions,
        }

    # Common URL-bearing commands: open / navigate.
    if sub in {"open", "navigate"}:
        url = parts[2].strip() if len(parts) >= 3 else ""
        if not url:
            return {
                "action": "browser",
                "session_id": session_id,
                "output": f"Usage: browser {sub} <url>",
                "reason": ER.INVALID_ARGUMENT,
            }
        if not backend.available:
            return {
                "action": "browser",
                "session_id": session_id,
                "output": (
                    "Browser backend unavailable. Install with: "
                    "`pip install playwright` then `playwright install chromium`."
                ),
                "reason": ER.PLAYWRIGHT_NOT_INSTALLED,
            }
        origin_host = bs.parse_origin(url)
        if not origin_host:
            return {
                "action": "browser",
                "session_id": session_id,
                "output": f"Invalid URL (no scheme/host): {url!r}",
                "reason": ER.INVALID_ARGUMENT,
            }
        approved = local_memory.get_approved_origins(session_id=session_id)
        if origin_host not in approved:
            # Queue the approval. The user reviews + types `approve`.
            pending_action = {
                "kind": "browser_navigate",
                "origin": "command",
                "url": url,
                "origin_host": origin_host,
                "browser_session_id": session_id,
                "reason": "first navigation to this origin needs approval",
            }
            local_memory.set_pending_approval(
                session_id=session_id, pending_approval=pending_action
            )
            return {
                "action": "browser",
                "session_id": session_id,
                "output": (
                    f"[approval required] browser {sub} {url}\n"
                    f"   (origin not yet approved for this session: {origin_host})\n"
                    f"Type `approve` to continue or `deny` to cancel."
                ),
                "pending_action": pending_action,
                "reason": ER.APPROVAL_REQUIRED,
            }
        # Origin already approved — run immediately.
        result = backend.navigate(session_id, url)
        return _format_browser_navigate_result(session_id, sub, url, result)

    # Inspect commands operate on the current session's active page.
    if sub == "screenshot":
        if not backend.available:
            return _browser_unavailable(session_id)
        selector = parts[2].strip() if len(parts) >= 3 else None
        result = backend.screenshot(session_id, selector=selector)
        return _format_browser_inspect_result(
            session_id, "screenshot", result,
            ok_template="Screenshot saved to {screenshot_path}",
        )

    if sub in {"get-text", "text"}:
        if not backend.available:
            return _browser_unavailable(session_id)
        selector = parts[2].strip() if len(parts) >= 3 else None
        result = backend.get_text(session_id, selector=selector)
        return _format_browser_inspect_result(session_id, "get_text", result)

    if sub in {"get-html", "html"}:
        if not backend.available:
            return _browser_unavailable(session_id)
        selector = parts[2].strip() if len(parts) >= 3 else None
        result = backend.get_html(session_id, selector=selector)
        return _format_browser_inspect_result(session_id, "get_html", result)

    if sub == "close":
        if not backend.available:
            return _browser_unavailable(session_id)
        result = backend.close(session_id)
        local_memory.set_browser_active_session(session_id=session_id, browser_session_id=None)
        local_memory.record_page_snapshot(session_id=session_id, snapshot=None)
        _publish_browser_event("close", session_id)
        return {
            "action": "browser",
            "session_id": session_id,
            "output": (
                f"Closed browser session ({'had a page' if result.get('had_page') else 'was empty'})."
            ),
            "result": result,
        }

    return {
        "action": "browser",
        "session_id": session_id,
        "output": (
            "Usage: browser [info] | browser open <url> | browser navigate <url> | "
            "browser screenshot [<selector>] | browser get-text [<selector>] | "
            "browser get-html [<selector>] | browser close | browser list"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _browser_unavailable(session_id):
    return {
        "action": "browser",
        "session_id": session_id,
        "output": (
            "Browser backend unavailable. Install with: "
            "`pip install playwright` then `playwright install chromium`."
        ),
        "reason": ER.PLAYWRIGHT_NOT_INSTALLED,
    }


def _format_browser_navigate_result(session_id, sub, url, result):
    """Common formatter for `open` / `navigate` happy paths."""
    if not result.get("ok"):
        return {
            "action": "browser",
            "session_id": session_id,
            "output": f"Could not {sub} {url}: {result.get('error', 'unknown error')}",
            "reason": ER.INTERNAL_ERROR,
            "result": result,
        }
    # Update session state + emit event.
    local_memory.set_browser_active_session(
        session_id=session_id, browser_session_id=session_id
    )
    local_memory.record_page_snapshot(
        session_id=session_id,
        snapshot={
            "url": result.get("url"),
            "title": result.get("title"),
            "screenshot_path": None,
            "text_excerpt": None,
        },
    )
    _publish_browser_event(
        "navigate",
        session_id,
        url=result.get("url"),
        title=result.get("title"),
    )
    return {
        "action": "browser",
        "session_id": session_id,
        "output": (
            f"Loaded {result.get('url')!r}\n"
            f"  title: {result.get('title')!r}"
        ),
        "result": result,
    }


def _format_browser_inspect_result(session_id, name, result, *, ok_template=None):
    """Common formatter for screenshot / get-text / get-html paths."""
    if not result.get("ok"):
        err = result.get("error", "unknown error")
        # Map common errors to canonical reasons.
        if err == "no_active_page":
            return {
                "action": "browser",
                "session_id": session_id,
                "output": "No active browser page. Use `browser open <url>` first.",
                "reason": ER.INVALID_ARGUMENT,
                "result": result,
            }
        return {
            "action": "browser",
            "session_id": session_id,
            "output": f"browser {name} failed: {err}",
            "reason": ER.INTERNAL_ERROR,
            "result": result,
        }
    # Build the output text.
    if ok_template:
        try:
            output = ok_template.format(**result)
        except KeyError:
            output = str(result.get("text") or result.get("html") or result)
    elif "text" in result:
        text = result.get("text") or ""
        if len(text) > 4000:
            text = text[:3997] + "..."
        output = text
    elif "html" in result:
        html = result.get("html") or ""
        if len(html) > 4000:
            html = html[:3997] + "..."
        output = html
    else:
        output = str(result)
    _publish_browser_event(name, session_id, url=result.get("url"))
    return {
        "action": "browser",
        "session_id": session_id,
        "output": output,
        "result": result,
    }


def _handle_mcp_command(text, session_id):
    """`mcp serve` (run stdio MCP server) | `mcp tools` (list tools) | `mcp config`.

    The MCP server is normally launched by the client (Claude Code,
    Cursor) via `python -m app.mcp`; this dispatcher command is for
    inspecting the tool surface from inside localaiproject.
    """
    parts = text.split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "info"

    if sub in {"info", "status", ""}:
        from app.mcp import tools as mcp_tools
        n_tools = len(mcp_tools.TOOLS)
        n_resources = len(mcp_tools.list_resource_descriptors())
        return {
            "action": "mcp",
            "session_id": session_id,
            "output": (
                f"MCP server: {n_tools} tools + {n_resources} resource(s) registered.\n"
                "Run as a subprocess: `python -m app.mcp` (stdio transport).\n"
                "Inspect: `mcp tools` (tool list) | `mcp config` (.mcp.json snippet)."
            ),
            "result": {"tools": n_tools, "resources": n_resources},
        }

    if sub == "tools":
        from app.mcp import tools as mcp_tools
        lines = [f"MCP tools ({len(mcp_tools.TOOLS)} registered):"]
        for tool in mcp_tools.TOOLS:
            desc = tool.description.replace("\n", " ").strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"  {tool.name:<22}  {desc}")
        return {
            "action": "mcp",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": [t.descriptor() for t in mcp_tools.TOOLS],
        }

    if sub == "config":
        # Emit a copy-paste .mcp.json snippet pointing at this checkout.
        cwd = os.getcwd()
        snippet = (
            '{\n'
            '  "mcpServers": {\n'
            '    "localaiproject": {\n'
            '      "command": "python",\n'
            '      "args": ["-m", "app.mcp"],\n'
            f'      "cwd": {repr(cwd)}\n'
            '    }\n'
            '  }\n'
            '}'
        )
        return {
            "action": "mcp",
            "session_id": session_id,
            "output": (
                "Add this to ~/.claude/.mcp.json (or your client's MCP config):\n\n"
                + snippet
            ),
        }

    if sub == "serve":
        # Run the stdio loop in-process. Blocks until stdin EOFs (the
        # spawning client closing the pipe). Mostly useful for ad-hoc
        # testing — production launches via `python -m app.mcp`.
        import sys
        from app.mcp import server as mcp_server
        try:
            mcp_server.serve(sys.stdin, sys.stdout)
            return {
                "action": "mcp",
                "session_id": session_id,
                "output": "MCP server stopped (stdin closed).",
            }
        except KeyboardInterrupt:
            return {
                "action": "mcp",
                "session_id": session_id,
                "output": "MCP server interrupted.",
                "reason": ER.CANCELLED,
            }

    return {
        "action": "mcp",
        "session_id": session_id,
        "output": "Usage: mcp [info] | mcp tools | mcp config | mcp serve",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_tags_command(text, session_id):
    """`tags` (show) | `tags add <tag>` | `tags remove <tag>` | `tags clear`.

    Tags are used by the agent-rules `session_tag` predicate.
    """
    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "show"

    if sub in {"", "show", "list"}:
        tags = local_memory.get_tags(session_id=session_id)
        if not tags:
            return {
                "action": "tags",
                "session_id": session_id,
                "output": "(no tags on this session)",
            }
        return {
            "action": "tags",
            "session_id": session_id,
            "output": "Tags: " + ", ".join(tags),
            "result": tags,
        }

    if sub == "add":
        tag = parts[2].strip() if len(parts) >= 3 else ""
        if not tag:
            return {
                "action": "tags",
                "session_id": session_id,
                "output": "Usage: tags add <tag>",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            tags = local_memory.add_tag(session_id=session_id, tag=tag)
        except ValueError as exc:
            return {
                "action": "tags",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "tags",
            "session_id": session_id,
            "output": "Tags: " + ", ".join(tags),
            "result": tags,
        }

    if sub == "remove":
        tag = parts[2].strip() if len(parts) >= 3 else ""
        if not tag:
            return {
                "action": "tags",
                "session_id": session_id,
                "output": "Usage: tags remove <tag>",
                "reason": ER.INVALID_ARGUMENT,
            }
        removed = local_memory.remove_tag(session_id=session_id, tag=tag)
        if not removed:
            return {
                "action": "tags",
                "session_id": session_id,
                "output": f"Tag {tag!r} was not on this session.",
                "reason": ER.UNKNOWN_TARGET,
            }
        return {
            "action": "tags",
            "session_id": session_id,
            "output": "Tags: " + (", ".join(local_memory.get_tags(session_id=session_id)) or "(none)"),
        }

    if sub == "clear":
        local_memory.set_tags(session_id=session_id, tags=[])
        return {
            "action": "tags",
            "session_id": session_id,
            "output": "Cleared all tags on this session.",
        }

    return {
        "action": "tags",
        "session_id": session_id,
        "output": "Usage: tags [show] | tags add <tag> | tags remove <tag> | tags clear",
        "reason": ER.INVALID_ARGUMENT,
    }


def _format_template_summary(summary):
    """Render a `run_template` summary for the dispatcher event output."""
    head = (
        f"template {summary['template_id']} · "
        f"{len(summary['steps'])} step(s) · stopped: {summary['stopped_reason']}"
    )
    lines = [head]
    for entry in summary["steps"]:
        event = entry.get("event") or {}
        sym = "✗" if event.get("reason") else "✓"
        if event.get("reason") == "approval_required":
            sym = "⏸"
        cmd_preview = entry["cmd"].replace("\n", " ").strip()
        if len(cmd_preview) > 80:
            cmd_preview = cmd_preview[:77] + "..."
        lines.append(f"  {sym} step {entry['index']}: {cmd_preview}")
    return "\n".join(lines)


def _parse_template_run_args(rest):
    """Parse `<template_id> [k=v ...]`. Returns (template_id, params, error)."""
    tokens = rest.strip().split()
    if not tokens:
        return None, {}, "Usage: template run <id> [k=v ...]"
    template_id = tokens[0]
    params = {}
    for kv in tokens[1:]:
        if "=" not in kv:
            return None, {}, f"Bad parameter {kv!r} — expected `name=value`."
        name, _eq, value = kv.partition("=")
        params[name.strip()] = value.strip().strip('"').strip("'")
    return template_id, params, None


def _handle_template_command(text, session_id):
    """`template list / show / run / reload`."""
    from app.services import template_service as ts

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "list"

    if sub == "reload":
        new_registry = ts.reload_templates()
        return {
            "action": "template",
            "session_id": session_id,
            "output": (
                f"Reloaded {len(new_registry)} template(s)."
                if new_registry
                else "Reloaded — no templates found in templates/ directory."
            ),
            "result": {"count": len(new_registry), "ids": sorted(new_registry.keys())},
        }

    if sub in {"", "list"}:
        templates = ts.list_templates()
        if not templates:
            return {
                "action": "template",
                "session_id": session_id,
                "output": (
                    "No templates registered. Drop a *.toml or *.json file "
                    "into the project's templates/ directory and run "
                    "`template reload`."
                ),
            }
        lines = [f"Templates ({len(templates)}):"]
        for tmpl in templates:
            desc = tmpl.description or "(no description)"
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(f"  {tmpl.id:<24}  {desc}")
        return {
            "action": "template",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": [{"id": t.id, "description": t.description} for t in templates],
        }

    if sub == "show":
        tmpl_id = parts[2].strip() if len(parts) >= 3 else ""
        if not tmpl_id:
            return {
                "action": "template",
                "session_id": session_id,
                "output": "Usage: template show <id>",
                "reason": ER.INVALID_ARGUMENT,
            }
        tmpl = ts.get_template(tmpl_id)
        if tmpl is None:
            return {
                "action": "template",
                "session_id": session_id,
                "output": f"Unknown template {tmpl_id!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        lines = [
            f"Template: {tmpl.id}",
            f"Description: {tmpl.description or '(none)'}",
        ]
        if tmpl.params:
            lines.append("Params:")
            for p in tmpl.params:
                req = " (required)" if p.required else ""
                default = f" [default={p.default!r}]" if p.default is not None else ""
                lines.append(f"  - {p.name}{req}{default}: {p.description or ''}")
        lines.append(f"Steps ({len(tmpl.steps)}):")
        for index, step in enumerate(tmpl.steps):
            require = "  (require_success)" if step.require_success else ""
            cmd_preview = step.cmd.replace("\n", " ").strip()
            if len(cmd_preview) > 100:
                cmd_preview = cmd_preview[:97] + "..."
            lines.append(f"  {index}. [{step.kind}]{require}  {cmd_preview}")
        if tmpl.source_path:
            lines.append(f"Source: {tmpl.source_path}")
        return {
            "action": "template",
            "session_id": session_id,
            "output": "\n".join(lines),
        }

    if sub == "run":
        rest = parts[2] if len(parts) >= 3 else ""
        tmpl_id, params, error = _parse_template_run_args(rest)
        if error:
            return {
                "action": "template",
                "session_id": session_id,
                "output": error,
                "reason": ER.INVALID_ARGUMENT,
            }
        tmpl = ts.get_template(tmpl_id)
        if tmpl is None:
            return {
                "action": "template",
                "session_id": session_id,
                "output": f"Unknown template {tmpl_id!r}. Run `template list` to see available.",
                "reason": ER.UNKNOWN_TARGET,
            }
        try:
            summary = ts.run_template(tmpl, params, session_id=session_id)
        except ts.TemplateError as exc:
            return {
                "action": "template",
                "session_id": session_id,
                "output": str(exc),
                "reason": exc.reason,
            }
        event = {
            "action": "template",
            "session_id": session_id,
            "output": _format_template_summary(summary),
            "result": summary,
        }
        if summary["stopped_reason"] == "paused":
            event["reason"] = ER.APPROVAL_REQUIRED
        elif summary["stopped_reason"] == "step_failed":
            event["reason"] = ER.INTERNAL_ERROR
        return event

    return {
        "action": "template",
        "session_id": session_id,
        "output": (
            "Usage: template list | template show <id> | template run <id> [k=v ...] "
            "| template reload"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_note_command(text, session_id):
    """`note list | note show [<label>] | note set <label> <text> |
    note describe <label> <text> | note budget <label> <bytes> |
    note clear <label> | note remove <label>`.

    Phase 2 structured memory blocks (Letta-inspired). Each block is a
    labelled, length-bounded slot the model sees as a separate system
    message. The legacy ``notes`` command still works — it operates on
    the ``working_memory`` block.
    """
    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "list"

    if sub in {"", "list"}:
        blocks = local_memory.get_memory_blocks(session_id=session_id)
        sorted_items = sorted(
            blocks.items(),
            key=lambda kv: (kv[1].get("priority", 100), kv[0]),
        )
        lines = [f"Memory blocks ({len(sorted_items)}):"]
        for label, block in sorted_items:
            value = (block.get("value") or "")
            value_preview = value.replace("\n", " ").strip()
            if len(value_preview) > 60:
                value_preview = value_preview[:57] + "..."
            used = len(value.encode("utf-8"))
            limit = block.get("char_limit", 0)
            lines.append(
                f"  {label:<18}  "
                f"[{used:>5}/{limit:<5}]  "
                f"{value_preview or '(empty)'}"
            )
        return {
            "action": "note",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": blocks,
        }

    if sub == "show":
        label = parts[2].strip() if len(parts) >= 3 else ""
        if not label:
            # `note show` with no label = list everything (alias for list).
            return _handle_note_command("note list", session_id)
        block = local_memory.get_memory_block(session_id=session_id, label=label)
        if block is None:
            return {
                "action": "note",
                "session_id": session_id,
                "output": f"Unknown memory block {label!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        return {
            "action": "note",
            "session_id": session_id,
            "output": (
                f"Memory block: {label}\n"
                f"Description: {block.get('description') or '(none)'}\n"
                f"Char limit: {block.get('char_limit')}\n"
                f"Used:       {len((block.get('value') or '').encode('utf-8'))}\n\n"
                f"{block.get('value') or '(empty)'}"
            ),
            "result": block,
        }

    if sub == "set":
        rest = parts[2] if len(parts) >= 3 else ""
        label_split = rest.split(maxsplit=1)
        if len(label_split) < 2:
            return {
                "action": "note",
                "session_id": session_id,
                "output": "Usage: note set <label> <text>",
                "reason": ER.INVALID_ARGUMENT,
            }
        label, value = label_split[0].strip(), label_split[1]
        try:
            stored = local_memory.set_memory_block_value(
                session_id=session_id, label=label, value=value,
            )
        except ValueError as exc:
            return {
                "action": "note",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "note",
            "session_id": session_id,
            "output": f"Set {label!r} ({len(stored.encode('utf-8'))} bytes).",
        }

    if sub == "describe":
        rest = parts[2] if len(parts) >= 3 else ""
        label_split = rest.split(maxsplit=1)
        if len(label_split) < 2:
            return {
                "action": "note",
                "session_id": session_id,
                "output": "Usage: note describe <label> <description>",
                "reason": ER.INVALID_ARGUMENT,
            }
        label, description = label_split[0].strip(), label_split[1]
        try:
            local_memory.set_memory_block_description(
                session_id=session_id, label=label, description=description,
            )
        except ValueError as exc:
            return {
                "action": "note",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "note",
            "session_id": session_id,
            "output": f"Updated description for {label!r}.",
        }

    if sub == "budget":
        rest = parts[2] if len(parts) >= 3 else ""
        tokens = rest.split()
        if len(tokens) < 2:
            return {
                "action": "note",
                "session_id": session_id,
                "output": "Usage: note budget <label> <byte-limit>",
                "reason": ER.INVALID_ARGUMENT,
            }
        label = tokens[0].strip()
        try:
            cleaned = local_memory.set_memory_block_budget(
                session_id=session_id, label=label, char_limit=tokens[1],
            )
        except ValueError as exc:
            return {
                "action": "note",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "note",
            "session_id": session_id,
            "output": f"Budget for {label!r} set to {cleaned} bytes.",
        }

    if sub == "clear":
        label = parts[2].strip() if len(parts) >= 3 else ""
        if not label:
            return {
                "action": "note",
                "session_id": session_id,
                "output": "Usage: note clear <label>",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            local_memory.clear_memory_block(session_id=session_id, label=label)
        except ValueError as exc:
            return {
                "action": "note",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "note",
            "session_id": session_id,
            "output": f"Cleared block {label!r}.",
        }

    if sub == "remove":
        label = parts[2].strip() if len(parts) >= 3 else ""
        if not label:
            return {
                "action": "note",
                "session_id": session_id,
                "output": "Usage: note remove <label>",
                "reason": ER.INVALID_ARGUMENT,
            }
        removed = local_memory.remove_memory_block(
            session_id=session_id, label=label,
        )
        if removed:
            return {
                "action": "note",
                "session_id": session_id,
                "output": f"Removed user-defined block {label!r}.",
            }
        # `remove_memory_block` returns False for default blocks (which
        # it CLEARS rather than deletes — they get re-seeded on next
        # normalize) AND for missing labels.
        if label in local_memory.DEFAULT_MEMORY_BLOCKS:
            return {
                "action": "note",
                "session_id": session_id,
                "output": (
                    f"Block {label!r} is a default block; cleared its value "
                    f"instead (use `note clear` for the same effect)."
                ),
            }
        return {
            "action": "note",
            "session_id": session_id,
            "output": f"Block {label!r} not found.",
            "reason": ER.UNKNOWN_TARGET,
        }

    return {
        "action": "note",
        "session_id": session_id,
        "output": (
            "Usage: note list | note show [<label>] | "
            "note set <label> <text> | note describe <label> <desc> | "
            "note budget <label> <bytes> | note clear <label> | "
            "note remove <label>"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_notes_command(text, session_id):
    """`notes` (show) | `notes append <text>` | `notes clear`.

    Free-form per-session markdown scratchpad. Designed for the agent
    to leave itself notes between turns. See `notes-buffer.md`.
    """
    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "show"

    if sub in {"", "show"}:
        notes = local_memory.get_notes(session_id=session_id)
        if not notes:
            return {
                "action": "notes",
                "session_id": session_id,
                "output": "(notes buffer empty)",
            }
        return {
            "action": "notes",
            "session_id": session_id,
            "output": notes,
        }

    if sub == "clear":
        local_memory.clear_notes(session_id=session_id)
        return {
            "action": "notes",
            "session_id": session_id,
            "output": "Notes cleared.",
        }

    if sub == "append":
        body = parts[2] if len(parts) >= 3 else ""
        if not body.strip():
            return {
                "action": "notes",
                "session_id": session_id,
                "output": "Usage: notes append <text>",
                "reason": ER.INVALID_ARGUMENT,
            }
        local_memory.append_notes(session_id=session_id, text=body)
        size = len(local_memory.get_notes(session_id=session_id).encode("utf-8"))
        return {
            "action": "notes",
            "session_id": session_id,
            "output": f"Appended ({size} bytes total).",
        }

    if sub == "set":
        # Replace wholesale. Useful for templated overwrites.
        body = parts[2] if len(parts) >= 3 else ""
        local_memory.set_notes(session_id=session_id, notes=body)
        return {
            "action": "notes",
            "session_id": session_id,
            "output": f"Notes replaced ({len(body.encode('utf-8'))} bytes).",
        }

    return {
        "action": "notes",
        "session_id": session_id,
        "output": "Usage: notes [show] | notes append <text> | notes set <text> | notes clear",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_session_switch(text):
    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {
            "action": "session",
            "output": "Usage: session <name>",
            "reason": ER.INVALID_ARGUMENT,
        }

    session_id = parts[1].strip()
    local_memory.create_session(session_id)
    context = local_memory.get_session_context(session_id=session_id)
    lines = [f"Switched to session '{session_id}'."]
    if context.get("workspace_path"):
        lines.append(f"Workspace: {context['workspace_path']}")
    if context.get("active_skills"):
        lines.append(f"Skills: {', '.join(context['active_skills'])}")
    if context.get("pending_approval"):
        lines.append(f"Pending approval: {describe_pending_action(context['pending_approval'])}")
    if context.get("approved_search"):
        search = context["approved_search"]
        files = ", ".join(search.get("files", [])[:2]) or "none"
        lines.append(f"Last approved search: {search.get('query') or '(unknown query)'} -> {files}")
    return {
        "action": "session",
        "session_id": session_id,
        "output": "\n".join(lines),
    }


def _handle_prompt_command(text, session_id):
    """``prompt show | prompt limits | prompt set-budget <slot> <n> |
    prompt clear-budgets | prompt set-total <n>``

    Inspect + tune the layered prompt assembly (see
    ``docs/design/prompt-assembly.md``).

    - ``prompt show`` (default) — render the slot trace for the
      session's *next* chat turn (no message specified, so all slots
      are populated from the current session context).
    - ``prompt limits`` — show ``SLOT_BUDGET_DEFAULTS`` + per-session
      overrides + the active total cap.
    - ``prompt set-budget <slot> <n>`` — override one slot's
      char_budget for this session; ``n=0`` clears the override.
    - ``prompt clear-budgets`` — drop all per-slot overrides.
    - ``prompt set-total <n>`` — override the total-budget cap;
      ``n=0`` reverts to ``DEFAULT_TOTAL_BUDGET``.
    """
    from app.services import chat_service, prompt_assembly as pa

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "show"

    if sub == "show":
        # Build the current assembly with an empty user message —
        # we only need the trace.
        summary = local_memory.get_session_summary(session_id=session_id) or {}
        history = local_memory.get_history(session_id=session_id, limit=0)
        ctx = local_memory.get_session_context(session_id=session_id)
        _, trace = chat_service.build_runtime_assembly(
            summary, history, "", session_context=ctx, session_id=session_id,
        )
        if not trace:
            output = "No slots would render for this session."
        else:
            lines = ["Prompt assembly (next turn):"]
            for entry in trace:
                flags = []
                if entry.get("truncated"):
                    flags.append("TRUNCATED")
                if entry.get("evicted"):
                    flags.append("EVICTED")
                flag_str = f" [{','.join(flags)}]" if flags else ""
                budget = entry.get("char_budget") or 0
                budget_str = f"/{budget}" if budget else ""
                lines.append(
                    f"  {entry['name']:<22}  "
                    f"prio={entry['priority']:<3}  "
                    f"chars={entry['chars']}{budget_str}{flag_str}"
                )
            footer = pa.render_trace_footer(trace)
            if footer:
                lines.append("")
                lines.append(footer)
            output = "\n".join(lines)
        return {
            "action": "prompt",
            "session_id": session_id,
            "output": output,
            "result": trace,
        }

    if sub == "limits":
        defaults = pa.SLOT_BUDGET_DEFAULTS
        overrides = local_memory.get_prompt_budgets(session_id=session_id)
        total_override = local_memory.get_prompt_total_budget(session_id=session_id)
        active_total = total_override if total_override else pa.DEFAULT_TOTAL_BUDGET
        merged = sorted(set(defaults.keys()) | set(overrides.keys()))
        lines = [
            f"Total budget: {active_total} chars"
            + (" (default)" if not total_override else " (override)"),
            "",
            "Per-slot budgets (0 = unbounded):",
        ]
        for name in merged:
            d = defaults.get(name, 0)
            o = overrides.get(name)
            if o is not None:
                lines.append(f"  {name:<22}  default={d:<6}  override={o}")
            else:
                lines.append(f"  {name:<22}  default={d}")
        if not merged:
            lines.append("  (no slots configured)")
        return {
            "action": "prompt",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": {
                "defaults": dict(defaults),
                "overrides": overrides,
                "total_budget": active_total,
                "total_budget_override": total_override,
            },
        }

    if sub == "set-budget":
        rest = parts[2] if len(parts) >= 3 else ""
        rest_parts = rest.split()
        if len(rest_parts) < 2:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": "Usage: prompt set-budget <slot> <chars>  (chars=0 clears override)",
                "reason": ER.INVALID_ARGUMENT,
            }
        slot_name = rest_parts[0]
        try:
            budget = int(rest_parts[1])
        except ValueError:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": f"Budget must be an integer, got {rest_parts[1]!r}.",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            budgets = local_memory.set_prompt_budget(
                session_id=session_id, slot=slot_name, char_budget=budget,
            )
        except ValueError as exc:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        if budget <= 0:
            output = f"Cleared override for slot {slot_name!r}; using SLOT_BUDGET_DEFAULTS."
        else:
            output = f"Set slot {slot_name!r} budget to {budget} chars."
        return {
            "action": "prompt",
            "session_id": session_id,
            "output": output,
            "result": budgets,
        }

    if sub == "clear-budgets":
        budgets = local_memory.clear_prompt_budgets(session_id=session_id)
        return {
            "action": "prompt",
            "session_id": session_id,
            "output": "Cleared all per-slot budget overrides.",
            "result": budgets,
        }

    if sub == "set-total":
        rest = parts[2].strip() if len(parts) >= 3 else ""
        if not rest:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": "Usage: prompt set-total <chars>  (chars=0 reverts to default)",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            value = int(rest)
        except ValueError:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": f"Total budget must be an integer, got {rest!r}.",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            applied = local_memory.set_prompt_total_budget(
                session_id=session_id, total_budget=value,
            )
        except ValueError as exc:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        if applied is None:
            output = f"Total budget reverted to default ({pa.DEFAULT_TOTAL_BUDGET} chars)."
        else:
            output = f"Set total budget to {applied} chars."
        return {
            "action": "prompt",
            "session_id": session_id,
            "output": output,
            "result": applied,
        }

    if sub == "compact":
        # Phase 1 of compact-prompt.md — toggle the per-session
        # dense-rendering flag. ``prompt compact`` (no arg) reports
        # the current value; ``prompt compact on|off`` sets it.
        rest = parts[2].strip().lower() if len(parts) >= 3 else ""
        current = pa.is_compact_enabled(session_id=session_id)
        if not rest:
            output = (
                f"compact_format: {'on' if current else 'off'}.\n"
                "Use: prompt compact on|off"
            )
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": output,
                "result": {"compact_format": current},
            }
        if rest in {"on", "true", "1", "yes", "enable", "enabled"}:
            new_val = True
        elif rest in {"off", "false", "0", "no", "disable", "disabled"}:
            new_val = False
        else:
            return {
                "action": "prompt",
                "session_id": session_id,
                "output": (
                    f"Unknown value {rest!r}. Use: prompt compact on|off"
                ),
                "reason": ER.INVALID_ARGUMENT,
            }
        applied = pa.set_compact(session_id=session_id, enabled=new_val)
        return {
            "action": "prompt",
            "session_id": session_id,
            "output": f"compact_format set to {'on' if applied else 'off'}.",
            "result": {"compact_format": applied},
        }

    return {
        "action": "prompt",
        "session_id": session_id,
        "output": (
            f"Unknown subcommand {sub!r}. "
            "Use: prompt show | prompt limits | "
            "prompt set-budget <slot> <chars> | prompt clear-budgets | "
            "prompt set-total <chars> | prompt compact [on|off]"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


# ============================================================
# Custom instructions (custom-instructions.md Phase 1 + minimal 2)
# ============================================================

_INSTRUCTIONS_USAGE = (
    "Usage: instructions list | "
    "instructions add <kind> <body...> "
    "[--label X] [--id slug] [--mode plan|build] [--workspace PATH] "
    "[--tags a,b] [--lanes a,b] [--priority N] [--char-limit N] | "
    "instructions edit <id> "
    "[--body NEW] [--label X] [--kind K] [--priority N] [--char-limit N] "
    "[--mode plan|build] [--no-mode] [--workspace PATH] [--no-workspace] "
    "[--tags a,b] [--no-tags] [--lanes a,b] [--no-lanes] | "
    "instructions show <id> | "
    "instructions enable <id> | instructions disable <id> | "
    "instructions hush <id> [--turns N] | "
    "instructions remove <id> | instructions clear | "
    "instructions import-persona"
)


def _parse_instructions_add_args(rest):
    """Hand-rolled flag parser for ``instructions add``. Returns
    ``(kind, body, fields, error)`` where ``fields`` is the dict of
    parsed flag values to merge into ``add_instruction(**fields)``.
    ``error`` is a string explaining a parse failure (or None).

    Recognised flags:
      --label <slug>            display label
      --id <slug>               explicit id (else minted)
      --mode plan|build         applies_to.mode
      --workspace <path>        applies_to.workspace (verbatim)
      --tags a,b,c              applies_to.tags
      --lanes a,b,c             applies_to.lanes
      --priority <n>            within-slot order (0..100)
      --char-limit <n>          per-instruction body cap

    The first non-flag token is ``kind``; everything remaining
    after the flags-strip pass is the body. Quoted bodies are not
    supported (the dispatcher already strips a wrapping pair of
    matching quotes elsewhere — we leave whatever the user typed
    untouched here).
    """
    tokens = rest.split() if rest else []
    if not tokens:
        return None, None, {}, "missing kind + body"
    kind = tokens[0].strip().lower()
    fields = {"kind": kind}
    body_parts = []
    applies_to = {}
    i = 1
    flag_specs = {
        "--label": "label",
        "--id": "id",
        "--mode": ("applies_to", "mode"),
        "--workspace": ("applies_to", "workspace"),
        "--tags": ("applies_to", "tags"),
        "--lanes": ("applies_to", "lanes"),
        "--priority": "priority",
        "--char-limit": "char_limit",
    }
    while i < len(tokens):
        tok = tokens[i]
        spec = flag_specs.get(tok)
        if spec is None:
            body_parts.append(tok)
            i += 1
            continue
        if i + 1 >= len(tokens):
            return None, None, {}, f"flag {tok} requires a value"
        raw_val = tokens[i + 1]
        i += 2
        if isinstance(spec, tuple) and spec[0] == "applies_to":
            sub_key = spec[1]
            if sub_key in {"tags", "lanes"}:
                values = [
                    p.strip().lower() for p in raw_val.split(",") if p.strip()
                ]
                if values:
                    applies_to[sub_key] = values
            else:
                applies_to[sub_key] = raw_val.strip()
        elif spec == "label" or spec == "id":
            fields[spec] = raw_val.strip().lower()
        elif spec == "priority" or spec == "char_limit":
            try:
                fields[spec] = int(raw_val)
            except ValueError:
                return None, None, {}, f"flag {tok} expects an integer"
    if applies_to:
        fields["applies_to"] = applies_to
    body = " ".join(body_parts).strip()
    if not body:
        return None, None, {}, "missing body"
    fields["body"] = body
    return kind, body, fields, None


def _parse_instructions_edit_args(rest):
    """Hand-rolled flag parser for ``instructions edit``. Returns
    ``(instruction_id, changes, error)`` where ``changes`` is the
    dict ready to merge into ``update_instruction(...)`` after the
    caller deep-merges ``applies_to`` against the existing record.

    The first positional token is the instruction id.

    Recognised flags:
      --body NEW BODY           replaces body (consumes the rest of
                                the line up to the next flag)
      --label X                 replaces label
      --kind K                  replaces kind
      --priority N              replaces priority
      --char-limit N            replaces char_limit
      --mode plan|build         set/replace applies_to.mode
      --no-mode                 clear applies_to.mode
      --workspace PATH          set/replace applies_to.workspace
      --no-workspace            clear applies_to.workspace
      --tags a,b,c              replace applies_to.tags
      --no-tags                 clear applies_to.tags
      --lanes a,b,c             replace applies_to.lanes
      --no-lanes                clear applies_to.lanes

    Two special keys appear in ``changes``:

    * ``__applies_set`` — dict of ``applies_to`` keys to set/merge
    * ``__applies_clear`` — set of ``applies_to`` keys to remove

    The caller resolves these against the existing entry's
    ``applies_to`` to preserve unmentioned keys.
    """
    tokens = rest.split() if rest else []
    if not tokens:
        return None, None, "missing instruction id"
    instruction_id = tokens[0].strip().lower()
    if not instruction_id:
        return None, None, "missing instruction id"
    changes = {}
    applies_set = {}
    applies_clear = set()
    flag_specs_simple = {
        "--label": "label",
        "--kind": "kind",
        "--priority": ("int", "priority"),
        "--char-limit": ("int", "char_limit"),
    }
    no_value_flags = {
        "--no-mode": "mode",
        "--no-workspace": "workspace",
        "--no-tags": "tags",
        "--no-lanes": "lanes",
    }
    list_flags = {
        "--tags": "tags",
        "--lanes": "lanes",
    }
    str_flags = {
        "--mode": "mode",
        "--workspace": "workspace",
    }
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--body":
            # --body consumes everything up to the next flag-looking
            # token. Quoted bodies are not handled — write them
            # without quotes.
            j = i + 1
            body_parts = []
            while j < len(tokens) and not tokens[j].startswith("--"):
                body_parts.append(tokens[j])
                j += 1
            if not body_parts:
                return None, None, f"flag {tok} requires a value"
            changes["body"] = " ".join(body_parts)
            i = j
            continue
        if tok in no_value_flags:
            applies_clear.add(no_value_flags[tok])
            i += 1
            continue
        if tok in flag_specs_simple:
            spec = flag_specs_simple[tok]
            if i + 1 >= len(tokens):
                return None, None, f"flag {tok} requires a value"
            raw = tokens[i + 1]
            if isinstance(spec, tuple) and spec[0] == "int":
                try:
                    changes[spec[1]] = int(raw)
                except ValueError:
                    return None, None, f"flag {tok} expects an integer"
            else:
                changes[spec] = raw.strip().lower() if spec == "kind" else raw.strip()
            i += 2
            continue
        if tok in str_flags:
            if i + 1 >= len(tokens):
                return None, None, f"flag {tok} requires a value"
            applies_set[str_flags[tok]] = tokens[i + 1].strip()
            i += 2
            continue
        if tok in list_flags:
            if i + 1 >= len(tokens):
                return None, None, f"flag {tok} requires a value"
            values = [
                p.strip().lower() for p in tokens[i + 1].split(",") if p.strip()
            ]
            applies_set[list_flags[tok]] = values
            i += 2
            continue
        return None, None, f"unknown flag {tok}"
    if applies_set:
        changes["__applies_set"] = applies_set
    if applies_clear:
        changes["__applies_clear"] = applies_clear
    if not changes:
        return None, None, "no changes specified"
    return instruction_id, changes, None


_PERSONA_HEADER_KIND_MAP = {
    # heading text → instruction kind
    "tone": "style",
    "style": "style",
    "voice": "style",
    "format": "style",
    "formatting": "style",
    "constraint": "constraint",
    "constraints": "constraint",
    "rules": "constraint",
    "don't": "constraint",
    "dont": "constraint",
    "do not": "constraint",
    "background": "knowledge",
    "about": "knowledge",
    "about me": "knowledge",
    "about us": "knowledge",
    "about the project": "knowledge",
    "knowledge": "knowledge",
    "facts": "knowledge",
    "context": "knowledge",
    "persona": "persona",
    "role": "persona",
    "identity": "persona",
    "override": "override",
    "overrides": "override",
}


def _kind_from_persona_heading(heading_text):
    """Map a markdown heading to one of CUSTOM_INSTRUCTION_KINDS.
    Falls back to ``persona`` when nothing matches — the safe
    default since PERSONA.md is itself a persona document."""
    cleaned = heading_text.strip().lower().rstrip(":")
    cleaned = cleaned.lstrip("#").strip()
    if cleaned in _PERSONA_HEADER_KIND_MAP:
        return _PERSONA_HEADER_KIND_MAP[cleaned]
    # Try matching individual words in the heading.
    for word in cleaned.split():
        if word in _PERSONA_HEADER_KIND_MAP:
            return _PERSONA_HEADER_KIND_MAP[word]
    return "persona"


def _parse_persona_markdown(text):
    """Walk a PERSONA.md-shaped markdown blob and return a list of
    ``(kind, label, body)`` tuples — one per H1 / H2 section.

    The first H1 (if present) often labels the document
    (``# Persona``); when its body is empty (just sub-headings)
    we skip it. Sections with empty bodies are dropped entirely.
    """
    if not text:
        return []
    lines = text.splitlines()
    sections = []
    cur_label = None
    cur_kind = None
    cur_body = []

    def _flush():
        if cur_label is None:
            return
        body = "\n".join(cur_body).strip()
        if not body:
            return
        sections.append((cur_kind or "persona", cur_label, body))

    for raw_line in lines:
        stripped = raw_line.lstrip()
        if stripped.startswith("# ") or stripped.startswith("## "):
            _flush()
            heading_text = stripped.lstrip("#").strip()
            cur_kind = _kind_from_persona_heading(heading_text)
            cur_label = heading_text[:40].lower().strip()
            cur_body = []
            continue
        cur_body.append(raw_line)
    _flush()
    return sections


def _format_instruction_condition(applies_to):
    """Render ``applies_to`` as a short single-line summary for
    ``instructions list``. Returns ``"-"`` when no condition.

    Plain-ASCII output: dispatcher results render through Windows
    cp1252 consoles too, where em-dash / ellipsis raise
    UnicodeEncodeError.
    """
    if not isinstance(applies_to, dict) or not applies_to:
        return "-"
    parts = []
    if "mode" in applies_to:
        parts.append(f"mode={applies_to['mode']}")
    if "workspace" in applies_to:
        ws = applies_to["workspace"]
        # Truncate long paths so the table stays readable.
        if len(ws) > 24:
            ws = "..." + ws[-21:]
        parts.append(f"ws={ws}")
    if applies_to.get("tags"):
        parts.append("tags=" + ",".join(applies_to["tags"]))
    if applies_to.get("lanes"):
        parts.append("lanes=" + ",".join(applies_to["lanes"]))
    return " ".join(parts) if parts else "-"


def _handle_instructions_command(text, session_id):
    """``instructions list | add | show | enable | disable | remove | clear``

    Phase 1 + minimal Phase 2 of
    ``docs/design/custom-instructions.md``. Each instruction is a
    typed, conditional, addressable behavioural rule the model
    sees on every chat turn (filtered by its own ``applies_to``).

    See ``_INSTRUCTIONS_USAGE`` above for the flag list on
    ``instructions add``.
    """
    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "list"
    rest = parts[2].strip() if len(parts) >= 3 else ""

    if sub == "list":
        entries = local_memory.get_instructions(session_id=session_id)
        if not entries:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": "No instructions configured. Add one with `instructions add <kind> <body...>`.",
                "result": [],
            }
        lines = [
            f"{'ID':<11} {'KIND':<11} {'EN':<3} {'COND':<28} LABEL",
        ]
        for entry in entries:
            cond = _format_instruction_condition(entry.get("applies_to"))
            if len(cond) > 28:
                cond = cond[:25] + "…"
            lines.append(
                f"{entry['id']:<11} {entry['kind']:<11} "
                f"{'on' if entry.get('enabled', True) else 'off':<3} "
                f"{cond:<28} {entry.get('label') or ''}"
            )
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": entries,
        }

    if sub == "add":
        kind, body, fields, parse_err = _parse_instructions_add_args(rest)
        if parse_err:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"{parse_err}.\n{_INSTRUCTIONS_USAGE}",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            created = local_memory.add_instruction(
                session_id=session_id, **fields
            )
        except ValueError as exc:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": (
                f"Added: {created['id']} [{created['kind']}] "
                f"{created.get('label') or created['body'][:40]}"
            ),
            "result": created,
        }

    if sub in {"show", "view"}:
        if not rest:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"Usage: instructions show <id>",
                "reason": ER.INVALID_ARGUMENT,
            }
        entry = local_memory.get_instruction(
            session_id=session_id, instruction_id=rest,
        )
        if entry is None:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"No instruction with id {rest!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        cond = _format_instruction_condition(entry.get("applies_to"))
        lines = [
            f"id:        {entry['id']}",
            f"label:     {entry.get('label') or '(none)'}",
            f"kind:      {entry['kind']}",
            f"enabled:   {'on' if entry.get('enabled', True) else 'off'}",
            f"condition: {cond}",
            f"priority:  {entry.get('priority', 50)}",
            f"char_limit: {entry.get('char_limit', 2048)}",
            "",
            "body:",
            entry.get("body") or "",
        ]
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": entry,
        }

    if sub in {"enable", "disable"}:
        if not rest:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"Usage: instructions {sub} <id>",
                "reason": ER.INVALID_ARGUMENT,
            }
        new_val = (sub == "enable")
        applied = local_memory.set_instruction_enabled(
            session_id=session_id, instruction_id=rest, enabled=new_val,
        )
        if applied is None:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"No instruction with id {rest!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": (
                f"{rest}: {'enabled' if applied else 'disabled'}."
            ),
            "result": {"id": rest, "enabled": applied},
        }

    if sub in {"remove", "rm", "delete", "del"}:
        if not rest:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"Usage: instructions remove <id>",
                "reason": ER.INVALID_ARGUMENT,
            }
        ok = local_memory.remove_instruction(
            session_id=session_id, instruction_id=rest,
        )
        if not ok:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"No instruction with id {rest!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": f"Removed instruction {rest}.",
            "result": {"id": rest, "removed": True},
        }

    if sub == "edit":
        instruction_id, changes, err = _parse_instructions_edit_args(rest)
        if err:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"{err}.\n{_INSTRUCTIONS_USAGE}",
                "reason": ER.INVALID_ARGUMENT,
            }
        existing = local_memory.get_instruction(
            session_id=session_id, instruction_id=instruction_id,
        )
        if existing is None:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"No instruction with id {instruction_id!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        # Resolve applies_to merge: start with the current dict,
        # apply explicit clears, then explicit sets.
        merged_applies = dict(existing.get("applies_to") or {})
        for key in changes.pop("__applies_clear", set()):
            merged_applies.pop(key, None)
        for key, value in (changes.pop("__applies_set", {}) or {}).items():
            merged_applies[key] = value
        if merged_applies != (existing.get("applies_to") or {}):
            changes["applies_to"] = merged_applies
        try:
            updated = local_memory.update_instruction(
                session_id=session_id,
                instruction_id=instruction_id,
                **changes,
            )
        except (TypeError, ValueError) as exc:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": str(exc),
                "reason": ER.INVALID_ARGUMENT,
            }
        if updated is None:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": (
                    f"Edit rejected: the resulting instruction would "
                    "be invalid (kind/body/applies_to)."
                ),
                "reason": ER.INVALID_ARGUMENT,
            }
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": (
                f"Updated {updated['id']} [{updated['kind']}]: "
                f"{updated.get('label') or updated['body'][:40]}"
            ),
            "result": updated,
        }

    if sub == "hush":
        rest_parts = rest.split() if rest else []
        if not rest_parts:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": "Usage: instructions hush <id> [--turns N]",
                "reason": ER.INVALID_ARGUMENT,
            }
        instruction_id = rest_parts[0].strip().lower()
        turns = 1
        i = 1
        while i < len(rest_parts):
            tok = rest_parts[i]
            if tok == "--turns" and i + 1 < len(rest_parts):
                try:
                    turns = int(rest_parts[i + 1])
                except ValueError:
                    return {
                        "action": "instructions",
                        "session_id": session_id,
                        "output": "--turns expects an integer",
                        "reason": ER.INVALID_ARGUMENT,
                    }
                i += 2
                continue
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"unknown flag {tok!r}",
                "reason": ER.INVALID_ARGUMENT,
            }
        if turns < 0:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": "--turns must be >= 0",
                "reason": ER.INVALID_ARGUMENT,
            }
        applied = local_memory.hush_instruction(
            session_id=session_id, instruction_id=instruction_id, turns=turns,
        )
        if applied is None:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": f"No instruction with id {instruction_id!r}.",
                "reason": ER.UNKNOWN_TARGET,
            }
        if applied == 0:
            output = f"{instruction_id}: hush cleared (active again)."
        else:
            output = (
                f"{instruction_id}: hushed for {applied} more turn"
                f"{'s' if applied != 1 else ''}."
            )
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": output,
            "result": {"id": instruction_id, "hushed_remaining": applied},
        }

    if sub == "import-persona":
        ws = local_memory.get_session_context(
            session_id=session_id,
        ).get("workspace_path")
        if not ws:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": (
                    "No workspace pinned. Run `workspace <path>` "
                    "first; PERSONA.md is read from the workspace root."
                ),
                "reason": ER.NO_WORKSPACE,
            }
        from app.services import project_context_service
        text = project_context_service.read_persona(ws) or ""
        if not text.strip():
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": (
                    "PERSONA.md is empty or missing at the workspace "
                    "root. Nothing to import."
                ),
                "reason": ER.UNKNOWN_TARGET,
            }
        sections = _parse_persona_markdown(text)
        if not sections:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": (
                    "PERSONA.md has no '# Heading' or '## Heading' "
                    "sections. Add headings to import as separate "
                    "instructions."
                ),
                "reason": ER.UNKNOWN_TARGET,
            }
        added = []
        for kind, label, body in sections:
            try:
                created = local_memory.add_instruction(
                    session_id=session_id,
                    kind=kind,
                    body=body,
                    label=label,
                )
            except ValueError:
                continue
            added.append(created)
        if not added:
            return {
                "action": "instructions",
                "session_id": session_id,
                "output": (
                    "Found sections but none were importable "
                    "(empty bodies / invalid headers)."
                ),
                "reason": ER.INVALID_ARGUMENT,
            }
        lines = [f"Imported {len(added)} instruction(s) from PERSONA.md:"]
        for entry in added:
            lines.append(
                f"  {entry['id']} [{entry['kind']}] {entry.get('label') or ''}"
            )
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": added,
        }

    if sub == "clear":
        count = local_memory.clear_instructions(session_id=session_id)
        return {
            "action": "instructions",
            "session_id": session_id,
            "output": f"Cleared {count} instruction(s).",
            "result": {"removed": count},
        }

    return {
        "action": "instructions",
        "session_id": session_id,
        "output": f"Unknown subcommand {sub!r}.\n{_INSTRUCTIONS_USAGE}",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_proactive_command(text, session_id):
    """``proactive status | proactive tick | proactive reset |
    proactive prompt``

    Phase 3 of `docs/design/patch-taxonomy.md`. Inspect + drive the
    proactive trigger that periodically asks the model 'is there
    anything genuinely worth proposing right now?'

    - ``proactive status`` (default): show the heuristic state
      (last check / last proposal / null streak / recent action
      count + whether we'd fire right now).
    - ``proactive tick``: invoke the trigger now if the heuristic
      allows. Returns the proactive lane's response (null or a
      proposed dispatcher command).
    - ``proactive reset``: clear all proactive state. Useful when
      debugging or after a long idle.
    - ``proactive prompt``: show the system+user messages we'd
      send to the model on the next tick (debug aid).
    """
    from app.services import proactive_service as ps

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "status"
    rest = parts[2].strip() if len(parts) >= 3 else ""

    if sub == "status":
        state = ps.load_state(session_id)
        decision = ps.should_fire(state)
        lines = [
            f"Proactive trigger -- session={session_id}",
            f"  last_check_at:    {int(state.last_check_at)}",
            f"  last_proposal_at: {int(state.last_proposal_at)}",
            f"  null_streak:      {state.null_streak}/{ps.MAX_NULL_STREAK}",
            f"  recent_actions:   {len(state.recent_action_times)} stored",
            f"  would_fire_now:   {decision.ok}  ({decision.reason or 'ready'}: {decision.detail})",
        ]
        if state.last_response:
            preview = state.last_response.get("reason") or state.last_response.get("parse_error") or ""
            lines.append(f"  last_response:    is_null={state.last_response.get('is_null')}  reason={preview!r}")
        return {
            "action": "proactive",
            "session_id": session_id,
            "output": "\n".join(lines),
            "result": {
                "state": state.to_dict(),
                "would_fire": decision.ok,
                "refused_reason": decision.reason,
                "refused_detail": decision.detail,
            },
        }

    if sub == "tick":
        result = ps.tick(session_id=session_id)
        if not result.fired:
            return {
                "action": "proactive",
                "session_id": session_id,
                "output": (
                    f"Proactive tick refused: {result.refused_reason} "
                    f"({result.detail})"
                ),
                "result": {
                    "fired": False,
                    "refused_reason": result.refused_reason,
                    "detail": result.detail,
                },
            }
        response = result.response
        if response and response.is_null:
            return {
                "action": "proactive",
                "session_id": session_id,
                "output": (
                    f"Proactive tick: model returned null. "
                    f"Reason: {response.reason or response.parse_error or '(none)'}"
                ),
                "result": {
                    "fired": True,
                    "is_null": True,
                    "reason": response.reason,
                    "parse_error": response.parse_error,
                },
            }
        # Non-null proposal
        return {
            "action": "proactive",
            "session_id": session_id,
            "output": (
                f"Proactive tick: proposal emitted "
                f"(emitted={result.proposal_emitted}). Reason: {result.detail}"
            ),
            "result": {
                "fired": True,
                "is_null": False,
                "proposal_emitted": result.proposal_emitted,
                "patch": response.patch if response else None,
                "reason": response.reason if response else "",
            },
        }

    if sub == "reset":
        ps.reset_state(session_id=session_id)
        return {
            "action": "proactive",
            "session_id": session_id,
            "output": "Proactive state cleared (all timestamps + null_streak).",
        }

    if sub == "prompt":
        messages = ps.build_proactive_prompt(session_id=session_id)
        rendered = "\n\n".join(
            f"--- {m['role']} ---\n{m['content']}" for m in messages
        )
        return {
            "action": "proactive",
            "session_id": session_id,
            "output": rendered,
            "result": {"messages": messages},
        }

    if sub == "autofire":
        # ``proactive autofire`` (no arg) reports current state.
        # ``proactive autofire on|off`` toggles. Phase 3b.
        if not rest:
            return {
                "action": "proactive",
                "session_id": session_id,
                "output": (
                    f"Auto-fire is {'ON' if ps.AUTOFIRE_ENABLED else 'OFF'}. "
                    f"Use `proactive autofire on` or `proactive autofire off`."
                ),
                "result": {"enabled": ps.AUTOFIRE_ENABLED},
            }
        flag = rest.strip().lower()
        if flag in {"on", "true", "1", "yes"}:
            ps.set_autofire(True)
            return {
                "action": "proactive",
                "session_id": session_id,
                "output": (
                    "Auto-fire enabled. Every user command now invokes "
                    "the proactive heuristic; ticks fire in a daemon "
                    "thread when the heuristic agrees."
                ),
                "result": {"enabled": True},
            }
        if flag in {"off", "false", "0", "no"}:
            ps.set_autofire(False)
            return {
                "action": "proactive",
                "session_id": session_id,
                "output": (
                    "Auto-fire disabled. Use `proactive tick` to "
                    "manually invoke the heuristic."
                ),
                "result": {"enabled": False},
            }
        return {
            "action": "proactive",
            "session_id": session_id,
            "output": (
                f"Unknown auto-fire flag {flag!r}. Use 'on' or 'off'."
            ),
            "reason": ER.INVALID_ARGUMENT,
        }

    return {
        "action": "proactive",
        "session_id": session_id,
        "output": (
            f"Unknown subcommand {sub!r}. "
            "Use: proactive status | proactive tick | "
            "proactive reset | proactive prompt | "
            "proactive autofire [on|off]"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_retrieve_command(text, session_id):
    """``retrieve <query> | retrieve build | retrieve status |
    retrieve clear``

    BM25 retrieval over the pinned workspace's text files. Phase
    2a of ``docs/design/recall-memory.md``. The index is built
    lazily on first ``retrieve <query>`` call and cached in-memory
    per session; ``retrieve build`` forces a rebuild;
    ``retrieve clear`` drops the cache so the next query rebuilds.
    """
    from app.services import retrieval_index as ri

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        # `retrieve` (no args) shows status — useful first command.
        return _retrieve_status(session_id)
    rest = parts[1].strip()

    # Sub-command dispatch — ``retrieve <verb>`` recognises a small
    # set of typed verbs; anything else is treated as a free-form
    # query.
    head = rest.split(maxsplit=1)[0].lower() if rest else ""

    if head == "status":
        return _retrieve_status(session_id)

    if head == "build":
        workspace_path = local_memory.get_session_context(session_id).get("workspace_path")
        if not workspace_path:
            return {
                "action": "retrieve",
                "session_id": session_id,
                "output": (
                    "No workspace pinned. Use `workspace <path>` first; "
                    "the BM25 index is per-workspace."
                ),
                "reason": ER.NO_WORKSPACE,
            }
        _, diag = ri.rebuild(session_id, workspace_path=workspace_path)
        return {
            "action": "retrieve",
            "session_id": session_id,
            "output": (
                f"Index rebuilt: {diag.get('doc_count', 0)} documents "
                f"({diag.get('vocab_size', 0)} unique tokens). "
                f"Indexed {diag.get('indexed', 0)} files; skipped "
                f"{diag.get('skipped_unknown_ext', 0)} unknown-ext, "
                f"{diag.get('skipped_oversize', 0)} oversize, "
                f"{diag.get('skipped_binary', 0)} binary."
            ),
            "result": diag,
        }

    if head == "clear":
        cleared = ri.clear(session_id)
        return {
            "action": "retrieve",
            "session_id": session_id,
            "output": (
                "Cleared the cached BM25 index."
                if cleared else "No cached index to clear."
            ),
        }

    # Free-form query.
    workspace_path = local_memory.get_session_context(session_id).get("workspace_path")
    if not workspace_path:
        return {
            "action": "retrieve",
            "session_id": session_id,
            "output": (
                "No workspace pinned. Use `workspace <path>` first; "
                "the BM25 index is per-workspace."
            ),
            "reason": ER.NO_WORKSPACE,
        }
    hits, diag = ri.search(
        session_id, query=rest, limit=10, workspace_path=workspace_path,
    )
    return {
        "action": "retrieve",
        "session_id": session_id,
        "output": ri.format_hits(hits),
        "result": {
            "query": rest,
            "hits": [
                {"path": h.path, "score": h.score,
                 "matched_terms": h.matched_terms, "snippet": h.snippet}
                for h in hits
            ],
            "stats": diag,
        },
    }


def _handle_tool_command(text, session_id):
    """``tool show | tool run <n> | tool parse <text>``

    Phase 1 of `docs/design/tool-calling.md`. Surfaces the
    ```dispatcher fenced blocks the model emits in chat
    responses; lets the user explicitly run individual calls.
    Phase 2 adds a system-prompt slot teaching the model the
    format; Phase 3 routes calls through the patch-taxonomy
    approval gate.

    - ``tool show`` (default): parse the most recent assistant
      turn for tool calls; print the list with 1-based indices.
    - ``tool run <n>``: dispatch the Nth tool call from the most
      recent assistant turn. Same effect as the user typing the
      command directly — but explicit ``tool run`` annotates the
      execution record so the audit trail shows which model
      response originated the action.
    - ``tool parse <text>``: parse arbitrary text for tool calls
      (debug helper; useful for testing parser edge cases).
    """
    from app.services import tool_calls

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "show"
    rest = parts[2].strip() if len(parts) >= 3 else ""

    if sub == "show":
        calls = tool_calls.parse_from_last_assistant(session_id=session_id)
        return {
            "action": "tool",
            "session_id": session_id,
            "output": tool_calls.format_tool_calls(calls),
            "result": {"calls": [c.to_dict() for c in calls]},
        }

    if sub == "run":
        if not rest:
            return {
                "action": "tool",
                "session_id": session_id,
                "output": "Usage: tool run <n> (1-based index from `tool show`)",
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            idx = int(rest)
        except ValueError:
            return {
                "action": "tool",
                "session_id": session_id,
                "output": f"Tool index must be an integer, got {rest!r}.",
                "reason": ER.INVALID_ARGUMENT,
            }
        calls = tool_calls.parse_from_last_assistant(session_id=session_id)
        if not calls:
            return {
                "action": "tool",
                "session_id": session_id,
                "output": "No tool calls in the most recent assistant turn.",
                "reason": ER.INVALID_ARGUMENT,
            }
        if idx < 1 or idx > len(calls):
            return {
                "action": "tool",
                "session_id": session_id,
                "output": (
                    f"Tool index {idx} out of range "
                    f"(have {len(calls)} call(s); use 1..{len(calls)})."
                ),
                "reason": ER.INVALID_ARGUMENT,
            }
        chosen = calls[idx - 1]
        # Recursive dispatch — runs the model-suggested command
        # through the same `dispatch_input` the user uses. The
        # dispatcher's existing approval gate (workspace writes,
        # mode checks, rule predicates, etc.) applies as if the
        # user had typed the command themselves.
        result = dispatch_input(chosen.command, session_id=session_id)
        # Decorate the result so consumers see this came from a
        # tool call, not direct user input.
        result.setdefault("origin", "tool_run")
        result.setdefault("tool_call_index", idx)
        result.setdefault("tool_call_command", chosen.command)
        return result

    if sub == "parse":
        if not rest:
            return {
                "action": "tool",
                "session_id": session_id,
                "output": (
                    "Usage: tool parse <text>  (debug helper — "
                    "scan arbitrary text for ```dispatcher fenced blocks)"
                ),
                "reason": ER.INVALID_ARGUMENT,
            }
        calls = tool_calls.parse_tool_calls(rest)
        return {
            "action": "tool",
            "session_id": session_id,
            "output": tool_calls.format_tool_calls(calls),
            "result": {"calls": [c.to_dict() for c in calls]},
        }

    if sub == "enable":
        # ``tool enable`` (no arg) reports state.
        # ``tool enable on|off`` toggles. Phase 2 of tool-calling.md.
        if not rest:
            enabled = tool_calls.is_enabled(session_id=session_id)
            return {
                "action": "tool",
                "session_id": session_id,
                "output": (
                    f"Tool calling is {'ON' if enabled else 'OFF'}. "
                    f"When ON, the model receives a system-prompt slot "
                    f"teaching it the ```dispatcher fence format. "
                    f"Use `tool enable on` or `tool enable off`."
                ),
                "result": {"enabled": enabled},
            }
        flag = rest.strip().lower()
        if flag in {"on", "true", "1", "yes"}:
            tool_calls.set_enabled(session_id=session_id, enabled=True)
            return {
                "action": "tool",
                "session_id": session_id,
                "output": (
                    "Tool calling enabled. The model now receives a "
                    "teaching message about the ```dispatcher format on "
                    "every chat turn. Suggestions appear via `tool show`; "
                    "run with `tool run <n>`."
                ),
                "result": {"enabled": True},
            }
        if flag in {"off", "false", "0", "no"}:
            tool_calls.set_enabled(session_id=session_id, enabled=False)
            return {
                "action": "tool",
                "session_id": session_id,
                "output": (
                    "Tool calling disabled. The teaching slot is dropped "
                    "from the prompt; the parser still works for `tool "
                    "show` / `tool parse` if you instruct the model "
                    "manually."
                ),
                "result": {"enabled": False},
            }
        return {
            "action": "tool",
            "session_id": session_id,
            "output": f"Unknown enable flag {flag!r}. Use 'on' or 'off'.",
            "reason": ER.INVALID_ARGUMENT,
        }

    return {
        "action": "tool",
        "session_id": session_id,
        "output": (
            f"Unknown subcommand {sub!r}. "
            "Use: tool show | tool run <n> | tool parse <text> | "
            "tool enable [on|off]"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _retrieve_status(session_id):
    from app.services import retrieval_index as ri
    info = ri.status(session_id)
    if not info["cached"]:
        output = (
            f"BM25 index not yet built for session {session_id}. "
            f"Use `retrieve <query>` to build lazily, or "
            f"`retrieve build` to force a build."
        )
    else:
        output = (
            f"BM25 index ready: {info['doc_count']} documents, "
            f"{info['vocab_size']} unique tokens, "
            f"avg doc length {info['avg_doc_length']:.1f} tokens."
        )
    return {
        "action": "retrieve",
        "session_id": session_id,
        "output": output,
        "result": info,
    }


def _handle_classifier_command(text, session_id):
    """``classifier train | classifier predict <text> | classifier
    status | classifier reset``

    Naive Bayes lane classifier, trained from this session's own
    execution history. Phase 1 — see ``app/services/lane_classifier.py``
    and ``docs/design/agent-rules.md``. Phase 2 (planned): blend
    these predictions with the rule-based router. Today the
    classifier is a parallel preview surface — running ``classifier
    predict <text>`` shows what the *learned* router would say
    without affecting the live route.
    """
    from app.services import lane_classifier as lc

    parts = text.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else "status"
    rest = parts[2].strip() if len(parts) >= 3 else ""

    if sub == "status":
        info = lc.model_status(session_id=session_id)
        if not info["trained"]:
            output = (
                f"Classifier not trained for session {session_id}. "
                f"Run `classifier train` after at least "
                f"{lc.MIN_SAMPLES_PER_CLASS} chat turns per lane."
            )
        else:
            ready = " (ready)" if info["ready_for_predictions"] else " (warming up)"
            label_lines = "\n".join(
                f"  {label:<12}  {count} samples"
                for label, count in sorted(info["label_doc_counts"].items())
            )
            output = (
                f"Classifier{ready}\n"
                f"  vocab_size: {info['vocab_size']} (cap {info['vocab_limit']})\n"
                f"  labels: {len(info['labels'])}\n"
                f"{label_lines}"
            )
        return {
            "action": "classifier",
            "session_id": session_id,
            "output": output,
            "result": info,
        }

    if sub == "train":
        diag = lc.train_from_executions(session_id=session_id)
        if not diag.get("ok"):
            return {
                "action": "classifier",
                "session_id": session_id,
                "output": (
                    f"Cannot train: {diag.get('reason', 'no_samples')}. "
                    f"({diag.get('samples', 0)} executions inspected; "
                    f"{diag.get('samples_discarded', 0)} discarded.)"
                ),
                "reason": ER.INVALID_ARGUMENT,
                "result": diag,
            }
        per_label = ", ".join(
            f"{l}: {c}" for l, c in sorted((diag.get("per_label") or {}).items())
        )
        return {
            "action": "classifier",
            "session_id": session_id,
            "output": (
                f"Trained on {diag['samples_used']} samples "
                f"({diag.get('samples_discarded', 0)} discarded). "
                f"Vocab: {diag['vocab_size']} tokens. "
                f"Per label: {per_label}"
            ),
            "result": diag,
        }

    if sub == "predict":
        if not rest:
            return {
                "action": "classifier",
                "session_id": session_id,
                "output": "Usage: classifier predict <text>",
                "reason": ER.INVALID_ARGUMENT,
            }
        lane, confidence = lc.predict_lane(rest, session_id=session_id)
        if lane is None:
            return {
                "action": "classifier",
                "session_id": session_id,
                "output": (
                    "Classifier has no prediction for this input. "
                    "(Either the model isn't trained, or the input "
                    "had no in-vocab tokens.)"
                ),
                "result": {"lane": None, "confidence": 0.0},
            }
        return {
            "action": "classifier",
            "session_id": session_id,
            "output": f"Predicted lane: {lane}  (confidence: {confidence:.3f})",
            "result": {"lane": lane, "confidence": confidence},
        }

    if sub == "reset":
        lc.reset_model(session_id=session_id)
        return {
            "action": "classifier",
            "session_id": session_id,
            "output": "Classifier model cleared. Run `classifier train` to rebuild.",
        }

    if sub == "autoroute":
        # ``classifier autoroute`` (no arg) reports state.
        # ``classifier autoroute on|off`` toggles. Phase 5b of
        # agent-rules.md.
        if not rest:
            enabled = lc.is_autoroute_enabled(session_id=session_id)
            return {
                "action": "classifier",
                "session_id": session_id,
                "output": (
                    f"Classifier autoroute is {'ON' if enabled else 'OFF'}. "
                    f"Use `classifier autoroute on` or `classifier autoroute off`."
                ),
                "result": {"enabled": enabled},
            }
        flag = rest.strip().lower()
        if flag in {"on", "true", "1", "yes"}:
            lc.set_autoroute(session_id=session_id, enabled=True)
            return {
                "action": "classifier",
                "session_id": session_id,
                "output": (
                    "Classifier autoroute enabled. Predictions will blend "
                    "into the rule-router's lane_biases on every chat turn."
                ),
                "result": {"enabled": True},
            }
        if flag in {"off", "false", "0", "no"}:
            lc.set_autoroute(session_id=session_id, enabled=False)
            return {
                "action": "classifier",
                "session_id": session_id,
                "output": (
                    "Classifier autoroute disabled. Predictions are "
                    "preview-only; live routing uses the rule-router alone."
                ),
                "result": {"enabled": False},
            }
        return {
            "action": "classifier",
            "session_id": session_id,
            "output": (
                f"Unknown autoroute flag {flag!r}. Use 'on' or 'off'."
            ),
            "reason": ER.INVALID_ARGUMENT,
        }

    return {
        "action": "classifier",
        "session_id": session_id,
        "output": (
            f"Unknown subcommand {sub!r}. "
            "Use: classifier status | classifier train | "
            "classifier predict <text> | classifier reset | "
            "classifier autoroute [on|off]"
        ),
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_workspace_command(text, session_id):
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return {
            "action": "workspace",
            "session_id": session_id,
            "output": render_workspace_status(local_memory.get_session_context(session_id).get("workspace_path")),
        }

    argument = parts[1].strip()
    if argument.lower() == "clear":
        local_memory.set_workspace(session_id=session_id, workspace_path=None)
        local_memory.clear_pending_approval(session_id=session_id)
        local_memory.clear_approved_search(session_id=session_id)
        return {
            "action": "workspace",
            "session_id": session_id,
            "output": "Cleared the pinned workspace for this session.",
        }

    # `workspace writes-bump <n>` raises the per-session write soft cap.
    lower_arg = argument.lower()
    if lower_arg == "writes-bump" or lower_arg.startswith("writes-bump "):
        raw_n = argument.split(maxsplit=1)
        if len(raw_n) != 2 or not raw_n[1].strip():
            current_cap = local_memory.get_write_cap(session_id)
            current_count = local_memory.get_write_count(session_id)
            return {
                "action": "workspace",
                "session_id": session_id,
                "output": (
                    f"Writes: {current_count}/{current_cap}. "
                    f"Usage: workspace writes-bump <new-cap>"
                ),
                "reason": ER.INVALID_ARGUMENT,
            }
        try:
            new_cap = int(raw_n[1].strip())
            if new_cap < 1:
                raise ValueError
        except ValueError:
            return {
                "action": "workspace",
                "session_id": session_id,
                "output": f"Usage: workspace writes-bump <positive integer>",
                "reason": ER.INVALID_ARGUMENT,
            }
        applied = local_memory.set_write_cap(session_id=session_id, cap=new_cap)
        return {
            "action": "workspace",
            "session_id": session_id,
            "output": f"Write cap raised to {applied} for this session.",
        }

    # `workspace audit` — scan stored paths for cross-workspace
    # leakage. Adopted from Local AI Workbench's isolation pattern.
    # See `docs/design/workspace-writes.md` Phase 3.
    if lower_arg == "audit":
        from app.services import workspace_isolation
        result = workspace_isolation.audit_session(session_id=session_id)
        if result.skipped_reason:
            return {
                "action": "workspace",
                "session_id": session_id,
                "output": workspace_isolation.format_audit_report(result),
                "reason": ER.NO_WORKSPACE,
                "result": result.to_dict(),
            }
        # Errors -> emit the dispatcher's standard refusal so callers
        # (CoWork pre-flight, MCP) can pattern-match. Warnings stay
        # advisory: the report is shown, no `reason` set.
        event = {
            "action": "workspace",
            "session_id": session_id,
            "output": workspace_isolation.format_audit_report(result),
            "result": result.to_dict(),
        }
        if result.errors:
            event["reason"] = ER.OUTSIDE_WORKSPACE
        return event

    resolved_path, error, reason = resolve_workspace_path(argument, cwd=os.getcwd())
    if error:
        return {
            "action": "workspace",
            "session_id": session_id,
            "output": error,
            "reason": reason,
        }

    local_memory.set_workspace(session_id=session_id, workspace_path=resolved_path)
    local_memory.clear_pending_approval(session_id=session_id)
    local_memory.clear_approved_search(session_id=session_id)
    return {
        "action": "workspace",
        "session_id": session_id,
        "output": render_workspace_status(resolved_path),
    }


def _queue_workspace_action(session_id, pending_action):
    local_memory.set_pending_approval(session_id=session_id, pending_approval=pending_action)
    return {
        "action": "approval",
        "session_id": session_id,
        "output": (
            "[approval required]\n"
            f"Requested action: {describe_pending_action(pending_action)}\n"
            "Type `approve` to continue or `deny` to cancel."
        ),
        "pending_action": pending_action,
        # The read/search request didn't fail — but it's queued behind
        # an approval gate, so consumers (HTTP gateway, MCP) need to
        # know they MUST act before the operation runs.
        "reason": ER.APPROVAL_REQUIRED,
    }


def _workspace_root_or_error(session_id):
    workspace_path = local_memory.get_session_context(session_id).get("workspace_path")
    if not workspace_path:
        return None, "No workspace is pinned for this session. Use `workspace <path>` first."
    return workspace_path, None


def _handle_read_command(text, session_id):
    workspace_path, error = _workspace_root_or_error(session_id)
    if error:
        return {"action": "read", "session_id": session_id, "output": error, "reason": ER.NO_WORKSPACE}

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {
            "action": "read",
            "session_id": session_id,
            "output": "Usage: read <path>",
            "reason": ER.INVALID_ARGUMENT,
        }

    pending_action = {
        "kind": "read",
        "origin": "command",
        "workspace_path": workspace_path,
        "paths": [parts[1].strip()],
        "reason": "manual workspace read command",
    }
    return _queue_workspace_action(session_id, pending_action)


def _handle_search_command(text, session_id):
    workspace_path, error = _workspace_root_or_error(session_id)
    if error:
        return {"action": "search", "session_id": session_id, "output": error, "reason": ER.NO_WORKSPACE}

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {
            "action": "search",
            "session_id": session_id,
            "output": "Usage: search <query>",
            "reason": ER.INVALID_ARGUMENT,
        }

    pending_action = {
        "kind": "search",
        "origin": "command",
        "workspace_path": workspace_path,
        "query": parts[1].strip(),
        "reason": "manual workspace search command",
    }
    return _queue_workspace_action(session_id, pending_action)


# ============================================================
# Workspace writes (Phase 1 — see docs/design/workspace-writes.md)
# ============================================================

def _parse_write_command(text):
    """Parse `write <path>` or heredoc form `write <path> <<EOF\\n...\\nEOF`.

    Returns ``(path, content, error_text)``. ``content`` is None if no
    body was supplied (caller handles the empty-content case).
    """
    if "\n" in text:
        first_line, _, body = text.partition("\n")
    else:
        first_line, body = text, ""
    parts = first_line.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None, None, "Usage: write <path> <<EOF\\n<content>\\nEOF"
    rest = parts[1].strip()

    # Heredoc form: `write <path> <<MARKER\n<content>\nMARKER`
    if "<<" in rest:
        path_part, _, marker_part = rest.partition("<<")
        path = path_part.strip()
        marker = marker_part.strip()
        if not path or not marker:
            return None, None, "Usage: write <path> <<EOF\\n<content>\\nEOF"
        # Strip the trailing marker line from `body`. Allow content
        # without a trailing newline before the marker.
        lines = body.split("\n")
        # Find the LAST occurrence of the marker on its own line.
        marker_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == marker:
                marker_idx = i
                break
        if marker_idx is None:
            return None, None, f"Heredoc not closed — expected a line containing only `{marker}`."
        content = "\n".join(lines[:marker_idx])
        # Restore trailing newline if the user's content ended with one
        # (the split swallows the final empty element).
        if marker_idx > 0 and content and not content.endswith("\n"):
            content += "\n"
        return path, content, None

    # Inline form: `write <path> <single-line content>` — splits on the
    # first whitespace after the path.
    path_split = rest.split(maxsplit=1)
    path = path_split[0].strip()
    content = path_split[1] if len(path_split) == 2 else ""
    return path, content, None


def _handle_write_command(text, session_id):
    """`write <path> [<content> | <<EOF\\n...\\nEOF]` — propose a workspace write."""
    workspace_path, error = _workspace_root_or_error(session_id)
    if error:
        return {
            "action": "write",
            "session_id": session_id,
            "output": error,
            "reason": ER.NO_WORKSPACE,
        }

    if local_memory.get_mode(session_id) != "build":
        return {
            "action": "write",
            "session_id": session_id,
            "output": (
                "Cannot propose write: session is in Plan mode (read-only).\n"
                "Use `mode build` to enable workspace writes."
            ),
            "reason": ER.MODE_REQUIRED,
        }

    write_count = local_memory.get_write_count(session_id)
    write_cap = local_memory.get_write_cap(session_id)
    if write_count >= write_cap:
        return {
            "action": "write",
            "session_id": session_id,
            "output": (
                f"Write cap reached ({write_count}/{write_cap}). "
                f"Use `workspace writes-bump <n>` to raise."
            ),
            "reason": ER.WRITE_CAP_REACHED,
        }

    path, content, parse_error = _parse_write_command(text)
    if parse_error:
        return {
            "action": "write",
            "session_id": session_id,
            "output": parse_error,
            "reason": ER.INVALID_ARGUMENT,
        }

    resolved = resolve_workspace_write_path(workspace_path, path)
    if not resolved.get("ok"):
        err_kind = resolved.get("error", "")
        reason_map = {
            "missing_path": ER.INVALID_ARGUMENT,
            "outside_workspace": ER.OUTSIDE_WORKSPACE,
            "symlink_unsupported": ER.SYMLINK_UNSUPPORTED,
            "is_directory": ER.IS_DIRECTORY,
        }
        reason = reason_map.get(err_kind, ER.INVALID_ARGUMENT)
        return {
            "action": "write",
            "session_id": session_id,
            "output": f"Cannot write {path}: {err_kind}",
            "reason": reason,
        }

    pending_action = {
        "kind": "write",
        "origin": "command",
        "workspace_path": workspace_path,
        "path": resolved["relative_path"],
        "content": content if content is not None else "",
        "create": not resolved.get("exists", False),
        "reason": "manual workspace write command",
    }
    # Hash the existing content (if any) so we can detect external
    # tampering between propose and approve.
    existing, _existed, _diff_err = read_existing_for_diff(resolved["absolute_path"])
    if existing is not None:
        pending_action["previous_content_hash"] = sha256_text(existing)
    return _queue_workspace_action(session_id, pending_action)


def _handle_delete_command(text, session_id):
    """`delete <path>` — propose a workspace delete."""
    workspace_path, error = _workspace_root_or_error(session_id)
    if error:
        return {
            "action": "delete",
            "session_id": session_id,
            "output": error,
            "reason": ER.NO_WORKSPACE,
        }

    if local_memory.get_mode(session_id) != "build":
        return {
            "action": "delete",
            "session_id": session_id,
            "output": (
                "Cannot propose delete: session is in Plan mode (read-only).\n"
                "Use `mode build` to enable workspace writes."
            ),
            "reason": ER.MODE_REQUIRED,
        }

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {
            "action": "delete",
            "session_id": session_id,
            "output": "Usage: delete <path>",
            "reason": ER.INVALID_ARGUMENT,
        }

    resolved = resolve_workspace_write_path(workspace_path, parts[1].strip())
    if not resolved.get("ok"):
        err_kind = resolved.get("error", "")
        reason_map = {
            "missing_path": ER.INVALID_ARGUMENT,
            "outside_workspace": ER.OUTSIDE_WORKSPACE,
            "symlink_unsupported": ER.SYMLINK_UNSUPPORTED,
            "is_directory": ER.IS_DIRECTORY,
        }
        reason = reason_map.get(err_kind, ER.INVALID_ARGUMENT)
        return {
            "action": "delete",
            "session_id": session_id,
            "output": f"Cannot delete {parts[1].strip()}: {err_kind}",
            "reason": reason,
        }
    if not resolved.get("exists"):
        return {
            "action": "delete",
            "session_id": session_id,
            "output": f"Cannot delete {resolved['relative_path']}: file does not exist.",
            "reason": ER.PATH_NOT_FOUND,
        }

    pending_action = {
        "kind": "delete",
        "origin": "command",
        "workspace_path": workspace_path,
        "path": resolved["relative_path"],
        "reason": "manual workspace delete command",
    }
    return _queue_workspace_action(session_id, pending_action)


def _handle_diff_command(session_id):
    """Show the diff for the currently pending write proposal (if any)."""
    pending = local_memory.get_pending_approval(session_id=session_id)
    if not pending or pending.get("kind") not in {"write", "delete"}:
        return {
            "action": "diff",
            "session_id": session_id,
            "output": "No pending write or delete to diff.",
            "reason": ER.INVALID_ARGUMENT,
        }

    workspace_path = pending["workspace_path"]
    relative_path = pending["path"]
    resolved = resolve_workspace_write_path(workspace_path, relative_path)
    if not resolved.get("ok"):
        return {
            "action": "diff",
            "session_id": session_id,
            "output": f"Cannot diff {relative_path}: {resolved.get('error')}",
            "reason": ER.INVALID_ARGUMENT,
        }

    existing, existed, _err = read_existing_for_diff(resolved["absolute_path"])
    if pending["kind"] == "delete":
        diff = compute_unified_diff(existing or "", "", relative_path)
        header = f"[pending delete] {relative_path}"
    else:
        new_content = pending.get("content", "")
        diff = compute_unified_diff(existing or "", new_content, relative_path)
        header = f"[pending write] {relative_path} ({'create' if not existed else 'modify'})"

    if not diff:
        return {
            "action": "diff",
            "session_id": session_id,
            "output": f"{header}\n(no textual changes)",
        }
    return {
        "action": "diff",
        "session_id": session_id,
        "output": f"{header}\n{diff}",
    }


def _handle_approval_command(session_id, confirmation=""):
    """Apply the pending approval. Phase 2 of patch-taxonomy.md
    layered an optional ``confirmation`` arg used by L3 patches:
    plain ``approve`` is refused for L3, the user must type
    ``approve <phrase>`` matching ``confirmation_phrase_for(action)``.

    L1 / L2 patches accept plain ``approve`` and ignore the
    confirmation arg (so a user who types ``approve foo`` against
    an L2 write still gets the write applied)."""
    pending_action = local_memory.get_pending_approval(session_id=session_id)
    if not pending_action:
        return {
            "action": "approve",
            "session_id": session_id,
            "output": "No pending action to approve.",
            # Nothing pending — approve/deny was called against an empty
            # queue. We don't have a "no_pending_action" tag; the closest
            # canonical meaning is "the command's precondition isn't
            # satisfied", which maps to invalid_argument.
            "reason": ER.INVALID_ARGUMENT,
        }

    # Phase 2: L3 patches gate on a typed confirmation phrase. We
    # check BEFORE clearing pending_approval so a wrong / missing
    # phrase leaves the banner intact for the user to retry.
    from app.services import impact_statements
    tier = impact_statements.tier_for(pending_action)
    if tier == "L3":
        expected = impact_statements.confirmation_phrase_for(pending_action)
        if not impact_statements.confirmation_matches(pending_action, confirmation):
            return {
                "action": "approve",
                "session_id": session_id,
                "output": (
                    "L3 confirmation required. This change is high-impact; "
                    f"type `approve {expected}` (case-insensitive) to "
                    "proceed, or `deny` to cancel. Pending action remains."
                ),
                "reason": ER.APPROVAL_REQUIRED,
            }

    local_memory.clear_pending_approval(session_id=session_id)
    kind = pending_action["kind"]

    # CoWork plan approval — flip status to `approved`. The user runs
    # `cowork run <id>` next to actually execute.
    if kind == "cowork_plan":
        from app.services import cowork
        result = cowork.start_run_after_approval(pending_action["run_id"])
        if not result.get("ok"):
            return {
                "action": "approve",
                "session_id": session_id,
                "output": result.get("output", "Could not approve cowork plan."),
                "reason": result.get("reason", ER.INVALID_ARGUMENT),
            }
        run = result["run"]
        return {
            "action": "approve",
            "session_id": session_id,
            "output": (
                f"Plan {run['id']} approved. Run with `cowork run {run['id']}`."
            ),
            "result": run,
        }

    # Browser navigate — add origin to whitelist + perform the navigate.
    if kind == "browser_navigate":
        from app.services import browser_service as bs
        backend = bs.get_default_backend()
        if not backend.available:
            return {
                "action": "approve",
                "session_id": session_id,
                "output": (
                    "Browser backend unavailable. Install with: "
                    "`pip install playwright` then `playwright install chromium`."
                ),
                "reason": ER.PLAYWRIGHT_NOT_INSTALLED,
            }
        url = pending_action["url"]
        origin_host = pending_action["origin_host"]
        browser_session = pending_action.get("browser_session_id") or session_id
        local_memory.add_approved_origin(session_id=session_id, origin=origin_host)
        result = backend.navigate(browser_session, url)
        return _format_browser_navigate_result(session_id, "navigate", url, result)

    # Workspace writes / deletes — apply to disk and record a backup.
    if kind in {"write", "delete"}:
        return _apply_pending_workspace_write(session_id, pending_action)

    # Bundle (patch-taxonomy.md Phase 1) — atomic apply of N
    # workspace writes/deletes; rollback all on any failure.
    if kind == "bundle":
        return _apply_pending_bundle(session_id, pending_action)

    # Tool call (tool-calling.md Phase 3) — recursively dispatch
    # the model-emitted command. The dispatcher's own safety nets
    # (mode gate, workspace approval, write cap, rule predicates)
    # apply per-command at recursion time.
    if kind == "tool_call":
        command = pending_action.get("command") or ""
        if not command:
            return {
                "action": "approve",
                "session_id": session_id,
                "output": "Pending tool call has no command body.",
                "reason": ER.INVALID_ARGUMENT,
            }
        result = dispatch_input(command, session_id=session_id)
        # Decorate with provenance so the audit trail shows this
        # came from a chat-flow tool-call approval (not direct
        # user input).
        result.setdefault("origin", "chat_tool_call")
        result.setdefault("tool_call_command", command)
        return result

    if kind == "read":
        workspace_result = read_workspace_files(pending_action["workspace_path"], pending_action.get("paths", []))
    else:
        workspace_result = search_workspace(pending_action["workspace_path"], pending_action.get("query"))

    if kind == "search":
        if workspace_result.get("ok"):
            local_memory.set_approved_search(
                session_id=session_id,
                approved_search={
                    "query": workspace_result.get("query"),
                    "files": workspace_result.get("files_with_matches", []),
                },
            )
        else:
            local_memory.clear_approved_search(session_id=session_id)

    if pending_action.get("origin") == "chat" and pending_action.get("original_message") and workspace_result.get("ok"):
        result = handle_chat(
            pending_action["original_message"],
            session_id=session_id,
            approved_workspace_result=workspace_result,
            skip_workspace_approval=True,
        )
        result["workspace_action_message"] = describe_pending_action(pending_action)
        return {
            "action": "chat",
            "session_id": session_id,
            "output": _format_chat_result(result),
            "result": result,
        }

    if kind == "read":
        output = format_workspace_read_result(workspace_result)
    else:
        output = format_workspace_search_result(workspace_result)
    return {
        "action": "approve",
        "session_id": session_id,
        "output": output,
        "result": workspace_result,
    }


def _publish_workspace_write_event(kind_const, session_id, payload):
    """Best-effort publish of a workspace-write event to the gateway bus."""
    try:
        from app.services.gateway.bus import default_bus, GatewayEvent

        default_bus().publish(
            GatewayEvent(
                kind=kind_const,
                session_id=session_id,
                payload=payload,
            )
        )
    except Exception:  # noqa: BLE001
        pass


def _apply_pending_workspace_write(session_id, pending_action):
    """Apply an approved write or delete to disk and record a backup.

    Re-checks the cap, mode, and previous-content-hash at apply time
    (the user may have approved much later — state could have changed).
    Always emits a `KIND_WORKSPACE_WRITE` event on success and stores
    the backup so undo can later restore.
    """
    workspace_path = pending_action["workspace_path"]
    relative_path = pending_action["path"]
    kind = pending_action["kind"]

    # Re-check mode at apply time.
    if local_memory.get_mode(session_id) != "build":
        return {
            "action": "approve",
            "session_id": session_id,
            "output": "Approval refused: session is no longer in Build mode.",
            "reason": ER.MODE_REQUIRED,
        }

    # Re-check cap at apply time (no race protection across processes,
    # but catches the single-process case where the user bumped writes
    # in between propose and approve).
    write_count = local_memory.get_write_count(session_id)
    write_cap = local_memory.get_write_cap(session_id)
    if write_count >= write_cap:
        return {
            "action": "approve",
            "session_id": session_id,
            "output": (
                f"Approval refused: write cap reached ({write_count}/{write_cap})."
            ),
            "reason": ER.WRITE_CAP_REACHED,
        }

    resolved = resolve_workspace_write_path(workspace_path, relative_path)
    if not resolved.get("ok"):
        return {
            "action": "approve",
            "session_id": session_id,
            "output": f"Cannot apply: {resolved.get('error')}",
            "reason": ER.INVALID_ARGUMENT,
        }

    absolute_path = resolved["absolute_path"]

    # Capture the previous content (for backup) and detect external
    # tampering: if the proposal recorded a hash, the file's current
    # content must still match.
    existing_content, existed, _err = read_existing_for_diff(absolute_path)
    expected_hash = pending_action.get("previous_content_hash")
    if expected_hash and existed:
        current_hash = sha256_text(existing_content or "")
        if current_hash != expected_hash:
            return {
                "action": "approve",
                "session_id": session_id,
                "output": (
                    f"Cannot apply {kind}: file changed on disk between "
                    f"propose and approve. Re-issue the command to refresh."
                ),
                "reason": ER.WRITE_BACKUP_STALE,
            }

    # Apply.
    if kind == "write":
        new_content = pending_action.get("content", "")
        result = apply_workspace_write(absolute_path, new_content)
        action_label = "write"
        action_describe = "create" if not existed else "modify"
        bytes_written = result.get("bytes_written", 0)
    else:  # delete
        result = apply_workspace_delete(absolute_path)
        action_label = "delete"
        action_describe = "delete"
        bytes_written = 0
        new_content = None

    if not result.get("ok"):
        return {
            "action": "approve",
            "session_id": session_id,
            "output": f"Could not apply {kind}: {result.get('error', 'unknown')}",
            "reason": ER.INTERNAL_ERROR,
        }

    # Record the backup so undo can restore. Use a short id so logs are
    # readable; collisions across cap rotation are fine.
    import secrets
    backup_id = "wb_" + secrets.token_hex(4)
    backup = {
        "id": backup_id,
        "path": resolved["relative_path"],
        "kind": kind,
        "previous_content": existing_content,
        "previous_existed": existed,
        "previous_sha256": sha256_text(existing_content) if existed else None,
        "applied_at": local_memory._now() if hasattr(local_memory, "_now") else None,
    }
    local_memory.add_write_backup(session_id, backup)

    _publish_workspace_write_event(
        "workspace_write",
        session_id,
        {
            "kind": kind,
            "path": resolved["relative_path"],
            "action": action_describe,
            "bytes": bytes_written,
            "backup_id": backup_id,
        },
    )

    # Phase 2 chat-flow follow-up: if this write came from a chat
    # response, the corresponding builder task is still in_progress
    # with the approval-pending marker. Flip it to done so cowork (and
    # any caller of `dispatch_and_wait_for_existing`) can move on.
    if pending_action.get("origin") == "chat":
        in_progress = local_memory.in_progress_task_for(session_id)
        if (
            in_progress
            and in_progress.get("result") == "(awaiting workspace approval)"
        ):
            done_msg = (
                f"wrote {resolved['relative_path']} "
                f"({action_describe}, {bytes_written} bytes)"
                if kind == "write"
                else f"deleted {resolved['relative_path']}"
            )
            updated = local_memory.mark_task(
                in_progress["id"], status="done", result=done_msg
            )
            # Best-effort task-event publication (mirrors what
            # _run_one_inbox_task does on the happy path).
            try:
                _publish_task_event(
                    updated or in_progress, "done",
                    builder_session=session_id,
                )
            except Exception:  # noqa: BLE001
                pass

    if kind == "write":
        msg = (
            f"Wrote {resolved['relative_path']} "
            f"({action_describe}, {bytes_written} bytes). Backup: {backup_id}."
        )
    else:
        msg = f"Deleted {resolved['relative_path']}. Backup: {backup_id}."

    return {
        "action": "approve",
        "session_id": session_id,
        "output": msg,
        "result": {
            "kind": kind,
            "path": resolved["relative_path"],
            "action": action_describe,
            "bytes_written": bytes_written,
            "backup_id": backup_id,
        },
    }


def _apply_pending_bundle(session_id, pending_action):
    """Apply a ``kind="bundle"`` pending_approval. Sub-action kinds:

    * ``write`` / ``delete`` (Phase 1 of patch-taxonomy.md) —
      atomic apply: if any later sub-action fails, every
      previously-applied write/delete is rolled back via
      ``_undo_last_workspace_write``.
    * ``tool_call`` (Phase 3b of tool-calling.md) — recursive
      ``dispatch_input``. **Fail-stop semantics**: if a tool_call
      sub-action fails OR a later sub-action fails after a
      successful tool_call, the tool_call's effects are NOT
      undone (we have no rollback machinery for arbitrary
      dispatcher commands). Writes from the same bundle ARE still
      rolled back; the user is told in the output which tool
      calls already ran and can't be reverted.

    Returns a single result dict summarising the bundle outcome.
    """
    sub_actions = list(pending_action.get("sub_actions") or [])
    if not sub_actions:
        return {
            "action": "approve",
            "session_id": session_id,
            "output": "Bundle has no sub-actions; nothing to apply.",
            "reason": ER.INVALID_ARGUMENT,
        }

    # Classify sub-actions so the pre-flight only enforces what
    # actually applies. A tool-call-only bundle doesn't need
    # Build mode + doesn't count against the write cap.
    write_like_count = sum(
        1 for sub in sub_actions
        if (sub.get("kind") or "").lower() in ("write", "delete")
    )
    has_tool_calls = any(
        (sub.get("kind") or "").lower() == "tool_call" for sub in sub_actions
    )

    if write_like_count > 0:
        if local_memory.get_mode(session_id) != "build":
            return {
                "action": "approve",
                "session_id": session_id,
                "output": (
                    "Approval refused: session is no longer in Build "
                    "mode (bundle contains workspace writes/deletes)."
                ),
                "reason": ER.MODE_REQUIRED,
            }
        write_count = local_memory.get_write_count(session_id)
        write_cap = local_memory.get_write_cap(session_id)
        if write_count + write_like_count > write_cap:
            return {
                "action": "approve",
                "session_id": session_id,
                "output": (
                    f"Approval refused: bundle would exceed write cap "
                    f"({write_count}+{write_like_count} > {write_cap})."
                ),
                "reason": ER.WRITE_CAP_REACHED,
            }

    applied = []
    applied_writes = 0       # only count write/delete for rollback math
    applied_tool_calls = 0   # for the success message + warning channel
    for idx, sub in enumerate(sub_actions):
        sub_kind = (sub.get("kind") or "").lower()

        if sub_kind == "tool_call":
            command = sub.get("command") or ""
            if not command:
                # Treat as failure — but tool calls have no rollback,
                # so prior writes need to roll back.
                return _bundle_fail(
                    session_id, sub_actions, applied, applied_writes,
                    idx=idx, sub=sub,
                    sub_result={"output": "tool_call has empty command",
                                "reason": ER.INVALID_ARGUMENT},
                    applied_tool_calls=applied_tool_calls,
                )
            sub_result = dispatch_input(command, session_id=session_id)
            if sub_result.get("reason"):
                return _bundle_fail(
                    session_id, sub_actions, applied, applied_writes,
                    idx=idx, sub=sub, sub_result=sub_result,
                    applied_tool_calls=applied_tool_calls,
                )
            applied_tool_calls += 1
            applied.append({
                "kind": "tool_call",
                "command": command,
                "result_action": sub_result.get("action"),
                "result_output": (sub_result.get("output") or "")[:200],
            })
            continue

        # write / delete path — existing semantics.
        sub_result = _apply_pending_workspace_write(session_id, sub)
        if sub_result.get("reason"):
            return _bundle_fail(
                session_id, sub_actions, applied, applied_writes,
                idx=idx, sub=sub, sub_result=sub_result,
                applied_tool_calls=applied_tool_calls,
            )
        applied_writes += 1
        applied.append(sub_result.get("result") or {})

    # Success — render a kind-aware summary.
    summary_lines = []
    for entry in applied:
        if entry.get("kind") == "tool_call":
            summary_lines.append(f"  - tool call: {entry.get('command')!r}")
        else:
            summary_lines.append(
                f"  - {entry.get('action', 'write')} {entry.get('path')}"
            )
    counts_bits = []
    if applied_writes:
        counts_bits.append(f"{applied_writes} file change{'s' if applied_writes != 1 else ''}")
    if applied_tool_calls:
        counts_bits.append(f"{applied_tool_calls} tool call{'s' if applied_tool_calls != 1 else ''}")
    counts = " + ".join(counts_bits) or f"{len(applied)} action(s)"
    undo_hint = (
        f"Use `undo` {applied_writes}x to roll back the file changes."
        if applied_writes else "(tool calls don't have automatic undo)"
    )
    if applied_writes and applied_tool_calls:
        undo_hint = (
            f"Use `undo` {applied_writes}x to roll back the file changes; "
            f"tool calls cannot be auto-reverted."
        )
    return {
        "action": "approve",
        "session_id": session_id,
        "output": (
            f"Bundle applied: {counts}.\n"
            + "\n".join(summary_lines) + "\n"
            + undo_hint
        ),
        "result": {
            "kind": "bundle",
            "applied": applied,
            "count": len(applied),
            "writes_applied": applied_writes,
            "tool_calls_applied": applied_tool_calls,
        },
    }


def _bundle_fail(
    session_id, sub_actions, applied, applied_writes, *,
    idx, sub, sub_result, applied_tool_calls=0,
):
    """Roll back any previously-applied write/delete sub-actions
    (tool calls are NOT rolled back — fail-stop semantics) and
    return a structured failure result. Phase 3b extension of the
    Phase 1 bundle rollback path."""
    rollback_lines = []
    rolled = 0
    for _ in range(applied_writes):
        undo = _undo_last_workspace_write(session_id)
        if undo is None:
            rollback_lines.append("WARNING: backup stack empty mid-rollback")
            break
        if undo.get("reason"):
            rollback_lines.append(
                f"WARNING: rollback step failed: {undo.get('output')}"
            )
        else:
            rolled += 1

    sub_kind = (sub.get("kind") or "").lower()
    sub_label = (
        f"tool_call: {(sub.get('command') or '')[:80]}"
        if sub_kind == "tool_call"
        else f"{sub_kind} {sub.get('path')!r}"
    )
    output_lines = [
        f"Bundle failed at sub-action {idx + 1}/{len(sub_actions)}:",
        f"  {sub_label}: {sub_result.get('output')}",
        "",
    ]
    if applied_writes:
        output_lines.append(
            f"Rolled back {rolled}/{applied_writes} previously-applied "
            f"write/delete sub-action(s)."
        )
    if applied_tool_calls:
        output_lines.append(
            f"WARNING: {applied_tool_calls} tool call(s) already ran and "
            f"cannot be auto-reverted."
        )
    output_lines.extend(f"  {line}" for line in rollback_lines if line)
    return {
        "action": "approve",
        "session_id": session_id,
        "output": "\n".join(output_lines),
        "reason": sub_result.get("reason"),
        "result": {
            "kind": "bundle",
            "applied": applied,
            "failed_at_index": idx,
            "failed_kind": sub_kind,
            "rolled_back_writes": rolled,
            "applied_tool_calls": applied_tool_calls,
            "rollback_warnings": rollback_lines,
        },
    }


def _undo_last_workspace_write(session_id):
    """Roll back the most recent approved write/delete on disk.

    Phase 1.5 of `docs/design/workspace-writes.md`. Returns either:

      - ``None`` if there is no workspace-write backup to undo (caller
        should fall through to the in-memory `local_memory.undo()`).
      - A result dict ``{action, session_id, output, ...}`` describing
        the rollback (success or refusal). On success the matching
        backup is popped and `write_count` is decremented so the soft
        cap stays consistent.

    Honest gap (Phase 1.5 only): we do not currently verify that the
    file's on-disk content still matches what we wrote at apply time —
    we don't record the post-write hash, only the pre-write one. So if
    another process modified the file between our write and this undo,
    we'll happily clobber their change. A future round (Phase 1.6) can
    record `applied_sha256` and skip the undo with `write_backup_stale`
    when the file has drifted.
    """
    backups = local_memory.get_write_backups(session_id)
    if not backups:
        return None

    backup = backups[-1]

    workspace_path = local_memory.get_session_context(session_id).get("workspace_path")
    if not workspace_path:
        return {
            "action": "undo",
            "session_id": session_id,
            "output": (
                "Cannot undo workspace write: no workspace is pinned. "
                "Re-pin the workspace and try again, or use `redo` paths "
                "that don't require disk access."
            ),
            "reason": ER.NO_WORKSPACE,
        }

    relative_path = backup.get("path", "")
    resolved = resolve_workspace_write_path(workspace_path, relative_path)
    if not resolved.get("ok"):
        return {
            "action": "undo",
            "session_id": session_id,
            "output": (
                f"Cannot undo {backup.get('kind', 'write')} of {relative_path}: "
                f"{resolved.get('error', 'unknown')}."
            ),
            "reason": ER.INVALID_ARGUMENT,
        }

    absolute_path = resolved["absolute_path"]
    kind = backup.get("kind")
    previous_existed = bool(backup.get("previous_existed"))
    previous_content = backup.get("previous_content")

    # Decide what "undo" means for each kind/state pair.
    if kind == "write" and not previous_existed:
        # Original was a CREATE — undo deletes the file.
        del_result = apply_workspace_delete(absolute_path)
        if not del_result.get("ok"):
            return {
                "action": "undo",
                "session_id": session_id,
                "output": (
                    f"Could not undo create of {relative_path}: "
                    f"{del_result.get('error', 'unknown')}."
                ),
                "reason": ER.INTERNAL_ERROR,
            }
        already_gone = not del_result.get("removed", False)
        suffix = " (already gone)" if already_gone else ""
        msg = f"Undid create of {relative_path}: file removed{suffix}."
        action_describe = "undo_create"
    elif kind == "write" and previous_existed:
        # Original OVERWROTE existing content — undo restores it.
        # `apply_workspace_write` re-creates the file even if it was
        # since deleted, which is the right behaviour: undo means
        # "put back what was there before our write".
        restore = apply_workspace_write(absolute_path, previous_content or "")
        if not restore.get("ok"):
            return {
                "action": "undo",
                "session_id": session_id,
                "output": (
                    f"Could not undo modify of {relative_path}: "
                    f"{restore.get('error', 'unknown')}."
                ),
                "reason": ER.INTERNAL_ERROR,
            }
        msg = f"Undid modify of {relative_path}: previous content restored."
        action_describe = "undo_modify"
    elif kind == "delete" and previous_existed:
        # Original DELETED a file — undo restores it.
        restore = apply_workspace_write(absolute_path, previous_content or "")
        if not restore.get("ok"):
            return {
                "action": "undo",
                "session_id": session_id,
                "output": (
                    f"Could not undo delete of {relative_path}: "
                    f"{restore.get('error', 'unknown')}."
                ),
                "reason": ER.INTERNAL_ERROR,
            }
        msg = f"Undid delete of {relative_path}: file restored."
        action_describe = "undo_delete"
    else:
        # Defensive: a delete recorded with previous_existed=False is
        # nonsense (we don't even queue the proposal). Treat as no-op
        # so we still pop the backup and keep state consistent.
        msg = (
            f"Skipped undo for {relative_path}: unrecognised backup state "
            f"(kind={kind}, previous_existed={previous_existed})."
        )
        action_describe = "undo_noop"

    # Disk side succeeded — drop the backup and rebalance the cap.
    local_memory.pop_write_backup(session_id=session_id)
    local_memory.decrement_write_count(session_id=session_id)

    _publish_workspace_write_event(
        "workspace_write",
        session_id,
        {
            "kind": kind,
            "path": resolved["relative_path"],
            "action": action_describe,
            "event": "undone",
            "backup_id": backup.get("id"),
        },
    )

    return {
        "action": "undo",
        "session_id": session_id,
        "output": msg,
        "result": {
            "kind": kind,
            "path": resolved["relative_path"],
            "action": action_describe,
            "backup_id": backup.get("id"),
        },
    }


def _handle_undo_command(session_id):
    """Dispatcher entry point for `undo` / `/undo` / `write undo`.

    First tries to roll back the most recent workspace write/delete on
    disk (Phase 1.5). If there are no write backups, falls through to
    the original in-memory `local_memory.undo()` snapshot swap.
    """
    workspace_event = _undo_last_workspace_write(session_id)
    if workspace_event is not None:
        return workspace_event

    ok = local_memory.undo()
    if ok:
        output = "Reverted last memory mutation. Use `redo` to put it back."
    else:
        output = "Nothing to undo."
    return {"action": "undo", "session_id": session_id, "output": output}


def _handle_deny_command(session_id):
    pending_action = local_memory.get_pending_approval(session_id=session_id)
    if not pending_action:
        return {
            "action": "deny",
            "session_id": session_id,
            "output": "No pending action to deny.",
            "reason": ER.INVALID_ARGUMENT,  # see _handle_approval_command
        }

    local_memory.clear_pending_approval(session_id=session_id)

    # Emit a deny event for write/delete proposals so subscribers can
    # show "denied" rather than silently dropping the proposal.
    if pending_action.get("kind") in {"write", "delete"}:
        _publish_workspace_write_event(
            "workspace_deny",
            session_id,
            {
                "kind": pending_action["kind"],
                "path": pending_action.get("path"),
            },
        )
    # Bundle deny — emit one workspace_deny event per sub-action so
    # subscribers see exactly what was rejected (mirrors the
    # workspace_write fan-out on bundle apply).
    if pending_action.get("kind") == "bundle":
        for sub in pending_action.get("sub_actions") or []:
            if sub.get("kind") in {"write", "delete"}:
                _publish_workspace_write_event(
                    "workspace_deny",
                    session_id,
                    {
                        "kind": sub.get("kind"),
                        "path": sub.get("path"),
                        "bundle": True,
                    },
                )

    # Cowork plan denial — mark the run cancelled so the user can plan
    # a new goal immediately.
    if pending_action.get("kind") == "cowork_plan":
        from app.services import cowork
        cowork.cancel_run_after_deny(pending_action["run_id"])

    return {
        "action": "deny",
        "session_id": session_id,
        "output": f"Canceled pending action: {describe_pending_action(pending_action)}.",
    }


def _handle_skill_command(text, session_id):
    parts = text.split(maxsplit=2)
    current_active = list(local_memory.get_session_context(session_id).get("active_skills", []))

    if len(parts) == 1:
        return {
            "action": "skills",
            "session_id": session_id,
            "output": format_skill_overview(current_active),
        }

    verb = parts[1].lower()
    if verb == "clear":
        local_memory.set_active_skills(session_id=session_id, skills=[])
        return {
            "action": "skills",
            "session_id": session_id,
            "output": "Cleared active skills for this session.",
        }

    if len(parts) < 3:
        return {
            "action": "skills",
            "session_id": session_id,
            "output": "Usage: skill add <name>, skill remove <name>, skill clear",
            "reason": ER.INVALID_ARGUMENT,
        }

    skill_name = parts[2].strip()
    skill = get_skill(skill_name)
    if not skill:
        return {
            "action": "skills",
            "session_id": session_id,
            "output": f"Unknown skill '{skill_name}'.\n\n{format_skill_overview(current_active)}",
            "reason": ER.UNKNOWN_TARGET,
        }

    if verb in {"add", "use", "enable"}:
        updated = normalize_active_skills(current_active + [skill["name"]])
        local_memory.set_active_skills(session_id=session_id, skills=updated)
        return {
            "action": "skills",
            "session_id": session_id,
            "output": f"Enabled skill '{skill['name']}'.\n\n{format_skill_overview(updated)}",
        }

    if verb in {"remove", "disable"}:
        updated = [name for name in current_active if name != skill["name"]]
        local_memory.set_active_skills(session_id=session_id, skills=updated)
        return {
            "action": "skills",
            "session_id": session_id,
            "output": f"Disabled skill '{skill['name']}'.\n\n{format_skill_overview(updated)}",
        }

    return {
        "action": "skills",
        "session_id": session_id,
        "output": "Usage: skill add <name>, skill remove <name>, skill clear",
        "reason": ER.INVALID_ARGUMENT,
    }


def _handle_retry_command(text, session_id):
    parts = text.split(maxsplit=1)
    target = parts[1].strip() if len(parts) == 2 and parts[1].strip() else "last"
    execution = local_memory.get_execution(session_id=session_id, execution_id=target)
    if not execution:
        return {
            "action": "retry",
            "session_id": session_id,
            "output": f"No execution found for '{target}' in session '{session_id}'.",
            "reason": ER.UNKNOWN_TARGET,
        }

    result = retry_saved_execution(execution, session_id=session_id)
    return {
        "action": "chat",
        "session_id": session_id,
        "output": _format_chat_result(result),
        "result": result,
    }


def _handle_execution_command(text, session_id):
    parts = text.split(maxsplit=1)
    target = parts[1].strip() if len(parts) == 2 and parts[1].strip() else "last"
    execution = local_memory.get_execution(session_id=session_id, execution_id=target)
    if not execution:
        return {
            "action": "execution",
            "session_id": session_id,
            "output": f"No execution found for '{target}' in session '{session_id}'.",
            "reason": ER.UNKNOWN_TARGET,
        }

    return {
        "action": "execution",
        "session_id": session_id,
        "output": format_execution_detail(execution),
        "result": execution,
    }


def _format_chat_result(result):
    sections = [result["response"]]

    if result.get("workspace_action_message") and not result.get("approval_required"):
        sections.extend(["", f"[approved workspace action: {result['workspace_action_message']}]"])

    if result.get("retried_from_execution_id"):
        mode = result.get("retry_mode")
        suffix = " using saved runtime state" if mode == "exact" else ""
        sections.extend(["", f"[retried from execution {result['retried_from_execution_id']}{suffix}]"])

    if result["fallback_used"]:
        sections.extend(
            [
                "",
                f"[route fallback: {result['requested_lane']} -> {result['resolved_lane']} via {result['model']}]",
            ]
        )

    if result.get("compaction_performed"):
        sections.extend(
            [
                "",
                "[auto-compacted session memory]",
                result.get("compaction_message") or "Compacted older messages into session memory.",
            ]
        )
        summary_text = result.get("compaction_summary_text", "").strip()
        if summary_text:
            sections.extend(["", summary_text])

    # Phase 2 prompt-assembly: render a single-line footer when any
    # slot was truncated or evicted, so the user sees what got
    # squeezed out. Quiet (no footer) when nothing crossed a budget.
    prompt_trace = result.get("prompt_assembly")
    if prompt_trace:
        from app.services import prompt_assembly as pa
        footer = pa.render_trace_footer(prompt_trace)
        if footer:
            sections.extend(["", footer])

    return "\n".join(sections)


def dispatch_input(text, session_id="default"):
    """Public entry point. Routes to `_dispatch_input_inner` and publishes the
    resulting event on the gateway bus for any subscribers (LinkedView, HTTP
    gateway, agents, etc.).

    Phase 3 of patch-taxonomy.md: every user-initiated dispatch adds
    a timestamp to the proactive trigger's recent-action window
    (best-effort — proactive bookkeeping never blocks dispatch)."""
    event = _dispatch_input_inner(text, session_id=session_id)
    _publish_dispatch_event(event)
    _note_proactive_action(text, session_id)
    return event


#: Commands the proactive trigger does NOT count as fresh user
#: activity. Two reasons to exclude:
#:   1. Recursion guard — proactive's own dispatch_input calls
#:      (when it emits an approved command) shouldn't count.
#:   2. Meta-state operations — undo/redo manipulate the undo
#:      stack; ``note_user_action`` mutates session.context which
#:      pushes a new entry onto that stack and clears the redo
#:      stack. Counting them would break the undo/redo flow.
_PROACTIVE_HOOK_EXCLUDES = ("proactive", "undo", "redo", "approve", "deny")


def _note_proactive_action(text, session_id):
    """Best-effort hook into the proactive trigger's activity gate.
    Only counts user-initiated commands (not internal dispatches
    from the proactive service itself or meta-state ops like
    undo/redo/approve/deny).

    Phase 3b: also fires the auto-scheduler. ``maybe_tick_async``
    is gated on ``AUTOFIRE_ENABLED`` (off by default — tests opt
    in) and runs the actual model call in a daemon thread so the
    dispatch never blocks."""
    if not text:
        return
    first = text.strip().lower().split(maxsplit=1)
    if not first:
        return
    head = first[0].lstrip("/")
    if head in _PROACTIVE_HOOK_EXCLUDES:
        return
    try:
        from app.services import proactive_service
        proactive_service.note_user_action(session_id=session_id)
        proactive_service.maybe_tick_async(session_id=session_id)
    except Exception:  # noqa: BLE001
        pass


def _publish_dispatch_event(event):
    # Lazy import + best-effort: a missing bus must never abort dispatch.
    try:
        from app.services.gateway.bus import GatewayEvent, KIND_DISPATCH, default_bus

        bus = default_bus()
        bus.publish(
            GatewayEvent(
                kind=KIND_DISPATCH,
                session_id=event.get("session_id"),
                payload={
                    "action": event.get("action"),
                    # Only forward a small preview of the output so the bus
                    # doesn't haul big strings around for every dispatch.
                    "output_preview": (event.get("output") or "")[:200],
                    "result": event.get("result"),
                },
            )
        )
    except Exception:  # noqa: BLE001
        # Anything wrong on the bus is a publish-side failure; never block dispatch.
        pass


def _dispatch_input_inner(text, session_id="default"):
    normalized = text.replace("\ufeff", "").replace("\u00ef\u00bb\u00bf", "").strip()
    lowered = normalized.lower()

    if not normalized:
        return {"action": "noop", "session_id": session_id, "output": ""}

    # Pre-dispatch agent-rules evaluation. Any matching `refuse` rule
    # short-circuits the dispatch with the rule's reason + output. A
    # small allowlist (rules / help / exit / etc.) is exempt so a
    # misbehaving rule never locks the user out of the controls used
    # to disable it. See `docs/design/agent-rules.md`.
    _rule_exempt_first_tokens = {
        "rules", "help", "exit", "quit", "doctor", "status", "undo", "redo",
    }
    _first_token = normalized.split(maxsplit=1)[0].lstrip("/").lower()
    if _first_token not in _rule_exempt_first_tokens:
        from app.services import rule_service
        _refusal = rule_service.evaluate_pre_dispatch(text, session_id)
        if _refusal is not None:
            return _refusal

    if lowered in {"exit", "quit", "/exit", "/quit"}:
        return {"action": "exit", "session_id": session_id, "output": "Shutting down."}

    if lowered in {"help", "/help"}:
        return {"action": "help", "session_id": session_id, "output": HELP_TEXT}

    if lowered in {"status", "/status"}:
        return {"action": "status", "session_id": session_id, "output": format_status()}

    if lowered in {"models", "/models"}:
        return {"action": "models", "session_id": session_id, "output": format_models_overview()}

    if lowered in {"doctor", "/doctor"}:
        return {"action": "doctor", "session_id": session_id, "output": format_doctor_report(session_id=session_id)}

    if lowered in {"onboard", "/onboard"}:
        return {"action": "onboard", "session_id": session_id, "output": format_onboarding_guide(session_id=session_id)}

    if lowered in {"approve", "/approve"}:
        return _handle_approval_command(session_id=session_id)
    if lowered.startswith("approve ") or lowered.startswith("/approve "):
        # Phase 2 of patch-taxonomy.md: L3 patches require
        # `approve <phrase>` where <phrase> matches the shell-
        # derived confirmation phrase for the pending patch.
        confirmation = normalized.lstrip("/").split(maxsplit=1)
        confirmation_phrase = (
            confirmation[1].strip() if len(confirmation) > 1 else ""
        )
        return _handle_approval_command(
            session_id=session_id, confirmation=confirmation_phrase,
        )

    if lowered in {"deny", "/deny"}:
        return _handle_deny_command(session_id=session_id)

    if lowered in {"sessions", "/sessions"}:
        sessions = local_memory.list_sessions()
        output = ", ".join(sessions) if sessions else "No saved sessions yet."
        return {"action": "sessions", "session_id": session_id, "output": output}

    if lowered in {"gateway", "/gateway"} or lowered.startswith("gateway ") or lowered.startswith("/gateway "):
        return _handle_gateway_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"link", "/link"} or lowered.startswith("link ") or lowered.startswith("/link "):
        return _handle_link_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"dispatch", "/dispatch"} or lowered.startswith("dispatch ") or lowered.startswith("/dispatch "):
        return _handle_dispatch_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"inbox", "/inbox"} or lowered.startswith("inbox ") or lowered.startswith("/inbox "):
        return _handle_inbox_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"tasks", "/tasks"} or lowered.startswith("tasks ") or lowered.startswith("/tasks "):
        return _handle_tasks_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"do", "/do"} or lowered.startswith("do ") or lowered.startswith("/do "):
        return _handle_do_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"iterate", "/iterate"} or lowered.startswith("iterate ") or lowered.startswith("/iterate "):
        return _handle_iterate_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"notes", "/notes"} or lowered.startswith("notes ") or lowered.startswith("/notes "):
        return _handle_notes_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"note", "/note"} or lowered.startswith("note ") or lowered.startswith("/note "):
        return _handle_note_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"cowork", "/cowork"} or lowered.startswith("cowork ") or lowered.startswith("/cowork "):
        return _handle_cowork_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"template", "/template"} or lowered.startswith("template ") or lowered.startswith("/template "):
        return _handle_template_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"rules", "/rules"} or lowered.startswith("rules ") or lowered.startswith("/rules "):
        return _handle_rules_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"tags", "/tags"} or lowered.startswith("tags ") or lowered.startswith("/tags "):
        return _handle_tags_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"provider", "/provider"} or lowered.startswith("provider ") or lowered.startswith("/provider "):
        return _handle_provider_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"mcp", "/mcp"} or lowered.startswith("mcp ") or lowered.startswith("/mcp "):
        return _handle_mcp_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"browser", "/browser"} or lowered.startswith("browser ") or lowered.startswith("/browser "):
        return _handle_browser_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"backlog", "/backlog"} or lowered.startswith("backlog ") or lowered.startswith("/backlog "):
        return _handle_backlog_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"recall", "/recall"} or lowered.startswith("recall ") or lowered.startswith("/recall "):
        return _handle_recall_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"prompt", "/prompt"} or lowered.startswith("prompt ") or lowered.startswith("/prompt "):
        return _handle_prompt_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"instructions", "/instructions"} or lowered.startswith("instructions ") or lowered.startswith("/instructions "):
        return _handle_instructions_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"proactive", "/proactive"} or lowered.startswith("proactive ") or lowered.startswith("/proactive "):
        return _handle_proactive_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"classifier", "/classifier"} or lowered.startswith("classifier ") or lowered.startswith("/classifier "):
        return _handle_classifier_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"retrieve", "/retrieve"} or lowered.startswith("retrieve ") or lowered.startswith("/retrieve "):
        return _handle_retrieve_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"tool", "/tool"} or lowered.startswith("tool ") or lowered.startswith("/tool "):
        return _handle_tool_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"wait", "/wait"} or lowered.startswith("wait ") or lowered.startswith("/wait "):
        return _handle_wait_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"lsp", "/lsp"} or lowered == "lsp status" or lowered == "/lsp status":
        return _handle_lsp_status(session_id=session_id)
    if lowered.startswith("lsp ") or lowered.startswith("/lsp "):
        return _handle_lsp_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"undo", "/undo"}:
        return _handle_undo_command(session_id=session_id)

    if lowered in {"redo", "/redo"}:
        ok = local_memory.redo()
        if ok:
            output = "Re-applied the last undone mutation."
        else:
            output = "Nothing to redo."
        return {"action": "redo", "session_id": session_id, "output": output}

    if lowered in {"cmd", "/cmd"} or lowered == "cmd list" or lowered == "/cmd list":
        return _handle_cmd_list(session_id=session_id)
    if lowered.startswith("cmd ") or lowered.startswith("/cmd "):
        return _handle_cmd_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"mode", "/mode"}:
        return {
            "action": "mode",
            "session_id": session_id,
            "output": f"Current mode: {local_memory.get_mode(session_id=session_id)}.",
        }

    if lowered.startswith("mode ") or lowered.startswith("/mode "):
        parts = normalized.lstrip("/").split(maxsplit=1)
        target = parts[1].strip().lower() if len(parts) == 2 else ""
        if target not in local_memory.VALID_MODES:
            return {
                "action": "mode",
                "session_id": session_id,
                "output": f"Usage: mode plan | build (got '{target or '<empty>'}')",
                "reason": ER.INVALID_ARGUMENT,
            }
        applied = local_memory.set_mode(session_id=session_id, mode=target)
        if applied == "plan":
            blurb = "Plan mode: model reads context only. Edits / file creation are blocked at the gate."
        else:
            blurb = "Build mode: model can suggest edits and queue workspace actions."
        return {
            "action": "mode",
            "session_id": session_id,
            "output": f"Mode set to {applied}. {blurb}",
        }

    if lowered.startswith("session ") or lowered.startswith("/session "):
        event = _handle_session_switch(normalized.lstrip("/"))
        event.setdefault("session_id", session_id)
        return event

    if lowered.startswith("history") or lowered.startswith("/history"):
        limit = _parse_history_limit(normalized.lstrip("/"))
        return {
            "action": "history",
            "session_id": session_id,
            "output": _format_history(session_id, limit),
        }

    if lowered.startswith("executions") or lowered.startswith("/executions"):
        return _handle_executions_command(normalized.lstrip("/"), session_id=session_id)

    if lowered == "execution" or lowered == "/execution" or lowered.startswith("execution ") or lowered.startswith("/execution "):
        return _handle_execution_command(normalized.lstrip("/"), session_id=session_id)

    if lowered.startswith("retry") or lowered.startswith("/retry"):
        return _handle_retry_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"summary", "/summary"}:
        summary_text = render_compact_summary(get_compact_summary(session_id))
        output = summary_text if summary_text else "No compact session memory yet."
        return {"action": "summary", "session_id": session_id, "output": output}

    if lowered in {"compact", "/compact"}:
        result = maybe_compact_session(session_id=session_id, force=True)
        return {
            "action": "compact",
            "session_id": session_id,
            "output": format_compaction_result(result),
            "result": result,
        }

    if lowered in {"workspace", "/workspace"} or lowered.startswith("workspace ") or lowered.startswith("/workspace "):
        return _handle_workspace_command(normalized.lstrip("/"), session_id=session_id)

    if lowered.startswith("read ") or lowered.startswith("/read "):
        return _handle_read_command(normalized.lstrip("/"), session_id=session_id)

    if lowered.startswith("search ") or lowered.startswith("/search "):
        return _handle_search_command(normalized.lstrip("/"), session_id=session_id)

    # Workspace writes (Phase 1 — see docs/design/workspace-writes.md).
    # Note: `lowered` may not match if the user's content has uppercase
    # in the path or body, so we test the first token of `normalized`
    # for write/delete, not the lowered full string.
    first_token = normalized.split(maxsplit=1)[0].lstrip("/").lower() if normalized else ""
    # `write undo` is an alias of `undo` that exists for clarity when the
    # user is mid-write-flow (Phase 1.5). It must be checked before the
    # generic write handler, which would otherwise try to treat "undo"
    # as a path.
    if lowered in {"write undo", "/write undo"}:
        return _handle_undo_command(session_id=session_id)
    if first_token == "write" and (" " in normalized or "\n" in normalized):
        return _handle_write_command(normalized.lstrip("/"), session_id=session_id)
    if first_token == "delete" and " " in normalized.split("\n", 1)[0]:
        return _handle_delete_command(normalized.lstrip("/"), session_id=session_id)
    if lowered in {"diff", "/diff"}:
        return _handle_diff_command(session_id=session_id)

    if lowered in {"skills", "/skills"}:
        current_active = local_memory.get_session_context(session_id).get("active_skills", [])
        return {
            "action": "skills",
            "session_id": session_id,
            "output": format_skill_overview(current_active),
        }

    if lowered.startswith("skill ") or lowered.startswith("/skill "):
        return _handle_skill_command(normalized.lstrip("/"), session_id=session_id)

    if lowered in {"install-defaults", "/install-defaults"}:
        results = ensure_models_installed()
        return {
            "action": "install-defaults",
            "session_id": session_id,
            "output": format_install_results(results),
        }

    if lowered == "install" or lowered == "/install" or lowered.startswith("install ") or lowered.startswith("/install "):
        parts = normalized.lstrip("/").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            return {
                "action": "install",
                "session_id": session_id,
                "output": "Usage: install <role|model>. Roles: reasoning, coding, vision, unfiltered.",
                "reason": ER.INVALID_ARGUMENT,
            }
        result = install_single_model(parts[1].strip())
        event = {
            "action": "install",
            "session_id": session_id,
            "output": format_single_install_result(result),
            "result": result,
        }
        reason = _classify_install_result(result)
        if reason is not None:
            event["reason"] = reason
        return event

    if lowered == "uninstall" or lowered == "/uninstall" or lowered.startswith("uninstall ") or lowered.startswith("/uninstall "):
        parts = normalized.lstrip("/").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            return {
                "action": "uninstall",
                "session_id": session_id,
                "output": "Usage: uninstall <role|model>. Roles: reasoning, coding, vision, unfiltered.",
                "reason": ER.INVALID_ARGUMENT,
            }
        result = uninstall_single_model(parts[1].strip())
        event = {
            "action": "uninstall",
            "session_id": session_id,
            "output": format_uninstall_result(result),
            "result": result,
        }
        # Uninstall: the only "real failure" is unknown_target (no such
        # role/model). `not_installed` is a successful no-op.
        if result and result.get("model") is None:
            event["reason"] = ER.UNKNOWN_TARGET
        return event

    # Custom slash commands: `/myname [extra args]` substitutes the saved prompt.
    custom_substituted = _try_resolve_custom_command(normalized)
    chat_input = custom_substituted if custom_substituted is not None else normalized

    result = handle_chat(chat_input, session_id=session_id)
    if result.get("approval_required") and result.get("pending_action"):
        local_memory.set_pending_approval(session_id=session_id, pending_approval=result["pending_action"])
    event = {
        "action": "chat",
        "session_id": session_id,
        "output": _format_chat_result(result),
        "result": result,
    }
    # Surface the standard reason tag when chat returned an approval
    # request — consumers (HTTP gateway, MCP, agents) need to detect
    # this without parsing English. See `error-vocabulary.md`.
    if result.get("approval_required"):
        event["reason"] = ER.APPROVAL_REQUIRED
    return event
