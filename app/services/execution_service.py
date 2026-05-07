from app.services.workspace_service import describe_pending_action

EXECUTION_LIST_DEFAULT_LIMIT = 8
EXECUTION_LIST_MAX_LIMIT = 25
EXECUTION_REPLAY_MAX_MESSAGES = 12
EXECUTION_REPLAY_MAX_MATCHES = 12


def _clip_text(text, limit=90):
    cleaned = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _status_label(status):
    return {
        "completed": "completed",
        "failed": "failed",
        "needs_approval": "needs approval",
        "no_model": "no model",
    }.get(status, status or "unknown")


def describe_workspace_result(result):
    if not isinstance(result, dict) or not result.get("ok"):
        return "none"

    if result.get("kind") == "read":
        paths = [file_result.get("relative_path") for file_result in result.get("files", []) if file_result.get("ok")]
        if not paths:
            return "read"
        return "read " + ", ".join(paths[:2])

    if result.get("kind") == "search":
        query = result.get("query") or result.get("raw_query") or ""
        return f"search '{query}'".strip()

    return "none"


def summarize_approval(pending_action=None, approved_workspace_result=None):
    if isinstance(approved_workspace_result, dict) and approved_workspace_result.get("ok"):
        return {
            "state": "used",
            "description": describe_workspace_result(approved_workspace_result),
        }

    if isinstance(pending_action, dict):
        return {
            "state": "pending",
            "description": describe_pending_action(pending_action),
        }

    return {
        "state": "none",
        "description": "none",
    }


def snapshot_runtime_messages(messages):
    snapshot = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        if role not in {"system", "user", "assistant"} or not content:
            continue
        snapshot.append({"role": role, "content": content})
        if len(snapshot) >= EXECUTION_REPLAY_MAX_MESSAGES:
            break
    return snapshot


def snapshot_workspace_result(result):
    if not isinstance(result, dict) or not result.get("ok"):
        return None

    if result.get("kind") == "read":
        files = []
        for file_result in result.get("files", [])[:2]:
            if not file_result.get("ok"):
                continue
            files.append(
                {
                    "ok": True,
                    "relative_path": file_result.get("relative_path"),
                    "content_with_line_numbers": file_result.get("content_with_line_numbers", ""),
                    "truncated": bool(file_result.get("truncated")),
                }
            )
        if not files:
            return None
        return {
            "ok": True,
            "kind": "read",
            "files": files,
        }

    if result.get("kind") == "search":
        matches = []
        for match in result.get("matches", [])[:EXECUTION_REPLAY_MAX_MATCHES]:
            matches.append(
                {
                    "relative_path": match.get("relative_path"),
                    "line_number": match.get("line_number"),
                    "line": match.get("line", ""),
                }
            )
        return {
            "ok": True,
            "kind": "search",
            "query": result.get("query"),
            "raw_query": result.get("raw_query"),
            "matches": matches,
            "files_with_matches": list(result.get("files_with_matches", [])[:6]),
            "truncated": bool(result.get("truncated")),
        }

    return None


def build_execution_record(
    message,
    status,
    requested_lane=None,
    resolved_lane=None,
    model=None,
    error=None,
    fallback_used=False,
    pending_action=None,
    approved_workspace_result=None,
    runtime_messages=None,
    retry_source_execution_id=None,
    route_confidence=None,
    route_scores=None,
    route_primary_signal=None,
    route_context_used=False,
    route_context_reason=None,
    inference_message_count=None,
    summary_included=False,
    compaction_performed=False,
):
    replay_messages = snapshot_runtime_messages(runtime_messages)
    replay_context = snapshot_workspace_result(approved_workspace_result)
    return {
        "status": status,
        "message": str(message or "").strip(),
        "message_preview": _clip_text(message, limit=100),
        "requested_lane": requested_lane,
        "resolved_lane": resolved_lane,
        "model": model,
        "error": error,
        "fallback_used": bool(fallback_used),
        "route_confidence": route_confidence,
        "route_scores": route_scores or {},
        "route_primary_signal": route_primary_signal,
        "route_context_used": bool(route_context_used),
        "route_context_reason": route_context_reason,
        "inference_message_count": inference_message_count,
        "summary_included": bool(summary_included),
        "compaction_performed": bool(compaction_performed),
        "approval": summarize_approval(
            pending_action=pending_action,
            approved_workspace_result=approved_workspace_result,
        ),
        "replay": {
            "model": model,
            "messages": replay_messages,
            "approved_workspace_result": replay_context,
        },
        "retry_source_execution_id": retry_source_execution_id,
    }


def format_execution_list(executions, session_id="default", limit=EXECUTION_LIST_DEFAULT_LIMIT):
    rows = list(executions or [])
    if not rows:
        return f"No executions recorded for session '{session_id}' yet."

    lines = [f"Executions ({session_id})"]
    for execution in reversed(rows[-limit:]):
        lane = execution.get("resolved_lane") or execution.get("requested_lane") or "unknown"
        approval = execution.get("approval") or {"state": "none", "description": "none"}
        approval_label = approval.get("description") or approval.get("state") or "none"
        model = execution.get("model") or "none"
        retry_suffix = ""
        if execution.get("retry_source_execution_id"):
            retry_suffix = f" | retry of {execution['retry_source_execution_id']}"
        lines.append(
            f"- {execution.get('id', '(no id)')}: {_status_label(execution.get('status'))} | "
            f"lane {lane} | model {model} | approval {approval_label}{retry_suffix}"
        )
        lines.append(f"  {_clip_text(execution.get('message_preview') or execution.get('message'), limit=120)}")
    lines.append("")
    lines.append("Use: retry <id> or retry last")
    return "\n".join(lines)


def _format_route(execution):
    requested = execution.get("requested_lane") or "unknown"
    resolved = execution.get("resolved_lane") or "unknown"
    if requested == resolved:
        route_text = resolved
    else:
        route_text = f"{requested} -> {resolved}"

    confidence = execution.get("route_confidence")
    if confidence:
        route_text += f" ({confidence} confidence)"
    return route_text


def _format_memory_line(execution):
    parts = []
    if execution.get("inference_message_count") is not None:
        parts.append(f"{execution['inference_message_count']} runtime message(s)")
    if execution.get("summary_included"):
        parts.append("compact summary included")
    if execution.get("compaction_performed"):
        parts.append("session auto-compacted")
    if not parts:
        return "saved normally"
    return ", ".join(parts)


def build_execution_flow(execution):
    if not execution:
        return []

    steps = [
        f"Input: {_clip_text(execution.get('message_preview') or execution.get('message'), limit=120)}",
        f"Route: {_format_route(execution)}",
        f"Model: {execution.get('model') or 'none'}",
    ]

    approval = execution.get("approval") or {"state": "none", "description": "none"}
    if approval.get("state") == "used":
        steps.append(f"Workspace: {approval.get('description')}")
    elif approval.get("state") == "pending":
        steps.append(f"Approval Gate: {approval.get('description')}")
    else:
        steps.append("Workspace: none")

    status = _status_label(execution.get("status"))
    if execution.get("error"):
        steps.append(f"Result: {status} ({execution['error']})")
    else:
        steps.append(f"Result: {status}")

    steps.append(f"Memory: {_format_memory_line(execution)}")
    return steps


def build_execution_graph(execution):
    if not execution:
        return {"nodes": [], "edges": []}

    approval = execution.get("approval") or {"state": "none", "description": "none"}
    route_body = _format_route(execution)
    if execution.get("route_primary_signal"):
        route_body += f"\n{execution['route_primary_signal']}"
    if execution.get("route_context_reason"):
        route_body += f"\n{execution['route_context_reason']}"

    workspace_title = "Approval Gate" if approval.get("state") == "pending" else "Workspace"
    workspace_body = approval.get("description") or "none"

    result_body = _status_label(execution.get("status"))
    if execution.get("error"):
        result_body += f"\n{execution['error']}"

    nodes = [
        {
            "id": "input",
            "title": "Input",
            "body": _clip_text(execution.get("message_preview") or execution.get("message"), limit=120),
            "kind": "input",
        },
        {
            "id": "route",
            "title": "Route",
            "body": route_body,
            "kind": "route",
        },
        {
            "id": "model",
            "title": "Model",
            "body": execution.get("model") or "none",
            "kind": "model",
        },
        {
            "id": "workspace",
            "title": workspace_title,
            "body": workspace_body,
            "kind": "workspace" if approval.get("state") == "used" else "approval",
        },
        {
            "id": "result",
            "title": "Result",
            "body": result_body,
            "kind": "result",
        },
        {
            "id": "memory",
            "title": "Memory",
            "body": _format_memory_line(execution),
            "kind": "memory",
        },
    ]
    edges = [
        ("input", "route"),
        ("route", "model"),
        ("model", "workspace"),
        ("workspace", "result"),
        ("result", "memory"),
    ]
    return {"nodes": nodes, "edges": edges}


def format_execution_detail(execution):
    if not execution:
        return "Execution not found."

    approval = execution.get("approval") or {"state": "none", "description": "none"}
    replay = execution.get("replay") or {}
    lines = [
        f"Execution {execution.get('id', '(no id)')}",
        f"- Status: {_status_label(execution.get('status'))}",
        f"- Created: {execution.get('created_at') or 'unknown'}",
        f"- Message: {execution.get('message') or '(empty)'}",
        f"- Route: {_format_route(execution)}",
        f"- Model: {execution.get('model') or 'none'}",
        f"- Approval: {approval.get('state') or 'none'} ({approval.get('description') or 'none'})",
        f"- Memory: {_format_memory_line(execution)}",
    ]

    if execution.get("route_primary_signal"):
        lines.append(f"- Primary signal: {execution['route_primary_signal']}")
    if execution.get("route_context_reason"):
        lines.append(f"- Context: {execution['route_context_reason']}")
    if execution.get("retry_source_execution_id"):
        lines.append(f"- Retry source: {execution['retry_source_execution_id']}")
    if execution.get("error"):
        lines.append(f"- Error: {execution['error']}")

    replay_messages = replay.get("messages") or []
    replay_workspace = replay.get("approved_workspace_result")
    lines.extend(
        [
            "",
            "Replay Snapshot",
            f"- Saved runtime messages: {len(replay_messages)}",
            f"- Saved workspace context: {describe_workspace_result(replay_workspace)}",
            "",
            "Flow",
        ]
    )
    for index, step in enumerate(build_execution_flow(execution), start=1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines)
