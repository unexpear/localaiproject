from app.config import INFERENCE_HISTORY_WINDOW, ROUTER_HISTORY_WINDOW
from app.memory import local_memory
from app.models import model_registry, runtime_client
from app.services import compaction_service
from app.services.skill_service import build_skill_system_message, get_lane_biases
from app.services.workspace_service import (
    build_workspace_inspection_system_message,
    build_workspace_system_message,
    describe_pending_action,
    format_approval_request,
    plan_workspace_action,
)
from app.supervisor import router

CONTROL_SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "You are a local desktop AI control layer. Use the provided conversation history, compact session memory, "
        "active skills, and workspace snapshot when available. Workspace reads and searches require explicit approval "
        "before they run. Do not claim to have inspected unseen files, edited code, run commands, or verified results "
        "unless the user explicitly supplied that information."
    ),
}


def _history_to_runtime_messages(history, limit=INFERENCE_HISTORY_WINDOW):
    messages = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content")
        if role not in {"user", "assistant", "system"}:
            continue
        if not content:
            continue
        messages.append({"role": role, "content": content})

    if limit is not None:
        messages = messages[-limit:]
    return messages


def _build_runtime_messages(summary, recent_history, current_message, session_context=None, approved_workspace_result=None):
    context = session_context or {}
    messages = [CONTROL_SYSTEM_MESSAGE]

    skill_message = build_skill_system_message(context.get("active_skills"))
    if skill_message:
        messages.append(skill_message)

    workspace_message = build_workspace_system_message(context.get("workspace_path"))
    if workspace_message:
        messages.append(workspace_message)

    inspection_message = build_workspace_inspection_system_message(approved_workspace_result)
    if inspection_message:
        messages.append(inspection_message)

    summary_message = compaction_service.build_summary_system_message(summary)
    if summary_message:
        messages.append(summary_message)

    messages.extend(_history_to_runtime_messages(recent_history, limit=INFERENCE_HISTORY_WINDOW))
    messages.append({"role": "user", "content": current_message})
    return messages


def handle_chat(message, session_id="default", approved_workspace_result=None, skip_workspace_approval=False):
    session_summary = compaction_service.get_compact_summary(session_id)
    session_context = local_memory.get_session_context(session_id=session_id)
    if not skip_workspace_approval:
        pending_action = plan_workspace_action(
            message,
            session_context.get("workspace_path"),
            approved_search=session_context.get("approved_search"),
        )
        if pending_action:
            pending_action["original_message"] = message
            return {
                "ok": False,
                "session_id": session_id,
                "requested_lane": None,
                "resolved_lane": None,
                "model": None,
                "response": format_approval_request(pending_action),
                "fallback_used": False,
                "memory_saved": False,
                "error": None,
                "approval_required": True,
                "pending_action": pending_action,
                "workspace_action_message": describe_pending_action(pending_action),
                "compaction_performed": False,
                "compaction_message": None,
                "compaction_summary_text": "",
                "compaction_result": None,
            }

    router_history = local_memory.get_history(session_id=session_id, limit=ROUTER_HISTORY_WINDOW)
    route_info = router.route_message(
        message,
        recent_history=router_history,
        lane_biases=get_lane_biases(session_context.get("active_skills")),
    )
    lane = route_info["lane"]
    resolution = model_registry.resolve_model_for_role(lane)

    inference_history = local_memory.get_history(session_id=session_id, limit=INFERENCE_HISTORY_WINDOW)
    runtime_messages = _build_runtime_messages(
        session_summary,
        inference_history,
        message,
        session_context=session_context,
        approved_workspace_result=approved_workspace_result,
    )
    summary_included = compaction_service.has_compact_summary(session_summary)
    workspace_inspection_used = approved_workspace_result.get("ok") if approved_workspace_result else False

    user_metadata = {
        "requested_lane": lane,
        "resolved_lane": resolution["resolved_role"],
        "fallback_used": resolution["fallback_used"],
        "route_confidence": route_info["confidence"],
        "route_scores": route_info["scores"],
        "route_primary_signal": route_info["primary_signal"],
        "route_context_used": route_info["context_used"],
        "route_context_reason": route_info["context_reason"],
        "inference_message_count": len(runtime_messages),
        "summary_included": summary_included,
        "workspace_path": session_context.get("workspace_path"),
        "active_skills": list(session_context.get("active_skills", [])),
        "workspace_inspection_used": workspace_inspection_used,
    }
    local_memory.append_message("user", message, session_id=session_id, metadata=user_metadata)

    if not resolution["model"]:
        response = "No local model is ready yet. Install a registered model or start Ollama and try again."
        assistant_metadata = {
            "requested_lane": lane,
            "resolved_lane": None,
            "fallback_used": False,
            "error": "no_model_available",
            "route_confidence": route_info["confidence"],
            "route_scores": route_info["scores"],
            "route_primary_signal": route_info["primary_signal"],
            "route_context_used": route_info["context_used"],
            "route_context_reason": route_info["context_reason"],
            "inference_message_count": len(runtime_messages),
            "summary_included": summary_included,
            "workspace_path": session_context.get("workspace_path"),
            "active_skills": list(session_context.get("active_skills", [])),
            "workspace_inspection_used": workspace_inspection_used,
        }
        local_memory.append_message(
            "assistant",
            response,
            session_id=session_id,
            metadata=assistant_metadata,
        )
        return {
            "ok": False,
            "session_id": session_id,
            "requested_lane": lane,
            "resolved_lane": None,
            "model": None,
            "response": response,
            "fallback_used": False,
            "memory_saved": True,
            "error": "no_model_available",
            "route_confidence": route_info["confidence"],
            "route_scores": route_info["scores"],
            "route_primary_signal": route_info["primary_signal"],
            "route_context_used": route_info["context_used"],
            "route_context_reason": route_info["context_reason"],
            "inference_message_count": len(runtime_messages),
            "summary_included": summary_included,
            "approval_required": False,
            "pending_action": None,
            "compaction_performed": False,
            "compaction_message": None,
            "compaction_summary_text": "",
            "compaction_result": None,
            "workspace_action_message": None,
        }

    inference = runtime_client.infer_chat(resolution["model"], messages=runtime_messages)
    response = inference["content"] if inference["ok"] else f"[Error calling Ollama: {inference['error']}]"

    assistant_metadata = {
        "requested_lane": lane,
        "resolved_lane": resolution["resolved_role"],
        "fallback_used": resolution["fallback_used"],
        "route_confidence": route_info["confidence"],
        "route_scores": route_info["scores"],
        "route_primary_signal": route_info["primary_signal"],
        "route_context_used": route_info["context_used"],
        "route_context_reason": route_info["context_reason"],
        "inference_message_count": len(runtime_messages),
        "summary_included": summary_included,
        "workspace_path": session_context.get("workspace_path"),
        "active_skills": list(session_context.get("active_skills", [])),
        "workspace_inspection_used": workspace_inspection_used,
    }
    if inference["error"]:
        assistant_metadata["error"] = inference["error"]

    local_memory.append_message(
        "assistant",
        response,
        model=resolution["model"],
        session_id=session_id,
        metadata=assistant_metadata,
    )

    compaction_result = compaction_service.maybe_compact_session(session_id=session_id)
    compaction_summary_text = ""
    compaction_message = None
    if compaction_result.get("performed"):
        compaction_summary_text = compaction_service.render_compact_summary(compaction_result.get("summary", {}))
        compaction_message = compaction_service.format_compaction_result(compaction_result)

    return {
        "ok": inference["ok"],
        "session_id": session_id,
        "requested_lane": lane,
        "resolved_lane": resolution["resolved_role"],
        "model": resolution["model"],
        "response": response,
        "fallback_used": resolution["fallback_used"],
        "memory_saved": True,
        "error": inference["error"],
        "route_confidence": route_info["confidence"],
        "route_scores": route_info["scores"],
        "route_primary_signal": route_info["primary_signal"],
        "route_context_used": route_info["context_used"],
        "route_context_reason": route_info["context_reason"],
        "inference_message_count": len(runtime_messages),
        "summary_included": summary_included,
        "approval_required": False,
        "pending_action": None,
        "compaction_performed": compaction_result.get("performed", False),
        "compaction_message": compaction_message,
        "compaction_summary_text": compaction_summary_text,
        "compaction_result": compaction_result,
        "workspace_action_message": None,
    }
