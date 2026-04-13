from .runtime_client import list_ollama_models
from app.hardware import summary as hardware_summary


# Dict-based model registry: single source of truth
MODEL_REGISTRY = {
    "reasoning": {
        "model": "llama3.1:8b",
        "required": True,
        "size_gb": 8,
    },
    "coding": {
        "model": "qwen2.5-coder:7b",
        "required": True,
        "size_gb": 7,
    },
    "vision": {
        "model": "qwen3-vl",
        "required": False,
        "size_gb": 9,
        "requires_ollama_version": "0.12.7",
    },
    "unfiltered": {
        "model": "dolphin3:8b",
        "required": False,
        "size_gb": 8,
    },
}

ROLE_FALLBACKS = {
    "reasoning": ["reasoning", "coding"],
    "coding": ["coding", "reasoning"],
    "vision": ["vision", "reasoning", "coding"],
    "unfiltered": ["unfiltered", "reasoning", "coding"],
}


def get_installed_models():
    return set(list_ollama_models())


def get_model_status():
    """Return installed, missing, and optional registered models."""
    installed = get_installed_models()
    status = {"installed": [], "missing": [], "optional": [], "detected": sorted(installed)}
    for role, info in MODEL_REGISTRY.items():
        entry = {
            "role": role,
            "model": info["model"],
            "required": info["required"],
            "size_gb": info["size_gb"],
        }
        if info["model"] in installed:
            status["installed"].append(entry)
        elif info["required"]:
            status["missing"].append(entry)
        else:
            status["optional"].append(entry)
    return status


def get_model_for_role(role):
    """Return the preferred model name for a given role."""
    return MODEL_REGISTRY.get(role, {}).get("model")


def resolve_model_for_role(role):
    """Pick the best available installed model for the requested role."""
    installed = get_installed_models()
    for candidate_role in ROLE_FALLBACKS.get(role, [role, "reasoning", "coding"]):
        model_name = get_model_for_role(candidate_role)
        if model_name and model_name in installed:
            return {
                "requested_role": role,
                "resolved_role": candidate_role,
                "model": model_name,
                "fallback_used": candidate_role != role,
            }

    for candidate_role, info in MODEL_REGISTRY.items():
        if info["model"] in installed:
            return {
                "requested_role": role,
                "resolved_role": candidate_role,
                "model": info["model"],
                "fallback_used": candidate_role != role,
            }

    return {
        "requested_role": role,
        "resolved_role": None,
        "model": None,
        "fallback_used": False,
    }


def get_model_info_by_name(name):
    for role, info in MODEL_REGISTRY.items():
        if info["model"] == name:
            return info
    return None


def is_model_too_big(model_name):
    info = get_model_info_by_name(model_name)
    if not info:
        return False
    ram = hardware_summary().get("ram")
    if ram is None:
        return False
    return info["size_gb"] * (1024 ** 3) > ram * 0.8
