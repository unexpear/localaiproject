import json
import logging

import requests

OLLAMA_BASE_URL = "http://localhost:11434"

logger = logging.getLogger(__name__)


def _request(method, path, timeout=60, **kwargs):
    return requests.request(method, f"{OLLAMA_BASE_URL}{path}", timeout=timeout, **kwargs)


def infer_chat(model, message=None, messages=None):
    """Call Ollama's /api/chat endpoint and return a structured result."""
    try:
        if messages is None:
            if message is None:
                raise ValueError("infer_chat requires either message or messages")
            messages = [{"role": "user", "content": message}]

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        resp = _request("POST", "/api/chat", timeout=180, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok": True,
            "content": data.get("message", {}).get("content") or data.get("response", ""),
            "error": None,
            "raw": data,
        }
    except Exception as e:
        return {
            "ok": False,
            "content": "",
            "error": str(e),
            "raw": None,
        }


def chat(model, message):
    """Backward-compatible text wrapper around infer_chat()."""
    result = infer_chat(model, message)
    if result["ok"]:
        return result["content"]
    return f"[Error calling Ollama: {result['error']}]"


def infer_chat_stream(model, message=None, messages=None):
    """Streaming variant of ``infer_chat``. Sends ``stream: true`` to
    Ollama's /api/chat endpoint and yields one chunk per server event
    plus a final ``done`` or ``error`` event.

    Yields dicts:

    * ``{"kind": "chunk", "content": "<token-or-fragment>"}`` —
      the model's incremental output. Multiple per call.
    * ``{"kind": "done", "content": "<full-accumulated-text>",
       "raw": {<final-event>}}`` — emitted exactly once on
      successful completion. ``content`` is the concatenation of
      every preceding chunk so callers that want "stream + then
      act on the full text" don't have to accumulate themselves.
    * ``{"kind": "error", "error": "<message>",
       "content": "<partial>"}`` — emitted exactly once on any
      transport / parse / HTTP failure. ``content`` carries
      whatever was accumulated before the failure (may be empty).

    The generator always emits a terminal ``done`` OR ``error``
    event — never both, and never neither — so callers can
    write a simple ``for event in stream:`` loop and break/handle
    on ``kind != "chunk"``.

    See ``docs/design/streaming.md`` Phase 1 for design context.
    """
    if messages is None:
        if message is None:
            yield {
                "kind": "error", "error": "infer_chat_stream requires either message or messages",
                "content": "",
            }
            return
        messages = [{"role": "user", "content": message}]

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    accumulated_parts = []
    final_raw = None
    try:
        resp = _request(
            "POST", "/api/chat", timeout=180, json=payload, stream=True,
        )
        resp.raise_for_status()
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                # Malformed line — skip it but keep streaming;
                # Ollama doesn't normally emit invalid JSON, but
                # transport hiccups (proxies splitting lines) can.
                continue
            if not isinstance(event, dict):
                continue
            # Ollama's chat-stream events nest the chunk under
            # "message"."content"; per-event "done" flag flips on
            # the final event. Tolerate both shapes (event-by-event
            # for /api/chat, single-string for older models).
            chunk_text = ""
            message_block = event.get("message")
            if isinstance(message_block, dict):
                chunk_text = str(message_block.get("content") or "")
            elif "response" in event:
                chunk_text = str(event.get("response") or "")
            if chunk_text:
                accumulated_parts.append(chunk_text)
                yield {"kind": "chunk", "content": chunk_text}
            if event.get("done"):
                final_raw = event
                break
    except Exception as exc:  # noqa: BLE001
        yield {
            "kind": "error",
            "error": str(exc),
            "content": "".join(accumulated_parts),
        }
        return
    yield {
        "kind": "done",
        "content": "".join(accumulated_parts),
        "raw": final_raw or {},
    }


def detect_ollama():
    """Return True if the Ollama HTTP API is reachable."""
    try:
        resp = _request("GET", "/api/version", timeout=5)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.debug("Ollama detect failed: %s", e)
        return False


def get_ollama_version():
    """Return the Ollama server version reported by the HTTP API."""
    try:
        resp = _request("GET", "/api/version", timeout=5)
        resp.raise_for_status()
        return resp.json().get("version")
    except Exception as e:
        logger.debug("Ollama version check failed: %s", e)
        return None


def list_ollama_models():
    """Return a list of installed Ollama models (names only)."""
    try:
        resp = _request("GET", "/api/tags", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [
            entry.get("model") or entry.get("name")
            for entry in data.get("models", [])
            if entry.get("model") or entry.get("name")
        ]
    except Exception as e:
        logger.debug("Ollama list models failed: %s", e)
        return []


def pull_model(model_name, progress_callback=None, cancel_event=None):
    """Pull a model using Ollama's HTTP API. Returns (success, output).

    When `progress_callback` is provided the request streams Ollama's
    newline-delimited JSON events (keys include `status`, `digest`,
    `completed`, `total`) and each parsed event is passed to the callback.
    Callback exceptions are swallowed so a flaky UI hook never aborts the pull.

    If `cancel_event` (a threading.Event-like object with `.is_set()`) is
    provided and set during streaming, the pull aborts early and returns
    (False, "cancelled").
    """
    streaming = progress_callback is not None
    try:
        print(f"  [Ollama] Pulling {model_name}...")
        if not streaming:
            resp = _request(
                "POST",
                "/api/pull",
                timeout=300,
                json={"name": model_name, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
            return True, data.get("status") or "success"

        resp = _request(
            "POST",
            "/api/pull",
            timeout=600,
            json={"name": model_name, "stream": True},
            stream=True,
        )
        resp.raise_for_status()
        last_status = ""
        saw_error = None
        cancelled = False
        for raw_line in resp.iter_lines(decode_unicode=True):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                event = {"status": str(raw_line)}
            event.setdefault("model", model_name)
            if event.get("status"):
                last_status = event["status"]
            if event.get("error"):
                saw_error = event["error"]
            try:
                progress_callback(event)
            except Exception as cb_exc:
                logger.debug("pull_model progress callback raised: %s", cb_exc)
        try:
            resp.close()
        except Exception:
            pass
        if cancelled:
            return False, "cancelled"
        if saw_error:
            return False, saw_error
        return True, last_status or "success"
    except Exception as e:
        return False, str(e)


def delete_model(model_name):
    """Delete a local Ollama model. Returns (success, output)."""
    try:
        resp = _request(
            "DELETE",
            "/api/delete",
            timeout=30,
            json={"name": model_name},
        )
        if resp.status_code == 404:
            return False, f"model '{model_name}' not found"
        resp.raise_for_status()
        return True, "deleted"
    except Exception as e:
        return False, str(e)
