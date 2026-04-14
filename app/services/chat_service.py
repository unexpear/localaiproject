from app.config import INFERENCE_HISTORY_WINDOW, ROUTER_HISTORY_WINDOW
from app.memory import local_memory
from app.models import model_registry, runtime_client
from app.services import compaction_service
from app.services.execution_service import build_execution_record
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


def _append_execution_record(
    message,
    session_id,
    result,
    runtime_messages=None,
    pending_action=None,
    approved_workspace_result=None,
    retry_source_execution_id=None,
):
    if result.get("approval_required"):
        status = "needs_approval"
    elif result.get("error") == "no_model_available":
        status = "no_model"
    elif result.get("ok"):
        status = "completed"
    else:
        status = "failed"

    execution = build_execution_record(
        message=message,
        status=status,
        requested_lane=result.get("requested_lane"),
        resolved_lane=result.get("resolved_lane"),
        model=result.get("model"),
        error=result.get("error"),
        fallback_used=result.get("fallback_used", False),
        pending_action=pending_action or result.get("pending_action"),
        approved_workspace_result=approved_workspace_result,
        runtime_messages=runtime_messages,
        retry_source_execution_id=retry_source_execution_id,
        route_confidence=result.get("route_confidence"),
        route_scores=result.get("route_scores"),
        route_primary_signal=result.get("route_primary_signal"),
        route_context_used=result.get("route_context_used"),
        route_context_reason=result.get("route_context_reason"),
        inference_message_count=result.get("inference_message_count"),
        summary_included=result.get("summary_included"),
        compaction_performed=result.get("compaction_performed"),
    )
    local_memory.append_execution(execution, session_id=session_id)


def _complete_response(
    *,
    session_id,
    message,
    response,
    ok,
    model,
    requested_lane,
    resolved_lane,
    fallback_used,
    error,
    route_confidence,
    route_scores,
    route_primary_signal,
    route_context_used,
    route_context_reason,
    inference_message_count,
    summary_included,
    approved_workspace_result,
    retry_source_execution_id=None,
    retry_mode=None,
    runtime_messages=None,
):
    compaction_result = compaction_service.maybe_compact_session(session_id=session_id)
    compaction_summary_text = ""
    compaction_message = None
    if compaction_result.get("performed"):
        compaction_summary_text = compaction_service.render_compact_summary(compaction_result.get("summary", {}))
        compaction_message = compaction_service.format_compaction_result(compaction_result)

    result = {
        "ok": ok,
        "session_id": session_id,
        "requested_lane": requested_lane,
        "resolved_lane": resolved_lane,
        "model": model,
        "response": response,
        "fallback_used": fallback_used,
        "memory_saved": True,
        "error": error,
        "route_confidence": route_confidence,
        "route_scores": route_scores,
        "route_primary_signal": route_primary_signal,
        "route_context_used": route_context_used,
        "route_context_reason": route_context_reason,
        "inference_message_count": inference_message_count,
        "summary_included": summary_included,
        "approval_required": False,
        "pending_action": None,
        "compaction_performed": compaction_result.get("performed", False),
        "compaction_message": compaction_message,
        "compaction_summary_text": compaction_summary_text,
        "compaction_result": compaction_result,
        "workspace_action_message": None,
        "retried_from_execution_id": retry_source_execution_id,
        "retry_mode": retry_mode,
    }
    _append_execution_record(
        message,
        session_id=session_id,
        result=result,
        runtime_messages=runtime_messages,
        approved_workspace_result=approved_workspace_result,
        retry_source_execution_id=retry_source_execution_id,
    )
    return result


def handle_chat(
    message,
    session_id="default",
    approved_workspace_result=None,
    skip_workspace_approval=False,
    replay_runtime=None,
    retry_source_execution_id=None,
):
    session_summary = compaction_service.get_compact_summary(session_id)
    session_context = local_memory.get_session_context(session_id=session_id)

    if replay_runtime:
        runtime_messages = list(replay_runtime.get("messages", []))
        replay_model = replay_runtime.get("model")
        requested_lane = replay_runtime.get("requested_lane")
        resolved_lane = replay_runtime.get("resolved_lane")
        fallback_used = bool(replay_runtime.get("fallback_used"))
        approved_workspace_result = replay_runtime.get("approved_workspace_result")
        summary_included = bool(replay_runtime.get("summary_included"))
        workspace_inspection_used = approved_workspace_result.get("ok") if approved_workspace_result else False

        user_metadata = {
            "requested_lane": requested_lane,
            "resolved_lane": resolved_lane,
            "fallback_used": fallback_used,
            "route_confidence": replay_runtime.get("route_confidence"),
            "route_scores": replay_runtime.get("route_scores"),
            "route_primary_signal": replay_runtime.get("route_primary_signal"),
            "route_context_used": replay_runtime.get("route_context_used"),
            "route_context_reason": replay_runtime.get("route_context_reason"),
            "inference_message_count": len(runtime_messages),
            "summary_included": summary_included,
            "workspace_path": session_context.get("workspace_path"),
            "active_skills": list(session_context.get("active_skills", [])),
            "workspace_inspection_used": workspace_inspection_used,
            "retry_source_execution_id": retry_source_execution_id,
        }
        local_memory.append_message("user", message, session_id=session_id, metadata=user_metadata)

        if not replay_model:
            response = "Saved execution has no replayable model state. Run the request again normally instead."
            assistant_metadata = dict(user_metadata)
            assistant_metadata["error"] = "missing_replay_model"
            local_memory.append_message(
                "assistant",
                response,
                session_id=session_id,
                metadata=assistant_metadata,
            )
            return _complete_response(
                session_id=session_id,
                message=message,
                response=response,
                ok=False,
                model=None,
                requested_lane=requested_lane,
                resolved_lane=resolved_lane,
                fallback_used=fallback_used,
                error="missing_replay_model",
                route_confidence=replay_runtime.get("route_confidence"),
                route_scores=replay_runtime.get("route_scores"),
                route_primary_signal=replay_runtime.get("route_primary_signal"),
                route_context_used=replay_runtime.get("route_context_used"),
                route_context_reason=replay_runtime.get("route_context_reason"),
                inference_message_count=len(runtime_messages),
                summary_included=summary_included,
                approved_workspace_result=approved_workspace_result,
                retry_source_execution_id=retry_source_execution_id,
                retry_mode="fallback",
                runtime_messages=runtime_messages,
            )

        inference = runtime_client.infer_chat(replay_model, messages=runtime_messages)
        response = inference["content"] if inference["ok"] else f"[Error calling Ollama: {inference['error']}]"
        assistant_metadata = dict(user_metadata)
        if inference["error"]:
            assistant_metadata["error"] = inference["error"]
        local_memory.append_message(
            "assistant",
            response,
            model=replay_model,
            session_id=session_id,
            metadata=assistant_metadata,
        )
        return _complete_response(
            session_id=session_id,
            message=message,
            response=response,
            ok=inference["ok"],
            model=replay_model,
            requested_lane=requested_lane,
            resolved_lane=resolved_lane,
            fallback_used=fallback_used,
            error=inference["error"],
            route_confidence=replay_runtime.get("route_confidence"),
            route_scores=replay_runtime.get("route_scores"),
            route_primary_signal=replay_runtime.get("route_primary_signal"),
            route_context_used=replay_runtime.get("route_context_used"),
            route_context_reason=replay_runtime.get("route_context_reason"),
            inference_message_count=len(runtime_messages),
            summary_included=summary_included,
            approved_workspace_result=approved_workspace_result,
            retry_source_execution_id=retry_source_execution_id,
            retry_mode="exact",
            runtime_messages=runtime_messages,
        )

    if not skip_workspace_approval:
        pending_action = plan_workspace_action(
            message,
            session_context.get("workspace_path"),
            approved_search=session_context.get("approved_search"),
        )
        if pending_action:
            pending_action["original_message"] = message
            result = {
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
                "retried_from_execution_id": retry_source_execution_id,
                "retry_mode": None,
            }
            _append_execution_record(
                message,
                session_id=session_id,
                result=result,
                pending_action=pending_action,
                approved_workspace_result=approved_workspace_result,
                retry_source_execution_id=retry_source_execution_id,
            )
            return result

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
        "retry_source_execution_id": retry_source_execution_id,
    }
    local_memory.append_message("user", message, session_id=session_id, metadata=user_metadata)

    if not resolution["model"]:
        response = "No local model is ready yet. Install a registered model or start Ollama and try again."
        assistant_metadata = dict(user_metadata)
        assistant_metadata["resolved_lane"] = None
        assistant_metadata["fallback_used"] = False
        assistant_metadata["error"] = "no_model_available"
        local_memory.append_message(
            "assistant",
            response,
            session_id=session_id,
            metadata=assistant_metadata,
        )
        result = {
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
            "retried_from_execution_id": retry_source_execution_id,
            "retry_mode": None,
        }
        _append_execution_record(
            message,
            session_id=session_id,
            result=result,
            runtime_messages=runtime_messages,
            approved_workspace_result=approved_workspace_result,
            retry_source_execution_id=retry_source_execution_id,
        )
        return result

    inference = runtime_client.infer_chat(resolution["model"], messages=runtime_messages)
    response = inference["content"] if inference["ok"] else f"[Error calling Ollama: {inference['error']}]"

    assistant_metadata = dict(user_metadata)
    if inference["error"]:
        assistant_metadata["error"] = inference["error"]
    local_memory.append_message(
        "assistant",
        response,
        model=resolution["model"],
        session_id=session_id,
        metadata=assistant_metadata,
    )

    return _complete_response(
        session_id=session_id,
        message=message,
        response=response,
        ok=inference["ok"],
        model=resolution["model"],
        requested_lane=lane,
        resolved_lane=resolution["resolved_role"],
        fallback_used=resolution["fallback_used"],
        error=inference["error"],
        route_confidence=route_info["confidence"],
        route_scores=route_info["scores"],
        route_primary_signal=route_info["primary_signal"],
        route_context_used=route_info["context_used"],
        route_context_reason=route_info["context_reason"],
        inference_message_count=len(runtime_messages),
        summary_included=summary_included,
        approved_workspace_result=approved_workspace_result,
        retry_source_execution_id=retry_source_execution_id,
        retry_mode=None,
        runtime_messages=runtime_messages,
    )


def retry_saved_execution(execution, session_id="default"):
    if not execution:
        return {
            "ok": False,
            "session_id": session_id,
            "requested_lane": None,
            "resolved_lane": None,
            "model": None,
            "response": "Execution not found.",
            "fallback_used": False,
            "memory_saved": False,
            "error": "execution_not_found",
            "approval_required": False,
            "pending_action": None,
            "compaction_performed": False,
            "compaction_message": None,
            "compaction_summary_text": "",
            "compaction_result": None,
            "workspace_action_message": None,
            "retried_from_execution_id": None,
            "retry_mode": None,
        }

    replay = execution.get("replay") or {}
    runtime_messages = replay.get("messages") or []
    replay_model = replay.get("model")
    if replay_model and runtime_messages:
        return handle_chat(
            execution.get("message", ""),
            session_id=session_id,
            approved_workspace_result=replay.get("approved_workspace_result"),
            skip_workspace_approval=True,
            replay_runtime={
                "model": replay_model,
                "messages": runtime_messages,
                "approved_workspace_result": replay.get("approved_workspace_result"),
                "requested_lane": execution.get("requested_lane"),
                "resolved_lane": execution.get("resolved_lane"),
                "fallback_used": execution.get("fallback_used", False),
                "route_confidence": execution.get("route_confidence"),
                "route_scores": execution.get("route_scores"),
                "route_primary_signal": execution.get("route_primary_signal"),
                "route_context_used": execution.get("route_context_used"),
                "route_context_reason": execution.get("route_context_reason"),
                "summary_included": any(message.get("role") == "system" for message in runtime_messages[1:]),
            },
            retry_source_execution_id=execution.get("id"),
        )

    return handle_chat(
        execution.get("message", ""),
        session_id=session_id,
        approved_workspace_result=replay.get("approved_workspace_result"),
        skip_workspace_approval=bool(replay.get("approved_workspace_result")),
        retry_source_execution_id=execution.get("id"),
    )
