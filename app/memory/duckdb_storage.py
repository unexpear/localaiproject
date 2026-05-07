"""DuckDB-backed storage for the memory layer.

Phase 1 design: a single-row table that holds the entire session blob as
a JSON payload. This is the smallest viable schema -- it gets us:

  - ACID writes through DuckDB's transaction layer
  - serialisation of concurrent writers (no more JSON read-modify-write race)
  - a single .duckdb file shipping next to local_memory.json
  - the whole `local_memory.py` business logic stays untouched

Phase 2 will switch the active backend from JSON to DuckDB. Phase 3 will
break the payload out into proper relational tables and expose SQL-backed
features (search executions, lane usage histograms, retention pruning).
The interface in `storage.py` does not change between phases, so callers
in `local_memory.py` remain stable.

Schema (Phase 1):

    CREATE TABLE store (
        id        INTEGER PRIMARY KEY DEFAULT 1,
        payload   JSON NOT NULL,
        updated_at TIMESTAMP NOT NULL
    );

Always exactly one row at id = 1. Reads return that row's payload.
Writes UPSERT it within a single transaction.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict


_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "local_memory.duckdb")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store (
    id         INTEGER PRIMARY KEY,
    payload    JSON NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

-- Executions index: side table populated automatically by write_store from
-- the JSON payload. Lets search_executions answer queries with SQL instead
-- of loading and scanning the entire payload.
CREATE TABLE IF NOT EXISTS executions_index (
    session_id      TEXT      NOT NULL,
    idx             INTEGER   NOT NULL,
    execution_id    TEXT,
    requested_lane  TEXT,
    resolved_lane   TEXT,
    status          TEXT,
    message_preview TEXT,
    created_at      TIMESTAMP,
    payload_json    TEXT      NOT NULL,
    PRIMARY KEY (session_id, idx)
);

CREATE INDEX IF NOT EXISTS executions_lane_idx
    ON executions_index (session_id, resolved_lane);
CREATE INDEX IF NOT EXISTS executions_status_idx
    ON executions_index (session_id, status);
CREATE INDEX IF NOT EXISTS executions_created_idx
    ON executions_index (session_id, created_at);
"""


class DuckDbStorage:
    """Single-row JSON-payload store backed by DuckDB.

    DuckDB serialises concurrent writers with row-level locking, so a chat
    handler on the UI thread and an install worker on a background thread
    can safely both call write_store without losing each other's edits.

    The connection is opened lazily and a single per-process lock guards
    writes (DuckDB itself is process-safe; the Python connection is not
    thread-safe to share, so we serialise at the Python layer too).
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        self._lock = threading.Lock()
        self._connection = None
        self._initialise()

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _connect(self):
        # Lazy import keeps test environments without duckdb installed working
        # for everything that doesn't actually touch the DB.
        import duckdb
        if self._connection is None:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            self._connection = duckdb.connect(self._path)
        return self._connection

    def _initialise(self) -> None:
        con = self._connect()
        con.execute(_SCHEMA)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001
                pass
            self._connection = None

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------
    def read_store(self) -> Dict[str, Any]:
        con = self._connect()
        with self._lock:
            row = con.execute("SELECT payload FROM store WHERE id = 1").fetchone()
        if not row or row[0] is None:
            return {"sessions": {}}
        try:
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (TypeError, json.JSONDecodeError):
            return {"sessions": {}}
        if not isinstance(data, dict):
            return {"sessions": {}}
        data.setdefault("sessions", {})
        return data

    def write_store(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise TypeError("write_store expects a dict")
        with self._lock:
            self._write_locked(data)

    def _write_locked(self, data: Dict[str, Any]) -> None:
        payload = dict(data)
        payload.setdefault("sessions", {})
        encoded = json.dumps(payload, ensure_ascii=False)
        con = self._connect()
        con.execute("BEGIN")
        try:
            con.execute(
                """
                INSERT INTO store (id, payload, updated_at)
                VALUES (1, ?, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                """,
                [encoded],
            )
            self._rebuild_executions_index(con, payload.get("sessions") or {})
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    @staticmethod
    def _rebuild_executions_index(con, sessions: Dict[str, Any]) -> None:
        """Refresh executions_index to match the payload.

        Whole-table refresh keeps the logic simple and the data canonical:
        executions_index is a derived projection of the payload, so the only
        invariant we need is "rebuild after every write".
        """
        con.execute("DELETE FROM executions_index")
        for session_id, session in sessions.items():
            executions = (session or {}).get("executions") or []
            for idx, exe in enumerate(executions):
                if not isinstance(exe, dict):
                    continue
                created_at = exe.get("created_at") or exe.get("timestamp")
                con.execute(
                    """
                    INSERT INTO executions_index (
                        session_id, idx, execution_id,
                        requested_lane, resolved_lane,
                        status, message_preview, created_at, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, TRY_CAST(? AS TIMESTAMP), ?)
                    """,
                    [
                        session_id,
                        idx,
                        exe.get("id"),
                        exe.get("requested_lane"),
                        exe.get("resolved_lane"),
                        exe.get("status"),
                        exe.get("message_preview") or (exe.get("message") or "")[:200],
                        created_at,
                        json.dumps(exe, ensure_ascii=False),
                    ],
                )

    # ------------------------------------------------------------------
    # SQL-backed search
    # ------------------------------------------------------------------
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
        """Return the most recent matching executions, newest first.

        Filters are AND-combined; any filter set to None / "" is ignored.
        - query   : substring match against `message_preview` (case-insensitive)
        - lane    : matches either requested_lane or resolved_lane (exact)
        - status  : exact match against `status`
        - since   : ISO timestamp; rows with `created_at >= since` only
        - limit   : 1-200 (clamped)
        """
        con = self._connect()
        clauses = ["session_id = ?"]
        params: list[Any] = [session_id]
        if query:
            clauses.append("LOWER(message_preview) LIKE ?")
            params.append(f"%{query.lower()}%")
        if lane:
            clauses.append("(requested_lane = ? OR resolved_lane = ?)")
            params.extend([lane, lane])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("created_at >= TRY_CAST(? AS TIMESTAMP)")
            params.append(since)
        limit = max(1, min(200, int(limit or 20)))
        sql = (
            "SELECT payload_json FROM executions_index "
            "WHERE " + " AND ".join(clauses) +
            " ORDER BY idx DESC LIMIT " + str(limit)
        )
        with self._lock:
            rows = con.execute(sql, params).fetchall()
        out: list[Dict[str, Any]] = []
        for (payload_json,) in rows:
            try:
                out.append(json.loads(payload_json))
            except (TypeError, json.JSONDecodeError):
                continue
        return out

    def _read_locked(self) -> Dict[str, Any]:
        con = self._connect()
        row = con.execute("SELECT payload FROM store WHERE id = 1").fetchone()
        if not row or row[0] is None:
            return {"sessions": {}}
        try:
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (TypeError, json.JSONDecodeError):
            return {"sessions": {}}
        if not isinstance(data, dict):
            return {"sessions": {}}
        data.setdefault("sessions", {})
        return data

    def mutate(self, fn) -> Dict[str, Any]:
        """Atomic read-modify-write: lock + transaction wrap the whole sequence.

        DuckDB's transaction guarantees that concurrent processes see a
        consistent view; the per-instance lock guarantees the same for
        threads sharing this Python connection.
        """
        with self._lock:
            data = self._read_locked()
            result = fn(data)
            if result is None:
                result = data
            if not isinstance(result, dict):
                raise TypeError("mutate fn must return a dict (or None for in-place mutation)")
            self._write_locked(result)
            return result

    # ------------------------------------------------------------------
    # Migration helpers (used in Phase 2 to import existing JSON data)
    # ------------------------------------------------------------------
    def import_from_json_file(self, json_path: str) -> bool:
        """Read an existing local_memory.json and write it into the DuckDB store.

        Returns True if anything was imported, False if the source was missing
        or empty. Idempotent -- calling twice with the same input is a no-op.
        """
        if not os.path.isfile(json_path):
            return False
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            if not raw.strip():
                return False
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        data.setdefault("sessions", {})
        self.write_store(data)
        return True
