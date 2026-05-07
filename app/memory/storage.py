"""Pluggable storage interface for the memory layer.

Two backends ship today:

  - `JsonStorage`    -- the legacy single-file JSON store (matches the
                        current behaviour of `app/memory/local_memory.py`).
  - `DuckDbStorage`  -- DuckDB-backed single-row payload store. Gives us
                        ACID writes and serialises concurrent updates that
                        previously raced on the JSON read-modify-write.

Both expose a tiny surface that `local_memory` can delegate to:

    storage.read_store() -> dict   # the {"sessions": {...}} blob
    storage.write_store(data)      # atomic write
    storage.path                   # file on disk (for diagnostics)

The public business logic in `local_memory.py` (normalization,
validation, defaults) stays intact -- only the I/O moves.

The active backend is chosen by `MEMORY_BACKEND` (env var) or
the explicit `get_storage(backend=...)` factory. JSON remains the
default in Phase 1 of the migration so existing data on disk is not
touched.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Protocol


class MemoryStorage(Protocol):
    """Storage contract every backend implements.

    `mutate(fn)` is the atomic read-modify-write primitive — backends are
    free to wrap it in a single transaction or a single lock so concurrent
    callers never lose each other's edits. Callers in `local_memory.py`
    should prefer `mutate` over manual read_store + write_store sequences.

    `search_executions(...)` returns recent execution records that match
    the supplied filters, newest first. Backends are free to back it with
    a SQL index (DuckDB) or a linear scan over the payload (JSON); the
    semantics are identical.
    """

    @property
    def path(self) -> str:
        ...

    def read_store(self) -> Dict[str, Any]:
        ...

    def write_store(self, data: Dict[str, Any]) -> None:
        ...

    def mutate(self, fn) -> Dict[str, Any]:
        ...

    def search_executions(
        self,
        session_id: str = "default",
        *,
        query: str | None = None,
        lane: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 20,
    ) -> list:
        ...


_DEFAULT_BACKEND = "json"


def get_storage(backend: str | None = None, path: str | None = None) -> MemoryStorage:
    """Return a storage backend instance.

    `backend` defaults to the `MEMORY_BACKEND` env var, falling back to "json".
    `path` overrides the on-disk location -- useful for tests.

    For the DuckDB backend, this will also auto-migrate from a legacy
    `local_memory.json` file the first time the database is initialised
    and is otherwise empty. The migration is idempotent.
    """
    chosen = (backend or os.environ.get("MEMORY_BACKEND") or _DEFAULT_BACKEND).strip().lower()
    if chosen == "duckdb":
        from app.memory.duckdb_storage import DuckDbStorage
        from app.memory.json_storage import _DEFAULT_PATH as _LEGACY_JSON
        instance = DuckDbStorage(path=path)
        # One-shot migration: if the DB has no rows yet AND a legacy JSON
        # file is sitting next to it, import that JSON.
        try:
            current = instance.read_store()
            if not current.get("sessions"):
                instance.import_from_json_file(_LEGACY_JSON)
        except Exception:  # noqa: BLE001
            pass
        return instance
    if chosen == "json":
        from app.memory.json_storage import JsonStorage
        return JsonStorage(path=path)
    raise ValueError(f"unknown MEMORY_BACKEND '{chosen}' (expected json or duckdb)")
