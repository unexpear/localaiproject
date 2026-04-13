import requests

OLLAMA_BASE_URL = "http://localhost:11434"


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


def detect_ollama():
    """Return True if the Ollama HTTP API is reachable."""
    try:
        resp = _request("GET", "/api/version", timeout=5)
        resp.raise_for_status()
        return True
    except Exception:
        return False


def get_ollama_version():
    """Return the Ollama server version reported by the HTTP API."""
    try:
        resp = _request("GET", "/api/version", timeout=5)
        resp.raise_for_status()
        return resp.json().get("version")
    except Exception:
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
    except Exception:
        return []


def pull_model(model_name):
    """Pull a model using Ollama's HTTP API. Returns (success, output)."""
    try:
        print(f"  [Ollama] Pulling {model_name}...")
        resp = _request(
            "POST",
            "/api/pull",
            timeout=300,
            json={"name": model_name, "stream": False},
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "")
        return True, status or "success"
    except Exception as e:
        return False, str(e)
