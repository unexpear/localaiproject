from app.models.model_registry import MODEL_REGISTRY, is_model_too_big, get_model_info_by_name
from app.models.runtime_client import get_ollama_version, pull_model


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


def ensure_default_models(installed_models):
    """Pull missing required models and return a list of (model, success, output)."""
    results = []
    ollama_version = get_ollama_version()

    for missing in get_missing_required_models(installed_models):
        name = missing["model"]
        info = get_model_info_by_name(name) or {}

        if is_model_too_big(name):
            results.append((name, False, "Model too large for available RAM."))
            continue

        if "requires_ollama_version" in info:
            if not ollama_version or not version_at_least(ollama_version, info["requires_ollama_version"]):
                results.append((name, False, f"Ollama {info['requires_ollama_version']}+ required for this model."))
                continue

        success, output = pull_model(name)
        if not success:
            print(f"  [Retry] Retrying pull for {name}...")
            success, output = pull_model(name)
        results.append((name, success, output))

    return results
