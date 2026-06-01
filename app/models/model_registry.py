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
        # Phase 3 of vision-input.md — explicit capability flag.
        # The chat-flow handler uses this to decide whether to
        # actually attach images to the runtime payload.
        "vision_capable": True,
    },
    "unfiltered": {
        "model": "dolphin3:8b",
        "required": False,
        "size_gb": 8,
    },
}


#: Heuristic: model names that contain any of these substrings are
#: considered vision-capable even when not explicitly in the
#: registry. Ollama users routinely install vision models we
#: haven't curated (llama3.2-vision, qwen2.5-vl, llava:34b, etc.)
#: and the cost of a wrong heuristic is only that we send images
#: to a model that ignores them — same as the pre-Phase-3
#: behaviour. The cost of being too strict (false negatives) is
#: refusing useful queries; lean permissive.
_VISION_NAME_HEURISTICS = (
    "vision",     # llama3.2-vision, llama3.3-vision
    "-vl",        # qwen2-vl, qwen2.5-vl, qwen3-vl
    ":vl",        # alt tag form
    "llava",      # llava, llava:34b, etc.
    "moondream",  # moondream / moondream2
    "bakllava",
    "minicpm-v",  # minicpm-v family
    "pixtral",
)


def is_vision_capable(model_name):
    """Return True iff ``model_name`` is known to accept image
    inputs via Ollama's ``messages[*].images`` field.

    Resolution order:
    1. Explicit ``vision_capable: True`` flag in MODEL_REGISTRY.
    2. Substring heuristic against ``_VISION_NAME_HEURISTICS``.

    The heuristic is deliberately permissive — false positives
    (sending images to a model that ignores them) are cheap;
    false negatives (refusing useful queries) are annoying.
    """
    if not model_name or not isinstance(model_name, str):
        return False
    # Explicit registry flag wins over the heuristic.
    for info in MODEL_REGISTRY.values():
        if info.get("model") == model_name and info.get("vision_capable"):
            return True
    name = model_name.lower()
    return any(kw in name for kw in _VISION_NAME_HEURISTICS)

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
    return classify_hardware_fit(model_name).get("tier") == "too_big"


#: Fraction of VRAM the model weights may occupy and still count as a
#: comfortable full-GPU fit. Leaves ~15% for KV cache + activations at
#: modest context lengths. Above this, Ollama spills layers to CPU
#: (partial offload) — it still runs, just slower. (Adapted concept:
#: hardware-aware fit scoring, as seen in AlexsJones/llmfit and
#: PewDiePie's Odysseus. We deliberately keep ONLY the VRAM-vs-RAM
#: placement axis — Ollama owns quantisation + layer offload, so the
#: full quant/bandwidth scoring those tools do doesn't apply here.)
VRAM_FIT_CEILING = 0.85


def _classify_placement(size_bytes, vram_bytes, tier):
    """Return ``(placement, vram_ratio)`` — the GPU-vs-CPU dimension a
    RAM-only check misses. ``vram_ratio`` is None when VRAM is unknown.

    Placement values:
      gpu             -> fits in detected VRAM (fully offloaded, fast)
      partial_offload -> exceeds VRAM but fits RAM (some layers on CPU)
      cpu             -> no GPU VRAM detected; runs on CPU/RAM
      too_big         -> exceeds RAM (won't run well anywhere)
    """
    if tier == "too_big":
        return "too_big", (size_bytes / vram_bytes if vram_bytes else None)
    if vram_bytes and vram_bytes > 0:
        vram_ratio = size_bytes / vram_bytes
        if vram_ratio <= VRAM_FIT_CEILING:
            return "gpu", vram_ratio
        return "partial_offload", vram_ratio
    # No VRAM detected — assume CPU/RAM execution (prior behaviour).
    return "cpu", None


def classify_hardware_fit(model_name, ram_bytes=None, vram_bytes=None):
    """Classify how well a model fits local hardware.

    RAM tiers (unchanged — ``is_model_too_big`` + callers depend on these):
      safe    -> model ≤ 40% of RAM (comfortable headroom for KV cache and OS)
      tight   -> 40%–80% of RAM (will run but leaves little slack)
      too_big -> > 80% of RAM
      unknown -> RAM not detectable or model not registered

    Plus a ``placement`` hint distinguishing fast GPU inference from
    slow CPU offload — the dimension a RAM-only check misses (see
    ``_classify_placement``). ``vram_bytes`` is auto-probed from the
    hardware summary ONLY on the live path (when ``ram_bytes`` is also
    None); callers that inject ``ram_bytes`` (e.g. tests) stay hermetic
    and get ``placement="cpu"`` unless they inject ``vram_bytes`` too.
    """
    info = get_model_info_by_name(model_name)
    if not info:
        return {
            "tier": "unknown", "placement": "unknown", "size_gb": None,
            "ram_bytes": ram_bytes, "vram_bytes": vram_bytes,
            "reason": "unregistered model",
        }

    size_gb = info["size_gb"]
    size_bytes = size_gb * (1024 ** 3)

    if ram_bytes is None:
        summary = hardware_summary()
        ram_bytes = summary.get("ram")
        # Single probe on the live path: pick up VRAM from the same
        # summary so we don't fire a second nvidia-smi subprocess.
        if vram_bytes is None:
            vram_bytes = summary.get("vram")
    if not ram_bytes:
        return {
            "tier": "unknown", "placement": "unknown", "size_gb": size_gb,
            "ram_bytes": None, "vram_bytes": vram_bytes,
            "reason": "RAM not detected",
        }

    ratio = size_bytes / ram_bytes
    if ratio > 0.8:
        tier = "too_big"
    elif ratio > 0.4:
        tier = "tight"
    else:
        tier = "safe"

    placement, vram_ratio = _classify_placement(size_bytes, vram_bytes, tier)

    result = {
        "tier": tier,
        "placement": placement,
        "size_gb": size_gb,
        "ram_bytes": ram_bytes,
        "vram_bytes": vram_bytes,
        "ratio": ratio,
    }
    if vram_ratio is not None:
        result["vram_ratio"] = vram_ratio
    return result


def get_hardware_lane_fit(ram_bytes=None, vram_bytes=None):
    """Return {role: classify_hardware_fit(...)} for every registered lane."""
    if ram_bytes is None:
        summary = hardware_summary()
        ram_bytes = summary.get("ram")
        if vram_bytes is None:
            vram_bytes = summary.get("vram")
    return {
        role: classify_hardware_fit(
            info["model"], ram_bytes=ram_bytes, vram_bytes=vram_bytes,
        )
        for role, info in MODEL_REGISTRY.items()
    }
