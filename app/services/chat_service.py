from app.config import INFERENCE_HISTORY_WINDOW, ROUTER_HISTORY_WINDOW
from app.memory import local_memory
from app.models import model_registry, runtime_client
from app.services import chat_providers, compaction_service, project_context_service, prompt_assembly
from app.services.execution_service import build_execution_record
from app.services.mentions_service import expand_mentions, format_expansion_summary
from app.services.skill_service import build_skill_system_message, get_lane_biases
from app.services.workspace_service import (
    build_workspace_inspection_system_message,
    build_workspace_system_message,
    describe_pending_action,
    format_approval_request,
    parse_fenced_writes,
    plan_workspace_action,
    resolve_workspace_write_path,
    sha256_text,
    read_existing_for_diff,
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


def _maybe_chat_write_proposal(message, response_text, session_id, session_context):
    """Detect chat-flow proposals in the assistant's response and
    build a ``pending_approval`` action.

    Two block types are recognised:

    * **``file=path`` fenced blocks** — workspace writes (Phase 2 of
      ``workspace-writes.md``). One valid block → single
      ``kind="write"``; N>1 → ``kind="bundle"`` (Phase 1 of
      ``patch-taxonomy.md``). Requires Build mode + a pinned
      workspace.
    * **``dispatcher`` fenced blocks** — tool calls (Phase 3 of
      ``tool-calling.md``). Returns ``kind="tool_call"`` with the
      single first command. Requires ``tool_calls_enabled`` on the
      session context. No workspace / mode gate — those are
      enforced per-command at apply time by the recursive
      ``dispatch_input``.

    Writes WIN when both kinds are present (more concrete + more
    safety machinery). Tool calls are still discoverable via
    ``tool show``. Phase 3b can compose write + tool-call into a
    bundle.

    Returns the pending action dict (already shape-validated) or
    None. The caller plumbs it into the result so the dispatcher's
    ``_run_one_inbox_task`` recognises the approval pause.
    """
    # Don't clobber an already-pending approval.
    if local_memory.get_pending_approval(session_id=session_id):
        return None
    if not response_text:
        return None
    workspace_path = (session_context or {}).get("workspace_path")
    mode = (session_context or {}).get("mode")
    tool_calls_enabled = bool((session_context or {}).get("tool_calls_enabled"))

    # ---- Try workspace writes first (writes win when both present) ----
    write_action = _maybe_workspace_write_proposal(
        message, response_text, mode, workspace_path,
    )
    if write_action is not None:
        return write_action

    # ---- Fall through to tool calls when enabled ----
    if tool_calls_enabled:
        return _maybe_tool_call_proposal(message, response_text)
    return None


def _maybe_workspace_write_proposal(message, response_text, mode, workspace_path):
    """Internal: extract ``file=path`` writes from the response.
    Same logic as the legacy single-function version. Returns the
    pending-action dict or None."""
    if mode != "build":
        return None
    if not workspace_path:
        return None
    candidates = parse_fenced_writes(response_text)
    if not candidates:
        return None

    valid_actions = []
    for cand in candidates:
        resolved = resolve_workspace_write_path(workspace_path, cand["path"])
        if not resolved.get("ok"):
            continue
        existing, existed, _err = read_existing_for_diff(resolved["absolute_path"])
        action = {
            "kind": "write",
            "origin": "chat",
            "workspace_path": workspace_path,
            "path": resolved["relative_path"],
            "content": cand["content"],
            "create": not existed,
            "original_message": message,
            "reason": "chat-flow fenced block",
        }
        if existing is not None:
            action["previous_content_hash"] = sha256_text(existing)
        valid_actions.append(action)

    if not valid_actions:
        return None

    if len(valid_actions) == 1:
        return valid_actions[0]

    from app.services import patches
    bundle = patches.bundle_from_action_dicts(
        valid_actions,
        rationale="chat-flow multi-file change",
        origin="chat",
    )
    if bundle is None:
        return valid_actions[0]
    bundle["original_message"] = message
    return bundle


def _maybe_tool_call_proposal(message, response_text):
    """Internal: extract ```dispatcher fenced blocks from the
    response.

    * **One call**: returns a single ``kind="tool_call"`` action.
    * **N>1 calls** (Phase 3b of tool-calling.md): wraps them as
      a ``kind="bundle"`` whose ``sub_actions`` carry every call.
      User approves once → all N dispatch in order. Fail-stop on
      the first error (no rollback machinery for tool calls).
    """
    from app.services import tool_calls as _tc
    from app.services import patches
    calls = _tc.parse_tool_calls(response_text)
    if not calls:
        return None
    if len(calls) == 1:
        return {
            "kind": "tool_call",
            "origin": "chat",
            "command": calls[0].command,
            "original_message": message,
            "reason": "chat-flow ```dispatcher block",
        }

    # N>1 — build a bundle of tool_call sub-actions.
    sub_actions = [
        {
            "kind": "tool_call",
            "origin": "chat",
            "command": call.command,
        }
        for call in calls
    ]
    bundle = patches.bundle_from_action_dicts(
        sub_actions,
        rationale=f"chat-flow {len(calls)} ```dispatcher blocks",
        origin="chat",
    )
    if bundle is None:
        # Bundle helper refused (oversize) — fall back to first call.
        return {
            "kind": "tool_call",
            "origin": "chat",
            "command": calls[0].command,
            "original_message": message,
            "reason": "chat-flow ```dispatcher block",
        }
    bundle["original_message"] = message
    return bundle


def _resolve_chat_provider(session_id, resolved_lane, fallback_model):
    """Pick the (provider_id, model) tuple for a chat turn.

    Returns ``(provider_id, model, source)`` where ``source`` is one of
    ``"binding"`` (a per-session lane→provider binding), ``"forbidden"``
    (a binding existed but a forbid_provider rule overrode it), or
    ``"default"`` (no binding — use the resolution from the registry).

    See ``chat-providers.md`` and ``agent-rules.md``.
    """
    spec = local_memory.get_lane_provider(
        session_id=session_id, lane=resolved_lane or ""
    )
    if not spec:
        return "ollama", fallback_model, "default"
    try:
        provider_id, model = chat_providers.parse_lane_binding(spec)
    except ValueError:
        # Stored binding is malformed — fall back to ollama silently
        # rather than aborting the whole turn.
        return "ollama", fallback_model, "default"

    # Phase 1 forbid_provider activation: if any active rule excludes
    # this provider for the session's tags, strip the binding and fall
    # back to ollama.
    forbidden = _check_forbid_provider(session_id, provider_id)
    if forbidden:
        return "ollama", fallback_model, "forbidden"
    return provider_id, model, "binding"


def _check_forbid_provider(session_id, provider_id):
    """Return True if any active rule forbids `provider_id` for this session.

    Lazy-imports rule_service to avoid a circular dependency.
    """
    try:
        from app.services import rule_service
    except ImportError:
        return False
    try:
        tags = local_memory.get_tags(session_id=session_id) or []
    except Exception:  # noqa: BLE001
        tags = []
    for rule in rule_service.list_rules():
        if rule_service.is_disabled(rule.id):
            continue
        if "forbid_provider" not in rule.then:
            continue
        # Match the rule's `when` predicate against a chat-style event.
        candidate = {
            "event_kind": "dispatch",
            "action": "chat",
            "session_id": session_id,
            "text": "",
        }
        if not rule_service._matches_predicate(rule, candidate, tags):
            continue
        spec = rule.then["forbid_provider"]
        allowed = list(spec.get("not_in") or spec.get("allow_only") or [])
        if provider_id not in allowed:
            return rule.id
    return False


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


def _mode_system_message(mode, *, compact=False):
    """Tell the model whether it can suggest edits or only read
    context. ``compact=True`` (Phase 1 of compact-prompt.md)
    collapses the verbose Plan-mode paragraph (which lists every
    refusal case) down to a one-liner. The model already knows
    what "Plan mode" means; the explicit refusals are for the
    paranoid case."""
    if mode == "build":
        if compact:
            content = "Mode: build (writes need user approval)."
        else:
            content = (
                "Mode: BUILD. You may propose concrete file edits, new files, and workspace actions. "
                "All write/create suggestions still require user approval before they run."
            )
        return {"role": "system", "content": content}
    # Default: plan
    if compact:
        content = "Mode: plan (read-only — no file edits or workspace writes)."
    else:
        content = (
            "Mode: PLAN. Read and analyse only. Do not propose new files, file edits, or workspace "
            "writes — restrict yourself to explanation, design, search, and read-only context. "
            "If the user asks for an edit, describe the change in prose; do not produce diffs or "
            "commands that mutate the workspace."
        )
    return {"role": "system", "content": content}


def build_runtime_assembly(
    summary,
    recent_history,
    current_message,
    session_context=None,
    approved_workspace_result=None,
    mentions_context_block=None,
    session_id=None,
    resolved_lane=None,
):
    """Build the runtime message list AND a per-slot trace.

    See ``docs/design/prompt-assembly.md``. Phase 1 of the layered
    assembly: every system-message contribution is registered as a
    typed ``PromptSlot``; the assembler emits the slots in priority
    order and produces a parallel ``trace`` listing
    ``{name, chars, priority, char_budget, truncated, evicted, …}``.

    Phase 2: per-slot ``char_budget`` truncates a single oversize
    slot in-place; ``total_budget`` evicts the lowest-priority slots
    until the assembled system-message bytes fit. Both are configurable
    via session context:

    - ``session.context["prompt_budgets"]`` — name → char_budget
      override, merged on top of ``SLOT_BUDGET_DEFAULTS``.
    - ``session.context["prompt_total_budget"]`` — total cap. Falls
      back to ``DEFAULT_TOTAL_BUDGET`` (24 KB) if unset.

    Trailing chat history + the current user message are appended
    after the assembled system messages — they're not part of the
    slot system. Returns ``(messages, trace)``.
    """
    context = session_context or {}

    total_budget = context.get("prompt_total_budget")
    if total_budget is None:
        total_budget = prompt_assembly.DEFAULT_TOTAL_BUDGET
    assembly = prompt_assembly.PromptAssembly(total_budget=int(total_budget or 0))

    # Phase 1 of compact-prompt.md — opt-in dense slot rendering.
    # When set, each slot producer that supports it strips its
    # verbose framing in favour of a tight ``## Header\n<body>``
    # form. The flag is per-session (see ``prompt compact``) and
    # composes additively with per-slot ``char_budget`` truncation.
    compact = bool(context.get("compact_format"))

    assembly.add(prompt_assembly.control_slot(CONTROL_SYSTEM_MESSAGE))
    assembly.add(prompt_assembly.mode_slot(_mode_system_message(
        context.get("mode") or "plan", compact=compact,
    )))
    # Phase 1 of custom-instructions.md — typed, conditional,
    # addressable behavioural rules. Filtered by the entry's own
    # ``applies_to`` predicate against the runtime context (mode,
    # workspace, tags, resolved lane). Empty list → slot
    # contributes nothing; the default for fresh sessions.
    assembly.add(prompt_assembly.custom_instructions_slot(
        context.get("instructions") or [],
        mode=context.get("mode"),
        workspace_path=context.get("workspace_path"),
        tags=context.get("tags") or [],
        lane=resolved_lane,
        compact=compact,
    ))
    # Phase 2 of tool-calling.md — opt-in slot teaching the model
    # the ``​```dispatcher``` fence format. Off by default; the
    # user enables via ``tool enable on``.
    assembly.add(prompt_assembly.tool_calls_slot(
        bool(context.get("tool_calls_enabled"))
    ))
    # Phase 3 — project-rooted persona + AGENTS.md (Hermes-inspired).
    # Both files are read fresh on every chat turn from the pinned
    # workspace root; missing files contribute nothing.
    workspace_path = context.get("workspace_path")
    assembly.add(prompt_assembly.persona_slot(
        project_context_service.read_persona(workspace_path),
        compact=compact,
    ))
    assembly.add(prompt_assembly.project_context_slot(
        project_context_service.read_project_agents(workspace_path),
        compact=compact,
    ))
    assembly.add(prompt_assembly.skills_slot(build_skill_system_message(context.get("active_skills"))))
    assembly.add(prompt_assembly.workspace_slot(build_workspace_system_message(context.get("workspace_path"))))
    assembly.add(prompt_assembly.inspection_slot(build_workspace_inspection_system_message(approved_workspace_result)))
    assembly.add(prompt_assembly.mentions_slot(mentions_context_block))
    # Phase 2b of recall-memory.md: BM25 retrieval over the
    # workspace, gated on a cached index (the user opts in via
    # `retrieve build`). Falls through silently when not cached.
    assembly.add(prompt_assembly.retrieved_docs_slot(
        current_message,
        session_id=session_id,
        workspace_path=context.get("workspace_path"),
    ))
    assembly.add_all(prompt_assembly.memory_block_slots(
        context.get("memory_blocks") or {}, compact=compact,
    ))
    assembly.add(prompt_assembly.summary_slot(compaction_service.build_summary_system_message(summary)))

    # Apply per-slot defaults, then layer session overrides on top.
    budgets = dict(prompt_assembly.SLOT_BUDGET_DEFAULTS)
    overrides = context.get("prompt_budgets") or {}
    if isinstance(overrides, dict):
        budgets.update({k: int(v or 0) for k, v in overrides.items()})
    assembly.apply_budgets(budgets)

    system_messages, trace = assembly.assemble()
    # When the pinned slots alone exceed total_budget, eviction
    # can't recover. Append a synthetic ``_diagnostics`` entry so
    # consumers (handle_chat result, dispatcher footer) can surface
    # the ``prompt_budget_exceeded`` warning. Skipped when there's
    # nothing to flag — keeps the trace one-per-slot in the
    # common case.
    active_total = assembly.total_budget
    rendered = prompt_assembly.live_total_chars(trace)
    over_budget = active_total > 0 and rendered > active_total
    if over_budget:
        trace = list(trace) + [{
            "name": "_diagnostics",
            "chars": rendered,
            "priority": 999,
            "char_budget": active_total,
            "truncated": False,
            "evicted": False,
            "evicted_reason": "",
            "total_budget": active_total,
            "over_budget": True,
        }]
    messages = list(system_messages)
    messages.extend(_history_to_runtime_messages(recent_history, limit=INFERENCE_HISTORY_WINDOW))
    messages.append({"role": "user", "content": current_message})
    return messages, trace


def _build_runtime_messages(
    summary,
    recent_history,
    current_message,
    session_context=None,
    approved_workspace_result=None,
    mentions_context_block=None,
    session_id=None,
):
    """Legacy entry point — returns just ``messages``. Prefer
    ``build_runtime_assembly`` when the caller wants the per-slot
    trace too."""
    messages, _trace = build_runtime_assembly(
        summary,
        recent_history,
        current_message,
        session_context=session_context,
        session_id=session_id,
        approved_workspace_result=approved_workspace_result,
        mentions_context_block=mentions_context_block,
    )
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
    provider_id="ollama",
    provider_source="default",
    write_proposal=None,
    prompt_assembly_trace=None,
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
        "approval_required": bool(write_proposal),
        "pending_action": write_proposal,
        "compaction_performed": compaction_result.get("performed", False),
        "compaction_message": compaction_message,
        "compaction_summary_text": compaction_summary_text,
        "compaction_result": compaction_result,
        "workspace_action_message": (
            f"chat-flow write proposal: {write_proposal['path']!r}"
            if write_proposal else None
        ),
        "retried_from_execution_id": retry_source_execution_id,
        "retry_mode": retry_mode,
        "provider_id": provider_id,
        "provider_source": provider_source,
        "prompt_assembly": list(prompt_assembly_trace or []),
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
                "prompt_assembly": [],
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
    # Phase 5b of agent-rules.md: when the user has opted into
    # ``classifier autoroute``, blend the learned NB classifier's
    # prediction into the rule-router's lane_biases. Skill biases
    # (from active_skills) are the baseline; classifier votes
    # add on top with confidence-weighted strength.
    lane_biases = get_lane_biases(session_context.get("active_skills"))
    if session_context.get("classifier_autoroute"):
        from app.services import lane_classifier as _lc
        for lane, bonus in _lc.classifier_lane_biases(
            message, session_id=session_id,
        ).items():
            lane_biases[lane] = lane_biases.get(lane, 0) + bonus

    # Phase 3b of vision-input.md: when the user types
    # ``@image:<path>`` tokens, bias the router toward the
    # ``vision`` lane proportional to the count (capped). Counts
    # INTENT — actual attachment success is checked later by the
    # capability gate (Phase 3a). The router still uses the raw
    # ``message`` (with @image: tokens intact) for keyword
    # scoring, so this bias is purely additive.
    from app.services import vision_service as _vs
    _vision_bias = _vs.vision_lane_bias(message)
    if _vision_bias > 0:
        lane_biases["vision"] = lane_biases.get("vision", 0) + _vision_bias

    route_info = router.route_message(
        message,
        recent_history=router_history,
        lane_biases=lane_biases,
    )
    lane = route_info["lane"]
    resolution = model_registry.resolve_model_for_role(lane)

    inference_history = local_memory.get_history(session_id=session_id, limit=INFERENCE_HISTORY_WINDOW)

    # Phase 1 of vision-input.md: extract @image:<path> attachments
    # from the message FIRST — before mentions_service runs — so
    # the generic @<path> regex doesn't shadow the more-specific
    # @image: form. Same consent model as @file mentions (user
    # typed it = explicit). Stripped tokens become [image:foo]
    # placeholders the model sees alongside the base64 image in
    # the user message's `images` field.
    from app.services import vision_service
    image_expansion = vision_service.attach_image_mentions(
        message, session_context.get("workspace_path"),
    )
    pre_mention_text = (
        image_expansion["stripped"]
        if image_expansion["original"] != image_expansion["stripped"]
        else message
    )

    # Expand @file mentions: bypasses workspace approval (user-explicit consent),
    # always read-only, bounded by mentions_service byte limits.
    mention_expansion = expand_mentions(pre_mention_text, session_context.get("workspace_path"))
    runtime_message = mention_expansion["stripped"] if mention_expansion["expanded"] else pre_mention_text

    runtime_messages, prompt_trace = build_runtime_assembly(
        session_summary,
        inference_history,
        runtime_message,
        session_context=session_context,
        approved_workspace_result=approved_workspace_result,
        mentions_context_block=mention_expansion["context_block"],
        session_id=session_id,
        resolved_lane=resolution.get("resolved_role") or lane,
    )

    # Phase 3 of vision-input.md: capability gate. If the resolved
    # model isn't vision-capable, strip the images from the
    # runtime payload BEFORE attaching — sending them to a model
    # that can't see them wastes upload time and clutters
    # context. The user sees the rejection inline via
    # `image_attachments` metadata flagged with
    # ``error="non_vision_model"``.
    if image_expansion["expanded"]:
        resolved_model = resolution.get("model") or ""
        if not model_registry.is_vision_capable(resolved_model):
            for att in image_expansion["attachments"]:
                if att.get("ok"):
                    att["ok"] = False
                    att["error"] = "non_vision_model"
                    # Drop the base64 payload from the cached
                    # attachment now that it won't be applied.
                    att["base64_data"] = ""
            for meta in image_expansion["metadata"]:
                if meta.get("ok"):
                    meta["ok"] = False
                    meta["error"] = "non_vision_model"
            image_expansion["expanded"] = False
            image_expansion["summary"] = (
                f"image attachments dropped — resolved model "
                f"{resolved_model!r} is not vision-capable"
            )

    # Attach base64 images to the trailing user message in the
    # runtime payload. No-op when there are no successful
    # attachments (failed-to-load attachments are surfaced via
    # `image_attachments` on the result for the user to see, but
    # don't make it into the model payload).
    if image_expansion["expanded"]:
        vision_service.apply_to_messages(
            runtime_messages, image_expansion["attachments"],
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
        # Vision-input Phase 1: metadata-only view of attempted
        # image attachments (no base64 to keep history compact).
        "image_attachments": image_expansion["metadata"],
    }
    local_memory.append_message("user", message, session_id=session_id, metadata=user_metadata)

    # Resolve the active provider for this turn. A non-default
    # `provider_source` means a per-session binding (or a forbid rule)
    # was applied; record it on metadata + the result so consumers can
    # see which provider answered.
    provider_id, provider_model, provider_source = _resolve_chat_provider(
        session_id, resolution["resolved_role"], resolution["model"]
    )
    user_metadata["provider_id"] = provider_id
    user_metadata["provider_source"] = provider_source

    # When a non-ollama provider is bound, we don't need a local Ollama
    # model installed. Skip the no-model error path in that case.
    if provider_id == "ollama" and not provider_model:
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
            "prompt_assembly": list(prompt_trace or []),
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

    inference = chat_providers.dispatch_chat(
        provider_id, provider_model, messages=runtime_messages
    )
    if inference["ok"]:
        response = inference["content"]
    else:
        # Use a provider-aware error string so users know which
        # provider failed (Ollama vs OpenAI vs Anthropic).
        provider_label = chat_providers.REGISTRY.get(provider_id)
        provider_label = provider_label.label if provider_label else provider_id
        response = f"[Error calling {provider_label}: {inference['error']}]"

    assistant_metadata = dict(user_metadata)
    if inference["error"]:
        assistant_metadata["error"] = inference["error"]
    local_memory.append_message(
        "assistant",
        response,
        model=provider_model,
        session_id=session_id,
        metadata=assistant_metadata,
    )

    # Phase 2 chat-flow detection: if the assistant's response contains
    # a ``file=path`` fenced block, propose a workspace write. The
    # proposal is gated on Build mode + a pinned workspace + no other
    # pending approval; mismatches return None and the response passes
    # through unchanged.
    write_proposal = None
    if inference["ok"] and not skip_workspace_approval:
        write_proposal = _maybe_chat_write_proposal(
            message=message,
            response_text=response,
            session_id=session_id,
            session_context=session_context,
        )

    # Phase 2.5 of custom-instructions.md — decrement every positive
    # ``hushed_remaining`` so a `--turns N` hush expires after exactly
    # N turns. No-op when no entry is hushed (the common case). Fires
    # on success AND failure: a failed turn still saw the prompt with
    # the entry filtered, so the hush did its job.
    local_memory.tick_hushed_instructions(session_id=session_id)

    return _complete_response(
        session_id=session_id,
        message=message,
        response=response,
        ok=inference["ok"],
        model=provider_model,
        requested_lane=lane,
        resolved_lane=resolution["resolved_role"],
        fallback_used=resolution["fallback_used"],
        error=inference["error"],
        provider_id=provider_id,
        provider_source=provider_source,
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
        write_proposal=write_proposal,
        prompt_assembly_trace=prompt_trace,
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
            "prompt_assembly": [],
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
