import json
import os
from uuid import uuid4
from datetime import datetime, timezone

from app.config import SESSION_EXECUTION_HISTORY_LIMIT

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "local_memory.json")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_summary():
    return {
        "facts": [],
        "preferences": [],
        "open_tasks": [],
        "updated_at": None,
        "compacted_messages": 0,
    }


def _default_context():
    return {
        "workspace_path": None,
        "active_skills": [],
        "pending_approval": None,
        "approved_search": None,
    }


def _default_session():
    return {
        "history": [],
        "executions": [],
        "summary": _default_summary(),
        "context": _default_context(),
    }


def _coerce_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _clean_summary_items(items):
    cleaned = []
    seen = set()
    for item in items:
        text = str(item or "").replace("\r", " ").replace("\n", " ").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _legacy_text_to_facts(text):
    lines = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.lower().rstrip(":")
        if normalized in {"facts", "preferences", "open tasks", "recent compacted context"}:
            continue
        line = line.lstrip("-*• ").strip()
        if line:
            lines.append(line)
    return _clean_summary_items(lines)


def _normalize_summary(summary):
    if not isinstance(summary, dict):
        summary = {}

    legacy_facts = _legacy_text_to_facts(summary.get("text"))
    normalized = _default_summary()
    normalized["facts"] = _clean_summary_items(_coerce_list(summary.get("facts")) + legacy_facts)
    normalized["preferences"] = _clean_summary_items(_coerce_list(summary.get("preferences")))
    normalized["open_tasks"] = _clean_summary_items(_coerce_list(summary.get("open_tasks")))
    normalized["updated_at"] = summary.get("updated_at")
    normalized["compacted_messages"] = int(summary.get("compacted_messages", 0) or 0)
    return normalized


def _normalize_context(context):
    if not isinstance(context, dict):
        context = {}

    workspace_path = context.get("workspace_path")
    if workspace_path is not None:
        workspace_path = str(workspace_path).strip() or None

    active_skills = []
    seen = set()
    for skill in _coerce_list(context.get("active_skills")):
        normalized_skill = str(skill or "").strip().lower()
        if not normalized_skill or normalized_skill in seen:
            continue
        seen.add(normalized_skill)
        active_skills.append(normalized_skill)

    normalized = _default_context()
    normalized["workspace_path"] = workspace_path
    normalized["active_skills"] = active_skills
    normalized["pending_approval"] = _normalize_pending_approval(context.get("pending_approval"))
    normalized["approved_search"] = _normalize_approved_search(context.get("approved_search"))
    return normalized


def _normalize_pending_approval(action):
    if not isinstance(action, dict):
        return None

    kind = str(action.get("kind") or "").strip().lower()
    if kind not in {"read", "search"}:
        return None

    origin = str(action.get("origin") or "").strip().lower()
    if origin not in {"chat", "command"}:
        origin = "command"

    workspace_path = str(action.get("workspace_path") or "").strip() or None
    if not workspace_path:
        return None

    normalized = {
        "kind": kind,
        "origin": origin,
        "workspace_path": workspace_path,
        "requested_at": action.get("requested_at") or _now(),
    }

    if kind == "read":
        paths = []
        seen = set()
        for path in _coerce_list(action.get("paths")):
            cleaned = str(path or "").replace("\\", "/").strip().lstrip("./")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            paths.append(cleaned)
        if not paths:
            return None
        normalized["paths"] = paths

    if kind == "search":
        query = str(action.get("query") or "").strip()
        if not query:
            return None
        normalized["query"] = query

    original_message = str(action.get("original_message") or "").strip()
    if original_message:
        normalized["original_message"] = original_message

    reason = str(action.get("reason") or "").strip()
    if reason:
        normalized["reason"] = reason

    return normalized


def _normalize_approved_search(data):
    if not isinstance(data, dict):
        return None

    query = str(data.get("query") or "").strip() or None
    files = []
    seen = set()
    for path in _coerce_list(data.get("files")):
        cleaned = str(path or "").replace("\\", "/").strip().lstrip("./")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        files.append(cleaned)

    if not query and not files:
        return None

    return {
        "query": query,
        "files": files[:8],
        "updated_at": data.get("updated_at") or _now(),
    }


def _normalize_execution_message(message):
    if not isinstance(message, dict):
        return None

    role = str(message.get("role") or "").strip().lower()
    content = str(message.get("content") or "").strip()
    if role not in {"system", "user", "assistant"} or not content:
        return None

    return {
        "role": role,
        "content": content,
    }


def _normalize_execution_messages(messages):
    normalized = []
    for message in _coerce_list(messages):
        rendered = _normalize_execution_message(message)
        if not rendered:
            continue
        normalized.append(rendered)
    return normalized


def _normalize_execution_workspace_result(result):
    if not isinstance(result, dict) or not result.get("ok"):
        return None

    kind = str(result.get("kind") or "").strip().lower()
    if kind == "read":
        files = []
        for file_result in result.get("files", []):
            if not isinstance(file_result, dict) or not file_result.get("ok"):
                continue
            relative_path = str(file_result.get("relative_path") or "").replace("\\", "/").strip()
            content = str(file_result.get("content_with_line_numbers") or "").strip()
            if not relative_path or not content:
                continue
            files.append(
                {
                    "ok": True,
                    "relative_path": relative_path,
                    "content_with_line_numbers": content,
                    "truncated": bool(file_result.get("truncated")),
                }
            )
        if not files:
            return None
        return {
            "ok": True,
            "kind": "read",
            "files": files[:2],
        }

    if kind == "search":
        matches = []
        for match in result.get("matches", []):
            if not isinstance(match, dict):
                continue
            relative_path = str(match.get("relative_path") or "").replace("\\", "/").strip()
            if not relative_path:
                continue
            matches.append(
                {
                    "relative_path": relative_path,
                    "line_number": int(match.get("line_number", 0) or 0),
                    "line": str(match.get("line") or "").strip(),
                }
            )
        files = []
        seen = set()
        for path in _coerce_list(result.get("files_with_matches")):
            cleaned = str(path or "").replace("\\", "/").strip().lstrip("./")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            files.append(cleaned)
        return {
            "ok": True,
            "kind": "search",
            "query": str(result.get("query") or "").strip() or None,
            "raw_query": str(result.get("raw_query") or "").strip() or None,
            "matches": matches[:12],
            "files_with_matches": files[:6],
            "truncated": bool(result.get("truncated")),
        }

    return None


def _normalize_execution_replay(replay):
    if not isinstance(replay, dict):
        return {"model": None, "messages": [], "approved_workspace_result": None}

    model = str(replay.get("model") or "").strip() or None
    return {
        "model": model,
        "messages": _normalize_execution_messages(replay.get("messages")),
        "approved_workspace_result": _normalize_execution_workspace_result(replay.get("approved_workspace_result")),
    }


def _normalize_execution_approval(approval):
    if not isinstance(approval, dict):
        return {"state": "none", "description": "none"}

    state = str(approval.get("state") or "").strip().lower()
    if state not in {"none", "pending", "used"}:
        state = "none"
    description = str(approval.get("description") or "").strip() or "none"
    return {
        "state": state,
        "description": description,
    }


def _normalize_execution(execution):
    if not isinstance(execution, dict):
        return None

    status = str(execution.get("status") or "").strip().lower()
    if status not in {"completed", "failed", "needs_approval", "no_model"}:
        status = "failed"

    message = str(execution.get("message") or "").strip()
    if not message:
        return None

    return {
        "id": str(execution.get("id") or f"exe_{uuid4().hex[:10]}").strip(),
        "created_at": execution.get("created_at") or _now(),
        "status": status,
        "message": message,
        "message_preview": str(execution.get("message_preview") or message).replace("\r", " ").replace("\n", " ").strip(),
        "requested_lane": str(execution.get("requested_lane") or "").strip() or None,
        "resolved_lane": str(execution.get("resolved_lane") or "").strip() or None,
        "model": str(execution.get("model") or "").strip() or None,
        "error": str(execution.get("error") or "").strip() or None,
        "fallback_used": bool(execution.get("fallback_used")),
        "route_confidence": str(execution.get("route_confidence") or "").strip() or None,
        "route_scores": execution.get("route_scores") if isinstance(execution.get("route_scores"), dict) else {},
        "route_primary_signal": str(execution.get("route_primary_signal") or "").strip() or None,
        "route_context_used": bool(execution.get("route_context_used")),
        "route_context_reason": str(execution.get("route_context_reason") or "").strip() or None,
        "inference_message_count": int(execution.get("inference_message_count", 0) or 0)
        if execution.get("inference_message_count") is not None
        else None,
        "summary_included": bool(execution.get("summary_included")),
        "compaction_performed": bool(execution.get("compaction_performed")),
        "approval": _normalize_execution_approval(execution.get("approval")),
        "replay": _normalize_execution_replay(execution.get("replay")),
        "retry_source_execution_id": str(execution.get("retry_source_execution_id") or "").strip() or None,
    }


def _normalize_executions(executions):
    normalized = []
    for execution in _coerce_list(executions):
        rendered = _normalize_execution(execution)
        if not rendered:
            continue
        normalized.append(rendered)
    return normalized[-SESSION_EXECUTION_HISTORY_LIMIT:]


def _ensure_session(session):
    if not isinstance(session, dict):
        session = {}
    session.setdefault("history", [])
    session["executions"] = _normalize_executions(session.get("executions"))
    session["summary"] = _normalize_summary(session.get("summary"))
    session["context"] = _normalize_context(session.get("context"))
    return session


def _ensure_store(data):
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    for session_id, session in list(data["sessions"].items()):
        data["sessions"][session_id] = _ensure_session(session)
    return data


def _read_store():
    if not os.path.exists(MEMORY_FILE):
        return {"sessions": {}}, False

    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        raw_text = f.read()

    if not raw_text.strip():
        return {"sessions": {}}, False

    decoder = json.JSONDecoder()
    try:
        stripped = raw_text.lstrip()
        data, end = decoder.raw_decode(stripped)
        needs_repair = bool(stripped[end:].strip())
    except json.JSONDecodeError:
        return {"sessions": {}}, True

    return _ensure_store(data), needs_repair


def load_memory(session_id=None):
    data, needs_repair = _read_store()
    if needs_repair:
        save_memory(data)
    if session_id is not None:
        return _ensure_session(data["sessions"].get(session_id, _default_session()))
    return data


def save_memory(data):
    temp_file = f"{MEMORY_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(_ensure_store(data), f, indent=2)
        f.write("\n")
    os.replace(temp_file, MEMORY_FILE)


def list_sessions():
    data = load_memory()
    return sorted(data.get("sessions", {}).keys())


def create_session(session_id):
    data = load_memory()
    data["sessions"].setdefault(session_id, _default_session())
    data["sessions"][session_id] = _ensure_session(data["sessions"][session_id])
    save_memory(data)
    return data["sessions"][session_id]


def get_session(session_id="default"):
    return load_memory(session_id=session_id)


def get_history(session_id="default", limit=None):
    history = load_memory(session_id=session_id).get("history", [])
    if limit is None:
        return history
    return history[-limit:]


def get_executions(session_id="default", limit=None):
    executions = load_memory(session_id=session_id).get("executions", [])
    if limit is None:
        return executions
    return executions[-limit:]


def get_execution(session_id="default", execution_id=None):
    executions = get_executions(session_id=session_id)
    if not executions:
        return None

    if execution_id in {None, "", "last", "latest"}:
        return executions[-1]

    target = str(execution_id).strip()
    for execution in executions:
        if execution.get("id") == target:
            return execution
    return None


def get_session_summary(session_id="default"):
    return load_memory(session_id=session_id).get("summary", _default_summary())


def get_session_context(session_id="default"):
    return load_memory(session_id=session_id).get("context", _default_context())


def get_pending_approval(session_id="default"):
    return get_session_context(session_id=session_id).get("pending_approval")


def get_approved_search(session_id="default"):
    return get_session_context(session_id=session_id).get("approved_search")


def update_session_summary(session_id="default", summary=None, compacted_messages=0):
    data = load_memory()
    session = data["sessions"].setdefault(session_id, _default_session())
    session = _ensure_session(session)
    current_summary = session["summary"]
    next_summary = _normalize_summary(summary if summary is not None else current_summary)
    next_summary["updated_at"] = _now()
    next_summary["compacted_messages"] = current_summary.get("compacted_messages", 0) + compacted_messages
    session["summary"] = next_summary
    data["sessions"][session_id] = session
    save_memory(data)
    return session["summary"]


def update_session_context(session_id="default", context=None):
    data = load_memory()
    session = data["sessions"].setdefault(session_id, _default_session())
    session = _ensure_session(session)
    next_context = _normalize_context(context if context is not None else session.get("context"))
    session["context"] = next_context
    data["sessions"][session_id] = session
    save_memory(data)
    return session["context"]


def set_workspace(session_id="default", workspace_path=None):
    context = get_session_context(session_id)
    context["workspace_path"] = workspace_path
    context["approved_search"] = None
    return update_session_context(session_id=session_id, context=context)


def set_active_skills(session_id="default", skills=None):
    context = get_session_context(session_id)
    context["active_skills"] = list(skills or [])
    return update_session_context(session_id=session_id, context=context)


def set_pending_approval(session_id="default", pending_approval=None):
    context = get_session_context(session_id)
    context["pending_approval"] = pending_approval
    return update_session_context(session_id=session_id, context=context)


def clear_pending_approval(session_id="default"):
    return set_pending_approval(session_id=session_id, pending_approval=None)


def set_approved_search(session_id="default", approved_search=None):
    context = get_session_context(session_id)
    context["approved_search"] = approved_search
    return update_session_context(session_id=session_id, context=context)


def clear_approved_search(session_id="default"):
    return set_approved_search(session_id=session_id, approved_search=None)


def replace_history(session_id="default", history=None):
    data = load_memory()
    session = data["sessions"].setdefault(session_id, _default_session())
    session = _ensure_session(session)
    session["history"] = list(history or [])
    data["sessions"][session_id] = session
    save_memory(data)
    return session["history"]


def append_message(role, content, model=None, session_id="default", metadata=None):
    data = load_memory()
    session = data["sessions"].setdefault(session_id, _default_session())
    session = _ensure_session(session)
    msg = {
        "role": role,
        "content": content,
        "timestamp": _now(),
    }
    if model:
        msg["model"] = model
    if metadata:
        msg["metadata"] = metadata
    session["history"].append(msg)
    data["sessions"][session_id] = session
    save_memory(data)
    return msg


def append_execution(execution, session_id="default"):
    normalized = _normalize_execution(execution)
    if not normalized:
        return None

    data = load_memory()
    session = data["sessions"].setdefault(session_id, _default_session())
    session = _ensure_session(session)
    session["executions"].append(normalized)
    session["executions"] = _normalize_executions(session["executions"])
    data["sessions"][session_id] = session
    save_memory(data)
    return normalized
