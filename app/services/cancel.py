"""Cooperative cancellation token — adopted from
``local_ai_workbench/shared/cancel.py`` (the user's other project
surveyed on 2026-05-06).

Wraps the existing ``threading.Event``-based cancellation that
``cowork.py`` and ``goals.py`` already use, but adds:

* **Reason capture** — today ``cancel_active_run`` flips the run's
  status but doesn't record *why* (user denied? timeout? sibling
  task failed?). ``CancelToken.cancel(reason=...)`` carries a
  short tag so downstream consumers can pattern-match.
* **Type clarity** — function signatures that take ``CancelToken``
  read better than bare ``threading.Event`` and can't be silently
  swapped for an unrelated event.
* **``OperationCancelled`` exception** — long-running code can
  call ``token.raise_if_cancelled()`` at boundaries instead of
  branching on ``if cancel_event.is_set(): return ...`` everywhere.
* **Backwards compatibility** — ``token.as_event()`` returns the
  underlying ``threading.Event`` so existing call sites that
  expect an event keep working unchanged during the migration.

This is the minimum viable token. We deliberately don't add fancy
features (composability, parent/child cancellation, observers) —
the existing event-based code only needs the four operations
``cancel`` / ``is_cancelled`` / ``wait_with_timeout`` /
``raise_if_cancelled`` and the pattern stays small.
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class OperationCancelled(Exception):
    """Raised by ``CancelToken.raise_if_cancelled()`` when the
    token was cancelled. Carries the cancellation reason so
    callers can decide whether to log / re-raise / swallow."""

    def __init__(self, reason: str = "cancelled", token_label: str = ""):
        message = f"operation cancelled: {reason}" if reason else "operation cancelled"
        if token_label:
            message = f"[{token_label}] {message}"
        super().__init__(message)
        self.reason = reason
        self.token_label = token_label


class CancelToken:
    """Cooperative cancellation handle.

    ``label`` is a short identifier (typically the cowork ``run_id``
    or task id) used in error messages and observability. ``reason``
    is captured on the call to ``cancel`` and exposed via
    ``cancelled_reason``.
    """

    def __init__(self, label: str = ""):
        self._event = threading.Event()
        self._label = str(label or "")
        self._reason = ""
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return self._label

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled_reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> bool:
        """Signal cancellation. Returns True iff this call was the
        one that flipped the token (so callers can do "I cancelled
        it" bookkeeping idempotently). Subsequent calls with a
        different reason DO NOT overwrite the original — first
        cancel wins."""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "cancelled")
            self._event.set()
        return True

    def raise_if_cancelled(self) -> None:
        """Raise ``OperationCancelled`` if the token has been
        cancelled. Idiomatic at the start of long-running stages.
        """
        if self._event.is_set():
            raise OperationCancelled(
                reason=self._reason or "cancelled",
                token_label=self._label,
            )

    def wait_with_timeout(self, seconds: float) -> bool:
        """Sleep up to ``seconds``, returning True if cancelled
        before the timeout, False otherwise. Replaces bare
        ``time.sleep(seconds)`` in cancellable loops. Negative or
        zero ``seconds`` returns immediately with the current
        cancelled state."""
        if seconds is None or seconds <= 0:
            return self._event.is_set()
        return bool(self._event.wait(timeout=float(seconds)))

    def as_event(self) -> threading.Event:
        """Return the underlying ``threading.Event``. Existing
        ``cowork.py`` / ``goals.py`` code that takes a bare event
        can be passed ``token.as_event()`` for an incremental
        migration."""
        return self._event

    def __repr__(self) -> str:  # pragma: no cover
        state = "cancelled" if self.is_cancelled else "active"
        return f"CancelToken(label={self._label!r}, state={state})"


# ============================================================
# Module-level conveniences
# ============================================================

def cancellable_sleep(token: Optional[CancelToken], seconds: float) -> bool:
    """Sleep ``seconds`` but bail out early if ``token`` is
    cancelled. Returns True iff cancellation happened during the
    sleep. Tolerates ``token=None`` (just sleeps).
    """
    if token is None:
        if seconds and seconds > 0:
            time.sleep(seconds)
        return False
    return token.wait_with_timeout(seconds)


def from_event(
    event: Optional[threading.Event], *, label: str = "",
) -> Optional[CancelToken]:
    """Wrap an existing ``threading.Event`` in a CancelToken
    without copying state. Returns None when ``event`` is None so
    callers can pass through optional parameters cleanly.

    Note: subsequent ``cancel(reason=...)`` calls go through the
    token and update both fields atomically, but a direct
    ``event.set()`` from elsewhere will leave ``cancelled_reason``
    empty. New code should use the token's ``cancel`` method.
    """
    if event is None:
        return None
    token = CancelToken(label=label)
    # Replace the internal event with the existing one so both views
    # of the cancellation stay in sync (both signalling AND polling).
    token._event = event
    return token
