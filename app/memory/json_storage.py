"""JSON-file storage backend (the legacy behaviour).

Single file, atomic write via `os.replace`. Identical to what
`local_memory.py` has done since the project began -- this module just
extracts the I/O bit so we can swap it out via the storage interface.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict


_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "local_memory.json")


class JsonStorage:
    """Read-modify-write JSON store. Wraps writes in a per-instance lock to
    avoid two threads in the same process clobbering each other.

    Cross-process locking is out of scope here; for that, run only one
    instance of the app (single-user desktop assumption holds).
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    # ----- read -----
    def read_store(self) -> Dict[str, Any]:
        if not os.path.exists(self._path):
            return {"sessions": {}}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            if not raw.strip():
                return {"sessions": {}}
            decoder = json.JSONDecoder()
            stripped = raw.lstrip()
            data, _end = decoder.raw_decode(stripped)
        except (OSError, json.JSONDecodeError):
            return {"sessions": {}}
        if not isinstance(data, dict):
            return {"sessions": {}}
        data.setdefault("sessions", {})
        return data

    # ----- write -----
    def write_store(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError("write_store expects a dict")
        with self._lock:
            self._write_locked(data)

    def _write_locked(self, data: Dict[str, Any]) -> None:
        payload = dict(data)
        payload.setdefault("sessions", {})
        tmp = f"{self._path}.tmp"
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self._path)

    # ----- atomic read-modify-write -----
    def mutate(self, fn) -> Dict[str, Any]:
        """Hold the lock for read + apply + write so concurrent callers serialise."""
        with self._lock:
            data = self.read_store()
            result = fn(data)
            if result is None:
                result = data
            if not isinstance(result, dict):
                raise TypeError("mutate fn must return a dict (or None for in-place mutation)")
            self._write_locked(result)
            return result

    # ----- search (linear scan) -----
    def search_executions(
        self,
        session_id: str = "default",
        *,
        query: str | None = None,
        lane: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list[Dict[str, Any]]:
        """Linear-scan search. Same semantics as the DuckDB SQL version."""
        data = self.read_store()
        executions = ((data.get("sessions") or {}).get(session_id) or {}).get("executions") or []
        q = (query or "").lower().strip() or None
        results: list[Dict[str, Any]] = []
        # Walk newest-first
        for exe in reversed(executions):
            if not isinstance(exe, dict):
                continue
            preview = (exe.get("message_preview") or exe.get("message") or "").lower()
            if q and q not in preview:
                continue
            if lane and lane not in (exe.get("requested_lane"), exe.get("resolved_lane")):
                continue
            if status and exe.get("status") != status:
                continue
            if since:
                created = exe.get("created_at") or exe.get("timestamp") or ""
                if created < since:
                    continue
            results.append(exe)
            if len(results) >= max(1, min(200, int(limit or 20))):
                break
        return results
