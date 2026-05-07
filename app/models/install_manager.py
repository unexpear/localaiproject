from app.models.model_registry import (
    MODEL_REGISTRY,
    get_installed_models,
    get_model_info_by_name,
    is_model_too_big,
)
from app.models.runtime_client import delete_model, get_ollama_version, pull_model


def _emit(progress_callback, event):
    if progress_callback is None:
        return
    try:
        progress_callback(event)
    except Exception:
        pass


def version_at_least(current, required):
    def parse(v):
        return [int(x) for x in v.split(".") if x.isdigit()]

    c, r = parse(current), parse(required)
    return c >= r


def get_missing_required_models(installed_models):
    missing = []
    for role, info in MODEL_REGISTRY.items():
        if info["required"] and info["model"] not in installed_models:
            missing.append(
                {
                    "role": role,
                    "model": info["model"],
                    "size_gb": info["size_gb"],
                }
            )
    return missing


def _is_cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def _pull_with_retry(name, progress_callback=None, cancel_event=None):
    success, output = pull_model(name, progress_callback=progress_callback, cancel_event=cancel_event)
    if not success and output != "cancelled" and not _is_cancelled(cancel_event):
        print(f"  [Retry] Retrying pull for {name}...")
        _emit(progress_callback, {"phase": "retry", "model": name})
        success, output = pull_model(name, progress_callback=progress_callback, cancel_event=cancel_event)
    return success, output


def _guard(name, ollama_version=None):
    """Return a (blocked, reason) tuple; blocked=True means skip the pull."""
    info = get_model_info_by_name(name) or {}
    if is_model_too_big(name):
        return True, "Model too large for available RAM."
    required_version = info.get("requires_ollama_version")
    if required_version:
        if not ollama_version or not version_at_least(ollama_version, required_version):
            return True, f"Ollama {required_version}+ required for this model."
    return False, None


def ensure_default_models(installed_models, progress_callback=None, cancel_event=None):
    """Pull missing required models and return a list of (model, success, output).

    Events emitted to progress_callback:
      {phase: "queue", models: [names]}                              before anything runs
      {phase: "start", model: name, index: i, total: n}              before each pull
      {phase: "blocked", model: name, reason: str}                   when guard blocks
      {phase: "done", model: name, success: bool, output: str}       after each pull
      {phase: "retry", model: name}                                  when retry fires
      {phase: "cancelled"}                                           when cancel_event fires
      plus all raw Ollama stream events (status/completed/total/...) during pulls.
    """
    results = []
    ollama_version = get_ollama_version()
    missing = get_missing_required_models(installed_models)
    _emit(progress_callback, {"phase": "queue", "models": [m["model"] for m in missing]})
    total = len(missing)

    for index, entry in enumerate(missing, start=1):
        if _is_cancelled(cancel_event):
            _emit(progress_callback, {"phase": "cancelled"})
            break
        name = entry["model"]
        _emit(progress_callback, {"phase": "start", "model": name, "index": index, "total": total})
        blocked, reason = _guard(name, ollama_version=ollama_version)
        if blocked:
            _emit(progress_callback, {"phase": "blocked", "model": name, "reason": reason})
            results.append((name, False, reason))
            _emit(progress_callback, {"phase": "done", "model": name, "success": False, "output": reason})
            continue
        success, output = _pull_with_retry(name, progress_callback=progress_callback, cancel_event=cancel_event)
        results.append((name, success, output))
        _emit(progress_callback, {"phase": "done", "model": name, "success": success, "output": output})

    return results


def resolve_install_target(target):
    """Accept a role name or a model name and return (role_or_None, model_name) or (None, None)."""
    if not target:
        return None, None

    cleaned = str(target).strip()
    if not cleaned:
        return None, None

    if cleaned in MODEL_REGISTRY:
        return cleaned, MODEL_REGISTRY[cleaned]["model"]

    for role, info in MODEL_REGISTRY.items():
        if info["model"] == cleaned:
            return role, cleaned

    return None, None


def install_model(target, installed_models=None, progress_callback=None, cancel_event=None):
    """Install a single registered model by role or model name.

    Returns {role, model, success, output, already_installed, warnings}.
    Progress events mirror ensure_default_models for a single-model run.
    """
    role, model_name = resolve_install_target(target)
    if not model_name:
        return {
            "role": None,
            "model": None,
            "success": False,
            "output": f"Unknown role or model '{target}'.",
            "already_installed": False,
            "warnings": [],
            "requested_target": target,
        }

    if installed_models is None:
        installed_models = get_installed_models()

    warnings = []
    if role == "unfiltered":
        warnings.append(
            "Unfiltered lane: this model has no safety filter. Use it deliberately for local, personal contexts only."
        )

    if model_name in installed_models:
        _emit(progress_callback, {"phase": "done", "model": model_name, "success": True, "output": "already installed"})
        return {
            "role": role,
            "model": model_name,
            "success": True,
            "output": "already installed",
            "already_installed": True,
            "warnings": warnings,
        }

    _emit(progress_callback, {"phase": "start", "model": model_name, "index": 1, "total": 1})
    blocked, reason = _guard(model_name, ollama_version=get_ollama_version())
    if blocked:
        _emit(progress_callback, {"phase": "blocked", "model": model_name, "reason": reason})
        _emit(progress_callback, {"phase": "done", "model": model_name, "success": False, "output": reason})
        return {
            "role": role,
            "model": model_name,
            "success": False,
            "output": reason,
            "already_installed": False,
            "warnings": warnings,
        }

    success, output = _pull_with_retry(model_name, progress_callback=progress_callback, cancel_event=cancel_event)
    _emit(progress_callback, {"phase": "done", "model": model_name, "success": success, "output": output})
    return {
        "role": role,
        "model": model_name,
        "success": success,
        "output": output,
        "already_installed": False,
        "warnings": warnings,
    }


def uninstall_model(target, installed_models=None):
    """Remove a single model by role or model name.

    Returns {role, model, success, output, not_installed}.
    """
    role, model_name = resolve_install_target(target)
    if not model_name:
        return {
            "role": None,
            "model": None,
            "success": False,
            "output": f"Unknown role or model '{target}'.",
            "not_installed": False,
            "requested_target": target,
        }

    if installed_models is None:
        installed_models = get_installed_models()

    if model_name not in installed_models:
        return {
            "role": role,
            "model": model_name,
            "success": False,
            "output": "not installed",
            "not_installed": True,
        }

    success, output = delete_model(model_name)
    return {
        "role": role,
        "model": model_name,
        "success": success,
        "output": output,
        "not_installed": False,
    }
