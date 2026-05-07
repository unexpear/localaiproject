"""Backlog adapters — pull tasks from external sources into a builder's inbox.

Phase 1 surface (see ``docs/design/extensibility.md`` §8):

  - ``BacklogSource`` Protocol: any object with ``id`` and
    ``iter_items(config)`` is a valid source.
  - ``register_backlog_source(id)`` decorator for plugins / scripts to
    add custom sources at import time.
  - Built-in ``json_file`` source: reads ``[{"id": ..., "body": ...,
    "builder_hint": ...}, ...]`` from a path on disk. Zero deps; the
    immediate "drop a JSON file and pull from it" workflow.
  - ``pull_into_inbox(source_id, config, *, planner_session, builder)``
    iterates the source and dispatches one ``local_memory.add_task``
    per item. Idempotent: items whose ``id`` matches a previously-
    pulled item are skipped (tracked per planner session).
  - Dispatcher commands: ``backlog list``, ``backlog sources``,
    ``backlog pull <source> [k=v ...]``.

Phase 2 (planned) adds: GitHub Issues source, Linear source, periodic
``backlog auto`` polling, per-session source-config storage.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Protocol

from app.memory import local_memory


logger = logging.getLogger(__name__)


# ============================================================
# BacklogItem + Source Protocol
# ============================================================

@dataclass
class BacklogItem:
    """One task pulled from a backlog source."""

    id: str            # source-relative unique id (e.g. "gh-#123")
    body: str          # the task text dispatched to the builder
    builder_hint: Optional[str] = None
    source_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class BacklogSource(Protocol):
    """Any object with an ``id`` and ``iter_items(config)`` is a source."""

    id: str
    description: str

    def iter_items(self, config: Dict[str, Any]) -> Iterator[BacklogItem]: ...


# ============================================================
# Registry + decorator
# ============================================================

_registry: Dict[str, BacklogSource] = {}
_registry_lock = threading.Lock()

# Per-planner-session: ids of items already pulled, to make `pull`
# idempotent. Keyed by (planner_session, source_id) → set of item ids.
_pulled_ids: Dict[str, set] = {}
_pulled_lock = threading.Lock()


def register_backlog_source(source: BacklogSource) -> BacklogSource:
    """Register a source instance under its ``.id``. Idempotent —
    re-registration overwrites (useful in tests)."""
    if not source.id or not isinstance(source.id, str):
        raise ValueError("source.id must be a non-empty string")
    with _registry_lock:
        _registry[source.id] = source
    return source


def list_sources() -> List[BacklogSource]:
    with _registry_lock:
        return sorted(_registry.values(), key=lambda s: s.id)


def get_source(source_id: str) -> Optional[BacklogSource]:
    with _registry_lock:
        return _registry.get(source_id)


def _reset_for_tests() -> None:
    """Drop the registry + pulled-id cache. Test-only."""
    with _registry_lock:
        _registry.clear()
    with _pulled_lock:
        _pulled_ids.clear()
    _register_builtin_sources()


def _pulled_key(planner_session: str, source_id: str) -> str:
    return f"{planner_session}::{source_id}"


def _already_pulled(planner_session: str, source_id: str, item_id: str) -> bool:
    key = _pulled_key(planner_session, source_id)
    with _pulled_lock:
        return item_id in _pulled_ids.get(key, set())


def _mark_pulled(planner_session: str, source_id: str, item_id: str) -> None:
    key = _pulled_key(planner_session, source_id)
    with _pulled_lock:
        _pulled_ids.setdefault(key, set()).add(item_id)


# ============================================================
# Built-in: json_file source
# ============================================================

class JsonFileSource:
    """Read a list of items from a JSON file.

    File format::

        [
          {"id": "task-1", "body": "Refactor the foo module"},
          {"id": "task-2", "body": "Write tests for bar", "builder_hint": "coder"}
        ]

    Config args: ``{"path": "<absolute or cwd-relative path>"}``.
    """

    id = "json_file"
    description = "Pull items from a local JSON file (no network)."

    def iter_items(self, config: Dict[str, Any]) -> Iterator[BacklogItem]:
        path_arg = (config or {}).get("path")
        if not path_arg:
            raise ValueError("json_file source requires `path` in config")
        path = Path(str(path_arg)).expanduser()
        if not path.exists():
            raise FileNotFoundError(str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"json_file: failed to parse {path}: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError(
                f"json_file: expected a JSON array of items, got {type(data).__name__}"
            )
        for entry in data:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id") or "").strip()
            body = str(entry.get("body") or "").strip()
            if not item_id or not body:
                continue
            yield BacklogItem(
                id=item_id,
                body=body,
                builder_hint=str(entry.get("builder_hint") or "") or None,
                source_url=str(entry.get("source_url") or "") or None,
                metadata={
                    k: v for k, v in entry.items()
                    if k not in {"id", "body", "builder_hint", "source_url"}
                },
            )


def _register_builtin_sources() -> None:
    register_backlog_source(JsonFileSource())


_register_builtin_sources()


# ============================================================
# Pull entry point
# ============================================================

@dataclass
class PullResult:
    """Summary of a `pull_into_inbox` call."""

    source_id: str
    requested: int = 0
    pulled: int = 0
    skipped_duplicate: int = 0
    skipped_failed: int = 0
    task_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def pull_into_inbox(
    source_id: str,
    config: Dict[str, Any],
    *,
    planner_session: str,
    builder: Optional[str] = None,
) -> PullResult:
    """Iterate ``source_id`` and dispatch each new item as a task.

    A task is only dispatched if its source-relative ``id`` hasn't been
    pulled before for this planner session. Per-item dispatch errors
    are captured in ``result.errors`` but don't abort the whole pull.

    ``builder`` overrides the per-item ``builder_hint``. If neither is
    set, dispatching falls back to whichever builder ``add_task``
    chooses (raises if there's ambiguity).
    """
    source = get_source(source_id)
    if source is None:
        raise ValueError(f"unknown backlog source {source_id!r}")
    result = PullResult(source_id=source_id)

    for item in source.iter_items(config or {}):
        result.requested += 1
        if _already_pulled(planner_session, source_id, item.id):
            result.skipped_duplicate += 1
            continue
        target_builder = builder or item.builder_hint
        try:
            task = local_memory.add_task(
                planner_session, item.body, builder=target_builder
            )
        except (ValueError, Exception) as exc:  # noqa: BLE001
            logger.debug("backlog dispatch failed for item %s: %s", item.id, exc)
            result.skipped_failed += 1
            result.errors.append(f"{item.id}: {exc}")
            continue
        _mark_pulled(planner_session, source_id, item.id)
        result.pulled += 1
        result.task_ids.append(task["id"])

    return result
