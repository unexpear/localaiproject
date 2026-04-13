from app.models import model_registry, install_manager
from app.models.runtime_client import detect_ollama, get_ollama_version


def get_status():
    status = model_registry.get_model_status()
    status["runtime_available"] = detect_ollama()
    status["version"] = get_ollama_version()
    return status


def get_missing_required_models():
    return get_status().get("missing", [])


def ensure_models_installed():
    installed = [m["model"] for m in model_registry.get_model_status().get("installed", [])]
    return install_manager.ensure_default_models(installed)


def format_status():
    status = get_status()
    lines = [
        "Local Runtime",
        f"Ollama reachable: {'yes' if status['runtime_available'] else 'no'}",
        f"Version: {status['version'] or 'unknown'}",
        "",
        "Registered Models",
    ]

    for label, bucket in (
        ("Installed", "installed"),
        ("Missing required", "missing"),
        ("Optional", "optional"),
    ):
        entries = status.get(bucket, [])
        if not entries:
            lines.append(f"{label}: none")
            continue
        rendered = ", ".join(f"{entry['role']}={entry['model']}" for entry in entries)
        lines.append(f"{label}: {rendered}")

    registered = {info["model"] for info in model_registry.MODEL_REGISTRY.values()}
    extras = [model for model in status.get("detected", []) if model not in registered]
    if extras:
        lines.append(f"Other local models: {', '.join(extras)}")

    return "\n".join(lines)


def format_install_results(results):
    if not results:
        return "Default model sync: nothing to do."

    lines = ["Default model sync"]
    for model_name, success, output in results:
        state = "ready" if success else "skipped"
        lines.append(f"- {model_name}: {state} ({output})")
    return "\n".join(lines)


def startup_summary():
    status = get_status()
    installed_roles = [entry["role"] for entry in status.get("installed", [])]
    missing_required = [entry["model"] for entry in status.get("missing", [])]

    lines = [
        "=== Local AI Control Layer ===",
        f"Ollama: {'connected' if status['runtime_available'] else 'offline'}",
    ]
    if status["version"]:
        lines.append(f"Runtime version: {status['version']}")

    if installed_roles:
        lines.append(f"Ready lanes: {', '.join(installed_roles)}")
    else:
        lines.append("Ready lanes: none yet")

    if missing_required:
        lines.append(f"Missing defaults: {', '.join(missing_required)}")

    lines.extend(
        [
            "",
            "Commands: help, status, history [n], workspace, read, search, approve, deny, skills, sessions, session <name>, install-defaults, exit",
        ]
    )
    return "\n".join(lines)


def prompt_for_missing_models(input_func=input, print_func=print):
    status = get_status()
    missing_required = status.get("missing", [])

    if not missing_required:
        return []

    if not status["runtime_available"]:
        print_func("Missing required models detected, but Ollama is not reachable, so downloads are unavailable right now.")
        return []

    model_list = ", ".join(entry["model"] for entry in missing_required)
    print_func(f"Missing required models: {model_list}")

    try:
        answer = input_func("Download the missing required models now? [Y/n]: ").strip().lower()
    except EOFError:
        print_func("Skipping downloads because startup input is unavailable.")
        return []
    except KeyboardInterrupt:
        print_func("\nSkipping downloads.")
        return []

    if answer not in {"", "y", "yes"}:
        print_func("Skipping model downloads for now.")
        return []

    results = ensure_models_installed()
    print_func(format_install_results(results))
    return results
