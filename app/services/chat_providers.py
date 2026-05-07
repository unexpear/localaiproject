"""Opt-in cloud chat providers (Phase 1 — see `docs/design/chat-providers.md`).

Default routing stays on Ollama. A user can bind a lane to a cloud
provider (`provider bind reasoning anthropic:claude-sonnet-4-5`); when
chat resolves to that lane, this module's adapters take over instead
of `runtime_client.infer_chat`.

**Hard rules** (mirrored from the design doc):
- Local-first stays default. Ollama is the only auto-bound provider.
- Cloud per-lane opt-in only — `provider bind` is the explicit user act.
- No auto-routing. The router never escalates to a cloud lane on its own.
- Approval gating still wins. Workspace reads/writes are unaffected.
- No telemetry. No prompts leave the machine unless the user has
  explicitly bound the lane.

Adapters use stdlib `urllib.request` so the package has no new
dependencies. SDK-based paths (`openai`, `anthropic`) are easy to add
later as opt-in optimisations.

Phase 1 ships OpenAI and Anthropic. Google Gemini is deferred until
someone needs it (the API auth flow is fiddly and the request shape
differs more from the OpenAI/Anthropic norm).

Auth resolution today: environment variables only. The keyring +
1Password CLI fallback chain spelled out in the design doc is Phase 2.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# Match the shape returned by `runtime_client.infer_chat` so the chat
# service can swap providers behind the scenes without conditional
# response parsing on its end.
_InferResult = Dict[str, Any]


@dataclass
class ProviderInfo:
    """One provider registered with the system."""
    id: str
    label: str
    auth_env: str          # primary env var name; empty for local providers
    chat_fn: Callable[[str, List[Dict[str, str]], Dict[str, Any]], _InferResult]
    is_local: bool = False
    description: str = ""


# ============================================================
# Auth
# ============================================================

def _resolve_credential(env_name: str) -> Optional[str]:
    """Phase 1: env var lookup only.

    Phase 2 (planned) extends to: env var → OS keyring → 1Password CLI.
    """
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if value and value.strip():
        return value.strip()
    return None


def has_credentials(provider_id: str) -> bool:
    info = REGISTRY.get(provider_id)
    if info is None:
        return False
    if info.is_local:
        return True
    return _resolve_credential(info.auth_env) is not None


# ============================================================
# Common HTTP helper (stdlib only)
# ============================================================

def _http_post_json(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    timeout: float = 60.0,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """POST a JSON body, return ``(parsed_response, error_text_or_None)``.

    Maps common failure modes to a short, machine-readable error_text:
    ``"timeout"``, ``"unreachable"``, ``"unauthorized"``,
    ``"rate_limited"``, ``"http_<code>"``, ``"bad_response"``.
    """
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw), None
            except json.JSONDecodeError:
                return None, "bad_response"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "unauthorized"
        if exc.code == 429:
            return None, "rate_limited"
        # Try to read the error body for a useful diagnostic.
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
            return None, f"http_{exc.code}: {body_text[:200]}"
        except Exception:  # noqa: BLE001
            return None, f"http_{exc.code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, "timeout"
        return None, "unreachable"
    except TimeoutError:
        return None, "timeout"


# ============================================================
# OpenAI Chat Completions API
# ============================================================

OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"


def openai_chat(
    model: str,
    messages: List[Dict[str, str]],
    options: Optional[Dict[str, Any]] = None,
) -> _InferResult:
    """Call OpenAI's chat completions endpoint.

    `options` may contain ``temperature``, ``max_tokens``, ``base_url``
    overrides. The base URL override lets users target compatible APIs
    (Together, Groq, Anyscale, local proxies, etc.).
    """
    options = options or {}
    api_key = _resolve_credential("OPENAI_API_KEY")
    if not api_key:
        return {"ok": False, "content": "", "error": "provider_unauthenticated", "raw": None}

    base_url = options.get("base_url") or OPENAI_DEFAULT_BASE
    body = {
        "model": model,
        "messages": messages,
    }
    if "temperature" in options:
        body["temperature"] = float(options["temperature"])
    if "max_tokens" in options:
        body["max_tokens"] = int(options["max_tokens"])

    data, err = _http_post_json(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
        timeout=float(options.get("timeout", 60.0)),
    )
    if err is not None:
        return {"ok": False, "content": "", "error": _classify_http_error(err), "raw": None}

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "content": "", "error": "bad_response", "raw": data}

    return {"ok": True, "content": content, "error": None, "raw": data}


# ============================================================
# Anthropic Messages API
# ============================================================

ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_API_VERSION = "2023-06-01"


def anthropic_chat(
    model: str,
    messages: List[Dict[str, str]],
    options: Optional[Dict[str, Any]] = None,
) -> _InferResult:
    """Call Anthropic's messages endpoint.

    Anthropic's API differs from OpenAI's in two key ways:
      1. ``system`` is a top-level field, not a message role.
      2. ``max_tokens`` is required (we default to 4096 if unset).

    We collapse multiple system-role messages into a single
    newline-joined string for the top-level ``system`` field. Order is
    preserved.
    """
    options = options or {}
    api_key = _resolve_credential("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "content": "", "error": "provider_unauthenticated", "raw": None}

    system_chunks: List[str] = []
    other_messages: List[Dict[str, str]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if content:
                system_chunks.append(str(content))
        else:
            # Anthropic requires alternating user/assistant turns; we
            # pass through whatever the chat service sent and let the
            # API surface validation errors directly.
            other_messages.append({"role": msg["role"], "content": msg.get("content", "")})

    base_url = options.get("base_url") or ANTHROPIC_DEFAULT_BASE
    body = {
        "model": model,
        "max_tokens": int(options.get("max_tokens", 4096)),
        "messages": other_messages,
    }
    if system_chunks:
        body["system"] = "\n\n".join(system_chunks)
    if "temperature" in options:
        body["temperature"] = float(options["temperature"])

    data, err = _http_post_json(
        f"{base_url.rstrip('/')}/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
        body=body,
        timeout=float(options.get("timeout", 60.0)),
    )
    if err is not None:
        return {"ok": False, "content": "", "error": _classify_http_error(err), "raw": None}

    try:
        # `content` is a list of content blocks; concatenate text blocks.
        blocks = data.get("content") or []
        parts: List[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        content = "".join(parts)
    except (TypeError, AttributeError):
        return {"ok": False, "content": "", "error": "bad_response", "raw": data}

    return {"ok": True, "content": content, "error": None, "raw": data}


# ============================================================
# Ollama wrapper (so the registry treats every provider uniformly)
# ============================================================

def ollama_chat(
    model: str,
    messages: List[Dict[str, str]],
    options: Optional[Dict[str, Any]] = None,
) -> _InferResult:
    """Pass-through wrapper around `runtime_client.infer_chat`.

    Lazy import keeps this module loadable in environments where
    runtime_client isn't on the path (some test contexts).
    """
    from app.models import runtime_client
    return runtime_client.infer_chat(model, messages=messages)


# ============================================================
# Error classification
# ============================================================

def _classify_http_error(err: str) -> str:
    """Map the small error vocabulary from `_http_post_json` to the
    canonical reason tags in `error_vocabulary.py`."""
    if err == "unauthorized":
        return "provider_unauthenticated"
    if err == "rate_limited":
        return "provider_quota_exceeded"
    if err in ("timeout", "unreachable"):
        return "provider_unreachable"
    # Everything else (bad_response, http_5xx, …) is a generic
    # provider_unreachable for the purposes of error_vocabulary; the
    # underlying error_text is preserved in result.error for diagnosis.
    return "provider_unreachable"


# ============================================================
# Registry
# ============================================================

REGISTRY: Dict[str, ProviderInfo] = {
    "ollama": ProviderInfo(
        id="ollama",
        label="Ollama (local)",
        auth_env="",
        chat_fn=ollama_chat,
        is_local=True,
        description="Default. Local models via the Ollama runtime.",
    ),
    "openai": ProviderInfo(
        id="openai",
        label="OpenAI",
        auth_env="OPENAI_API_KEY",
        chat_fn=openai_chat,
        is_local=False,
        description="Cloud. Requires OPENAI_API_KEY in the environment.",
    ),
    "anthropic": ProviderInfo(
        id="anthropic",
        label="Anthropic",
        auth_env="ANTHROPIC_API_KEY",
        chat_fn=anthropic_chat,
        is_local=False,
        description="Cloud. Requires ANTHROPIC_API_KEY in the environment.",
    ),
}


def list_providers() -> List[ProviderInfo]:
    """Stable list of registered providers, ollama first."""
    ordered = [REGISTRY["ollama"]]
    ordered.extend(p for pid, p in REGISTRY.items() if pid != "ollama")
    return ordered


def get_provider(provider_id: str) -> Optional[ProviderInfo]:
    return REGISTRY.get(provider_id)


# ============================================================
# Lane binding parser
# ============================================================

def parse_lane_binding(spec: str) -> Tuple[str, str]:
    """Parse ``"provider:model"`` (or just ``"model"`` defaulting to ollama).

    Returns ``(provider_id, model_id)``. Raises ValueError on a bad
    shape; the dispatcher translates that to ``invalid_argument``.
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("binding cannot be empty")
    if ":" not in spec:
        return "ollama", spec
    provider_id, model_id = spec.split(":", 1)
    provider_id = provider_id.strip().lower()
    model_id = model_id.strip()
    if not provider_id or not model_id:
        raise ValueError(f"binding must be 'provider:model' (got {spec!r})")
    return provider_id, model_id


# ============================================================
# Dispatch — the function chat_service calls
# ============================================================

def dispatch_chat(
    provider_id: str,
    model: str,
    messages: List[Dict[str, str]],
    options: Optional[Dict[str, Any]] = None,
) -> _InferResult:
    """Route a chat call to the named provider.

    Returns a result dict in the same shape as
    ``runtime_client.infer_chat``: ``{ok, content, error, raw}``.
    The ``error`` field carries a canonical reason tag from
    ``error_vocabulary.py`` on the failure path.
    """
    info = REGISTRY.get(provider_id)
    if info is None:
        return {
            "ok": False,
            "content": "",
            "error": "provider_unknown",
            "raw": None,
        }
    return info.chat_fn(model, messages, options)


# ============================================================
# Streaming dispatch (Phase 1 — see docs/design/streaming.md)
# ============================================================

def dispatch_chat_stream(
    provider_id: str,
    model: str,
    messages: List[Dict[str, str]],
    options: Optional[Dict[str, Any]] = None,
):
    """Streaming counterpart to ``dispatch_chat``. Returns a
    generator that yields ``{kind, ...}`` events as the model
    produces tokens.

    Phase 1: only Ollama supports streaming end-to-end. OpenAI /
    Anthropic fall through to a single ``done`` event built from
    the batch ``dispatch_chat`` call (lets callers write
    provider-agnostic streaming loops without losing the cloud
    fallback). Phase 2 will add native streaming for those
    providers via their respective SSE / chunked APIs.

    Yields:

    * ``{"kind": "chunk", "content": "<fragment>"}`` — repeated
      while the model is producing.
    * ``{"kind": "done", "content": "<full-text>", "raw": dict}``
      — once on success, with ``content`` carrying the full
      accumulated text (callers that just want batch behaviour
      can wait for this one event).
    * ``{"kind": "error", "error": "<reason>",
       "content": "<partial>"}`` — once on failure;
      ``error`` is a canonical reason tag from
      ``error_vocabulary.py`` when the provider classifier
      recognised the failure.

    Always emits exactly one terminal ``done`` or ``error`` event,
    so callers can write ``for event in stream:`` loops with a
    simple ``break`` on terminal kinds. See
    ``docs/design/streaming.md`` for the design rationale.
    """
    info = REGISTRY.get(provider_id)
    if info is None:
        yield {
            "kind": "error",
            "error": "provider_unknown",
            "content": "",
        }
        return

    if provider_id == "ollama":
        # Native streaming via runtime_client.infer_chat_stream.
        from app.models import runtime_client
        for event in runtime_client.infer_chat_stream(model, messages=messages):
            yield event
        return

    # Cloud providers: fall back to the batch path and synthesize a
    # single ``done`` event. Future phases will replace this with
    # native chunked reads against the provider's streaming API.
    result = info.chat_fn(model, messages, options)
    if result.get("ok"):
        yield {
            "kind": "done",
            "content": result.get("content") or "",
            "raw": result.get("raw") or {},
        }
    else:
        yield {
            "kind": "error",
            "error": result.get("error") or "provider_unreachable",
            "content": result.get("content") or "",
        }
