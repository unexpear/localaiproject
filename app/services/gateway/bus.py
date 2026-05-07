"""Process-local event bus.

Single source of truth for "something happened on a session". Every dispatch
funnels through `publish` after running, and any number of subscribers (UI
panes, the HTTP gateway, future agents) can register a callback and react.

Subscribers are categorised by **session id** so a Qt main window watching
session "default" doesn't get woken by chatter on session "arch", and the
sentinel `EventBus.ANY` registers a wildcard subscriber that sees every
event (used by the SSE stream).

Thread-safety:
  - publish/subscribe/unsubscribe are guarded by a single lock for state
    mutation; the actual callback dispatch happens OUTSIDE the lock so a
    slow subscriber can't deadlock anyone else.
  - Subscriber exceptions are logged at debug level and swallowed; one
    flaky subscriber can never abort delivery to others.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)

# Public event "kinds" the bus emits — keep this enum-like so subscribers
# can pattern-match without surprises.
KIND_DISPATCH = "dispatch"           # any dispatch_input call completed
KIND_TASK = "task"                   # task lifecycle (queued / done / failed / skipped)
KIND_GATEWAY = "gateway"             # gateway lifecycle (started / stopped)
KIND_WORKSPACE_WRITE = "workspace_write"  # an approved write/delete was applied to disk
KIND_WORKSPACE_DENY = "workspace_deny"    # a pending write/delete was denied
KIND_RULE = "rule"                   # an agent-rules predicate fired
KIND_BROWSER = "browser"             # a browser-automation skill action
KIND_COWORK = "cowork"               # a cowork run lifecycle event


@dataclass
class GatewayEvent:
    kind: str
    session_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0  # populated on publish()


class EventBus:
    """Process-local pub/sub. One instance is created via `default_bus()`."""

    ANY = "*"  # sentinel session id meaning "every event"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # session_id -> list of (subscriber_id, callback)
        self._subs: Dict[str, List[tuple[int, Callable[[GatewayEvent], None]]]] = {}
        self._next_id = 1

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------
    def subscribe(self, session_id: str, callback: Callable[[GatewayEvent], None]) -> int:
        """Register `callback` to receive events for `session_id`. Returns a
        subscription id usable with `unsubscribe`."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            self._subs.setdefault(session_id, []).append((sub_id, callback))
            return sub_id

    def unsubscribe(self, sub_id: int) -> bool:
        """Drop a subscription by id. Returns True if anything was removed."""
        with self._lock:
            for session_id, entries in list(self._subs.items()):
                kept = [e for e in entries if e[0] != sub_id]
                if len(kept) != len(entries):
                    if kept:
                        self._subs[session_id] = kept
                    else:
                        del self._subs[session_id]
                    return True
            return False

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------
    def publish(self, event: GatewayEvent) -> int:
        """Fan `event` out to subscribers of its session_id and to ANY listeners.

        Returns the count of callbacks that were invoked.
        """
        import time
        if not event.timestamp:
            event.timestamp = time.time()

        with self._lock:
            # Snapshot the relevant callback lists so we can release the lock
            # before invoking anything (subscribers may be slow / call back in).
            targets: List[Callable[[GatewayEvent], None]] = []
            if event.session_id is not None:
                targets.extend(cb for _id, cb in self._subs.get(event.session_id, []))
            targets.extend(cb for _id, cb in self._subs.get(self.ANY, []))

        delivered = 0
        for cb in targets:
            try:
                cb(event)
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("EventBus subscriber raised: %s", exc)
        return delivered

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Drop every subscriber. Test-only."""
        with self._lock:
            self._subs.clear()
            self._next_id = 1

    def subscriber_count(self, session_id: Optional[str] = None) -> int:
        """Total subscribers, or just for one session id (or ANY)."""
        with self._lock:
            if session_id is None:
                return sum(len(v) for v in self._subs.values())
            return len(self._subs.get(session_id, []))


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_default_bus: Optional[EventBus] = None
_default_bus_lock = threading.Lock()


def default_bus() -> EventBus:
    global _default_bus
    if _default_bus is None:
        with _default_bus_lock:
            if _default_bus is None:
                _default_bus = EventBus()
    return _default_bus


def reset_default_bus_for_tests() -> None:
    global _default_bus
    with _default_bus_lock:
        _default_bus = None
