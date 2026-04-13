import os

from app.memory import local_memory
from app.services.chat_service import handle_chat
from app.services.compaction_service import (
    format_compaction_result,
    get_compact_summary,
    maybe_compact_session,
    render_compact_summary,
)
from app.services.model_service import ensure_models_installed, format_install_results, format_status
from app.services.skill_service import format_skill_overview, get_skill, normalize_active_skills
from app.services.workspace_service import (
    describe_pending_action,
    format_workspace_read_result,
    format_workspace_search_result,
    read_workspace_files,
    render_workspace_status,
    resolve_workspace_path,
    search_workspace,
)

HELP_TEXT = "\n".join(
    [
        "Commands",
        "- help: show command list",
        "- status: inspect runtime and registered models",
        "- history [n]: show recent messages for the current session",
        "- summary: show compact session memory",
        "- compact: summarize older turns into compact session memory",
        "- workspace: show the current workspace",
        "- workspace <path>: pin a workspace to the current session",
        "- workspace clear: remove the pinned workspace",
        "- read <path>: request approval to read a file inside the pinned workspace",
        "- search <query>: request approval to search inside the pinned workspace",
        "- approve: run the pending approved-or-denied workspace action",
        "- deny: cancel the pending workspace action",
        "- skills: show available and active skills",
        "- skill add <name>: enable a skill for the current session",
        "- skill remove <name>: disable a skill for the current session",
        "- skill clear: clear active skills for the current session",
        "- sessions: list saved sessions",
        "- session <name>: switch to or create a session",
        "- install-defaults: pull missing required models",
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


def _handle_session_switch(text):
    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {
            "action": "session",
            "output": "Usage: session <name>",
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

    resolved_path, error = resolve_workspace_path(argument, cwd=os.getcwd())
    if error:
        return {
            "action": "workspace",
            "session_id": session_id,
            "output": error,
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
    }


def _workspace_root_or_error(session_id):
    workspace_path = local_memory.get_session_context(session_id).get("workspace_path")
    if not workspace_path:
        return None, "No workspace is pinned for this session. Use `workspace <path>` first."
    return workspace_path, None


def _handle_read_command(text, session_id):
    workspace_path, error = _workspace_root_or_error(session_id)
    if error:
        return {"action": "read", "session_id": session_id, "output": error}

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {"action": "read", "session_id": session_id, "output": "Usage: read <path>"}

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
        return {"action": "search", "session_id": session_id, "output": error}

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return {"action": "search", "session_id": session_id, "output": "Usage: search <query>"}

    pending_action = {
        "kind": "search",
        "origin": "command",
        "workspace_path": workspace_path,
        "query": parts[1].strip(),
        "reason": "manual workspace search command",
    }
    return _queue_workspace_action(session_id, pending_action)


def _handle_approval_command(session_id):
    pending_action = local_memory.get_pending_approval(session_id=session_id)
    if not pending_action:
        return {
            "action": "approve",
            "session_id": session_id,
            "output": "No pending action to approve.",
        }

    local_memory.clear_pending_approval(session_id=session_id)
    if pending_action["kind"] == "read":
        workspace_result = read_workspace_files(pending_action["workspace_path"], pending_action.get("paths", []))
    else:
        workspace_result = search_workspace(pending_action["workspace_path"], pending_action.get("query"))

    if pending_action["kind"] == "search":
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

    if pending_action["kind"] == "read":
        output = format_workspace_read_result(workspace_result)
    else:
        output = format_workspace_search_result(workspace_result)
    return {
        "action": "approve",
        "session_id": session_id,
        "output": output,
        "result": workspace_result,
    }


def _handle_deny_command(session_id):
    pending_action = local_memory.get_pending_approval(session_id=session_id)
    if not pending_action:
        return {
            "action": "deny",
            "session_id": session_id,
            "output": "No pending action to deny.",
        }

    local_memory.clear_pending_approval(session_id=session_id)
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
        }

    skill_name = parts[2].strip()
    skill = get_skill(skill_name)
    if not skill:
        return {
            "action": "skills",
            "session_id": session_id,
            "output": f"Unknown skill '{skill_name}'.\n\n{format_skill_overview(current_active)}",
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
    }


def _format_chat_result(result):
    sections = [result["response"]]

    if result.get("workspace_action_message") and not result.get("approval_required"):
        sections.extend(["", f"[approved workspace action: {result['workspace_action_message']}]"])

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

    return "\n".join(sections)


def dispatch_input(text, session_id="default"):
    normalized = text.replace("\ufeff", "").replace("\u00ef\u00bb\u00bf", "").strip()
    lowered = normalized.lower()

    if not normalized:
        return {"action": "noop", "session_id": session_id, "output": ""}

    if lowered in {"exit", "quit", "/exit", "/quit"}:
        return {"action": "exit", "session_id": session_id, "output": "Shutting down."}

    if lowered in {"help", "/help"}:
        return {"action": "help", "session_id": session_id, "output": HELP_TEXT}

    if lowered in {"status", "/status", "models", "/models"}:
        return {"action": "status", "session_id": session_id, "output": format_status()}

    if lowered in {"approve", "/approve"}:
        return _handle_approval_command(session_id=session_id)

    if lowered in {"deny", "/deny"}:
        return _handle_deny_command(session_id=session_id)

    if lowered in {"sessions", "/sessions"}:
        sessions = local_memory.list_sessions()
        output = ", ".join(sessions) if sessions else "No saved sessions yet."
        return {"action": "sessions", "session_id": session_id, "output": output}

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

    result = handle_chat(normalized, session_id=session_id)
    if result.get("approval_required") and result.get("pending_action"):
        local_memory.set_pending_approval(session_id=session_id, pending_approval=result["pending_action"])
    return {
        "action": "chat",
        "session_id": session_id,
        "output": _format_chat_result(result),
        "result": result,
    }
