from app.hardware import summary as hardware_summary
from app.memory import local_memory
from app.models import model_registry
from app.services import compaction_service
from app.services.model_service import get_status
from app.services.skill_service import normalize_active_skills
from app.services.workspace_service import describe_pending_action, inspect_workspace


FIT_LABELS = {
    "safe": "safe",
    "tight": "tight (will run but uses most of RAM)",
    "too_big": "too big for local RAM",
    "unknown": "fit unknown",
}

#: Human labels for the GPU-vs-CPU placement hint (model_registry
#: Phase: hardware-aware fit). Only surfaced when it adds signal —
#: ``cpu`` and ``unknown`` are the silent default (no GPU info), so we
#: don't clutter the report with "runs on CPU" for every lane.
PLACEMENT_LABELS = {
    "gpu": "GPU-accelerated (fits VRAM)",
    "partial_offload": "partial GPU offload (slower — exceeds VRAM)",
}


def _format_ram(ram_bytes):
    if not ram_bytes:
        return "unknown"
    return f"{ram_bytes / (1024 ** 3):.1f} GB"


def _version_tuple(version):
    if not version:
        return ()
    parts = []
    for token in str(version).strip().split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_meets_requirement(current_version, required_version):
    if not required_version:
        return True
    current = _version_tuple(current_version)
    required = _version_tuple(required_version)
    if not current:
        return False
    width = max(len(current), len(required))
    current = current + (0,) * (width - len(current))
    required = required + (0,) * (width - len(required))
    return current >= required


def _lane_rows(status, ram_bytes=None, vram_bytes=None):
    installed_models = {entry["model"] for entry in status.get("installed", [])}
    current_version = status.get("version")
    rows = []
    fit_by_role = model_registry.get_hardware_lane_fit(
        ram_bytes=ram_bytes, vram_bytes=vram_bytes,
    )

    for role, info in model_registry.MODEL_REGISTRY.items():
        state = "ready" if info["model"] in installed_models else "missing required" if info["required"] else "optional, not installed"
        details = []
        required_version = info.get("requires_ollama_version")
        if required_version and not _version_meets_requirement(current_version, required_version):
            details.append(f"needs Ollama {required_version}+")
        fit = fit_by_role.get(role, {})
        if fit.get("tier") in {"tight", "too_big"}:
            details.append(FIT_LABELS[fit["tier"]])
        # Surface GPU placement only when it adds signal (a positive
        # GPU fit or a slow partial-offload warning). cpu/unknown stay
        # silent — they're the no-GPU-info default.
        placement_label = PLACEMENT_LABELS.get(fit.get("placement"))
        if placement_label:
            details.append(placement_label)
        rows.append(
            {
                "role": role,
                "model": info["model"],
                "state": state,
                "details": details,
                "fit": fit,
            }
        )

    return rows


def _summary_counts(summary):
    normalized = summary or {}
    return {
        "facts": len(normalized.get("facts", []) or []),
        "preferences": len(normalized.get("preferences", []) or []),
        "open_tasks": len(normalized.get("open_tasks", []) or []),
    }


def _next_moves(status, context, workspace_snapshot):
    suggestions = []
    missing_required = [entry["model"] for entry in status.get("missing", [])]

    if not status.get("runtime_available"):
        suggestions.append("Start Ollama so model discovery and chat calls can run.")
    if missing_required:
        suggestions.append("Run `install-defaults` to pull the missing required models.")

    workspace_path = context.get("workspace_path")
    if not workspace_path:
        suggestions.append("Use `workspace <path>` to pin the project this session should inspect.")
    elif not workspace_snapshot.get("ok"):
        suggestions.append("Re-pin the workspace with `workspace <path>` because the current path is unavailable.")

    pending_action = context.get("pending_approval")
    if pending_action:
        suggestions.append("Type `approve` to run the pending workspace action or `deny` to cancel it.")
    elif workspace_path and workspace_snapshot.get("ok"):
        suggestions.append("Use `search <query>` or `read <path>`, then `approve`, when you want scoped workspace inspection.")

    if not normalize_active_skills(context.get("active_skills")):
        suggestions.append("Optional: add a session skill with `skill add builder`, `skill add reviewer`, or `skill add planner`.")

    if not suggestions:
        suggestions.append("The control layer is ready. Ask for a task directly or run `doctor` when you want a deeper check.")

    return suggestions


def get_doctor_snapshot(session_id="default"):
    status = get_status()
    context = local_memory.get_session_context(session_id=session_id)
    summary = compaction_service.get_compact_summary(session_id=session_id)
    workspace_snapshot = inspect_workspace(context.get("workspace_path"))
    hardware = hardware_summary()
    return {
        "session_id": session_id,
        "status": status,
        "lanes": _lane_rows(
            status,
            ram_bytes=hardware.get("ram"),
            vram_bytes=hardware.get("vram"),
        ),
        "context": context,
        "summary": summary,
        "summary_counts": _summary_counts(summary),
        "workspace_snapshot": workspace_snapshot,
        "hardware": hardware,
        "next_moves": _next_moves(status, context, workspace_snapshot),
    }


def format_doctor_report(session_id="default"):
    snapshot = get_doctor_snapshot(session_id=session_id)
    status = snapshot["status"]
    context = snapshot["context"]
    workspace_snapshot = snapshot["workspace_snapshot"]
    summary_counts = snapshot["summary_counts"]
    hardware = snapshot["hardware"]
    lines = [
        "Control Layer Doctor",
        f"Session: {snapshot['session_id']}",
        "",
        "Runtime",
        f"- Ollama: {'connected' if status.get('runtime_available') else 'offline'}",
        f"- Version: {status.get('version') or 'unknown'}",
        "",
        "Hardware",
        f"- RAM: {_format_ram(hardware.get('ram'))}",
        f"- GPU: {hardware.get('gpu') or 'not detected'}",
        f"- VRAM: {_format_ram(hardware.get('vram')) if hardware.get('vram') else 'n/a (CPU or non-NVIDIA)'}",
        "",
        "Lanes",
    ]

    for lane in snapshot["lanes"]:
        fit = lane.get("fit") or {}
        size_gb = fit.get("size_gb")
        parts = list(lane["details"])
        if size_gb:
            parts.insert(0, f"{size_gb} GB")
        fit_tier = fit.get("tier")
        if fit_tier == "safe" and "safe" not in parts:
            parts.append("safe")
        suffix = f" ({', '.join(parts)})" if parts else ""
        lines.append(f"- {lane['role']} -> {lane['model']} [{lane['state']}]{suffix}")

    extras = [model for model in status.get("detected", []) if model not in {row["model"] for row in snapshot["lanes"]}]
    if extras:
        lines.append(f"- other local models: {', '.join(extras)}")

    lines.extend(
        [
            "",
            "Session Context",
            f"- Workspace: {context.get('workspace_path') or 'none'}",
            f"- Skills: {', '.join(normalize_active_skills(context.get('active_skills'))) or 'none'}",
            f"- Pending approval: {describe_pending_action(context.get('pending_approval')) if context.get('pending_approval') else 'none'}",
        ]
    )

    approved_search = context.get("approved_search")
    if approved_search:
        files = ", ".join((approved_search.get("files") or [])[:2]) or "none"
        lines.append(f"- Last approved search: {approved_search.get('query') or '(unknown query)'} -> {files}")
    else:
        lines.append("- Last approved search: none")

    lines.append(
        f"- Compact memory: facts {summary_counts['facts']}, preferences {summary_counts['preferences']}, open tasks {summary_counts['open_tasks']}"
    )

    if context.get("workspace_path"):
        lines.extend(["", "Workspace Snapshot"])
        if workspace_snapshot.get("ok"):
            lines.append(f"- Detected project shape: {workspace_snapshot.get('stack')}")
            markers = ", ".join(workspace_snapshot.get("markers", [])) or "none detected"
            top_level = ", ".join(workspace_snapshot.get("top_level", [])) or "(empty)"
            lines.append(f"- Markers: {markers}")
            lines.append(f"- Top level: {top_level}")
        else:
            lines.append(f"- Workspace unavailable: {workspace_snapshot.get('path') or context.get('workspace_path')}")

    lines.extend(["", "Suggested Next Moves"])
    for suggestion in snapshot["next_moves"]:
        lines.append(f"- {suggestion}")
    lines.append("- Workspace reads and searches remain approval-gated.")
    return "\n".join(lines)


def format_onboarding_guide(session_id="default"):
    snapshot = get_doctor_snapshot(session_id=session_id)
    status = snapshot["status"]
    context = snapshot["context"]
    summary_counts = snapshot["summary_counts"]
    hardware = snapshot["hardware"]
    ready_required = [lane["role"] for lane in snapshot["lanes"] if lane["state"] == "ready" and model_registry.MODEL_REGISTRY[lane["role"]]["required"]]
    missing_required = [lane["model"] for lane in snapshot["lanes"] if lane["state"] == "missing required"]
    fit_warnings = [
        f"{lane['role']} ({lane['model']}): {FIT_LABELS[lane['fit']['tier']]}"
        for lane in snapshot["lanes"]
        if (lane.get("fit") or {}).get("tier") in {"tight", "too_big"}
    ]

    lines = [
        "Onboard",
        "This app is a local AI control layer over your Ollama models. You ask once; the router picks a lane, the registry picks a model, and compact memory keeps continuity.",
        "",
        "Current Readiness",
        f"- Runtime: {'connected' if status.get('runtime_available') else 'offline'} ({status.get('version') or 'version unknown'})",
        f"- Detected RAM: {_format_ram(hardware.get('ram'))}",
        f"- Required lanes ready: {', '.join(ready_required) if ready_required else 'none yet'}",
        f"- Missing required models: {', '.join(missing_required) if missing_required else 'none'}",
        f"- Pinned workspace: {context.get('workspace_path') or 'none'}",
        f"- Compact memory entries: facts {summary_counts['facts']}, preferences {summary_counts['preferences']}, open tasks {summary_counts['open_tasks']}",
    ]

    if fit_warnings:
        lines.append("")
        lines.append("Hardware Fit Warnings")
        for warning in fit_warnings:
            lines.append(f"- {warning}")

    lines.extend(["", "Quick Start"])

    step_number = 1
    if not status.get("runtime_available"):
        lines.append(f"{step_number}. Start Ollama so the control layer can discover models and chat.")
        step_number += 1
    if missing_required:
        lines.append(f"{step_number}. Run `install-defaults` to download the required lanes.")
        step_number += 1
    if not context.get("workspace_path"):
        lines.append(f"{step_number}. Run `workspace <path>` so this session can inspect the right project.")
        step_number += 1

    lines.append(f"{step_number}. Ask for work directly, or use `search <query>` / `read <path>` and then `approve` for scoped workspace inspection.")
    lines.append(f"{step_number + 1}. Run `doctor` any time you want the deeper control-plane check.")
    lines.append("")
    lines.append("Workspace actions still require approval before any read or search runs.")
    return "\n".join(lines)
