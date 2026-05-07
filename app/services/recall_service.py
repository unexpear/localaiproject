"""Recall memory — searchable session history (`docs/design/recall-memory.md`).

Phase 1 surface:

  - ``recall(query, *, since, session, limit)`` returns a list of
    ``RecallHit`` dataclasses ordered by relevance + recency.
  - ``parse_relative_time(spec)`` accepts ``"24h"``, ``"7d"``,
    ``"1w"``, or an ISO timestamp and returns an ISO timestamp the
    rest of the system can compare with.
  - ``format_hits(hits, query)`` renders a citation list for the
    dispatcher's chat output.

Phase 1 reads from `local_memory.list_sessions()` + `get_history`,
so it works equally well against the JSON or DuckDB backend without
caring which is configured. The matcher is Python-side substring +
recency tiebreak — fast enough for single-user logs (~1000s of
turns) and zero new deps. DuckDB FTS becomes a Phase 1.5 swap that
preserves the same return shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.memory import local_memory


# ============================================================
# Time parsing
# ============================================================

#: Match e.g. "24h", "7d", "1w", "30m", "2y" — used by the
#: ``--since`` flag to specify a recency window.
_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([smhdwy])\s*$", re.IGNORECASE)

#: Multipliers from one of {s/m/h/d/w/y} → seconds.
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 7 * 86_400,
    "y": 365 * 86_400,  # rough — we don't bother with leap years
}


class RecallTimeError(ValueError):
    """Raised when a `--since` value can't be parsed as either a
    relative-time spec or an ISO timestamp."""


def parse_relative_time(spec: str, *, now: Optional[datetime] = None) -> str:
    """Parse a ``--since`` value to an ISO timestamp.

    Accepts:
      - Relative: ``"24h"``, ``"7d"``, ``"1w"``, ``"30m"``, ``"2y"``
      - ISO: ``"2026-04-21"``, ``"2026-04-21T15:30:00Z"``, etc.
        (anything ``datetime.fromisoformat`` can grok)

    Returns an ISO 8601 string with trailing ``Z`` for UTC, matching
    the format ``local_memory._now()`` produces.
    """
    if not spec or not isinstance(spec, str):
        raise RecallTimeError(f"--since requires a non-empty value (got {spec!r})")

    spec = spec.strip()
    now = now or datetime.now(timezone.utc)

    match = _RELATIVE_RE.match(spec)
    if match:
        n = int(match.group(1))
        unit = match.group(2).lower()
        seconds = n * _UNIT_SECONDS[unit]
        target = now - timedelta(seconds=seconds)
        return target.isoformat().replace("+00:00", "Z")

    # Try ISO. Accept trailing Z by replacing it with +00:00 first.
    iso_candidate = spec.replace("Z", "+00:00") if spec.endswith("Z") else spec
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError as exc:
        raise RecallTimeError(
            f"--since {spec!r} is not a recognised relative time (e.g. '24h') "
            f"or ISO timestamp (e.g. '2026-04-21'): {exc}"
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


# ============================================================
# Hit dataclass
# ============================================================

@dataclass
class RecallHit:
    """One match in the recall result list."""

    session_id: str
    message_index: int            # position in session.history
    role: str                     # "user" | "assistant" | "system"
    content: str                  # full message content (caller may clip)
    timestamp: Optional[str]
    score: float                  # higher = more relevant
    follow_up: Optional[Dict[str, Any]] = None  # the next message in the thread
    matched_terms: List[str] = field(default_factory=list)


# ============================================================
# Search
# ============================================================

def _tokenize_query(query: str) -> List[str]:
    """Lowercase + split on whitespace; drop empty tokens."""
    if not query:
        return []
    return [t for t in query.lower().split() if t]


def _score_message(content: str, terms: List[str]) -> Tuple[float, List[str]]:
    """Score a single message against the query terms.

    Returns ``(score, matched_terms)``. Score is the number of unique
    terms matched plus a small per-occurrence bonus (so messages
    that mention a term repeatedly rank slightly above a single
    mention). Returns ``(0.0, [])`` for no match.
    """
    if not content or not terms:
        return 0.0, []
    lowered = content.lower()
    matched = []
    occurrences = 0
    for term in terms:
        count = lowered.count(term)
        if count > 0:
            matched.append(term)
            occurrences += count
    if not matched:
        return 0.0, []
    score = float(len(matched)) + (occurrences * 0.05)
    return score, matched


def recall(
    query: str,
    *,
    since: Optional[str] = None,
    session: Optional[str] = None,
    limit: int = 10,
) -> List[RecallHit]:
    """Search chat history across one or all sessions.

    ``since`` is an ISO timestamp (use ``parse_relative_time`` to
    convert ``"24h"`` etc. first). ``session`` restricts to one
    session id; None searches all. ``limit`` caps the returned hit
    count (default 10, max 200 — the same upper bound as
    ``executions search``).
    """
    terms = _tokenize_query(query)
    if not terms:
        return []
    bounded_limit = max(1, min(200, int(limit or 10)))

    sessions = (
        [session] if session else local_memory.list_sessions()
    )

    hits: List[RecallHit] = []
    for sid in sessions:
        history = local_memory.get_history(session_id=sid)
        for index, msg in enumerate(history):
            content = msg.get("content") or ""
            ts = msg.get("timestamp") or ""
            if since and ts and ts < since:
                continue
            score, matched = _score_message(content, terms)
            if score <= 0:
                continue
            follow_up = (
                history[index + 1] if (index + 1) < len(history) else None
            )
            hits.append(RecallHit(
                session_id=sid,
                message_index=index,
                role=str(msg.get("role") or ""),
                content=content,
                timestamp=ts or None,
                score=score,
                follow_up=follow_up,
                matched_terms=matched,
            ))

    # Sort: highest score first; tiebreak by recency (newer = better).
    hits.sort(
        key=lambda h: (h.score, h.timestamp or ""),
        reverse=True,
    )
    return hits[:bounded_limit]


# ============================================================
# Output rendering
# ============================================================

def _truncate(text: str, max_len: int = 220) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def format_hits(hits: List[RecallHit], query: str) -> str:
    """Render a recall result list as a citation block for chat output."""
    if not hits:
        return f"[recall · 0 hits for {query!r}]"
    lines = [f"[recall · {len(hits)} hit(s) for {query!r}]"]
    for i, hit in enumerate(hits, start=1):
        ts = hit.timestamp or "(no ts)"
        lines.append(
            f"{i}. session={hit.session_id} · {ts} · {hit.role}"
        )
        lines.append(f"   {_truncate(hit.content)}")
        if hit.follow_up and hit.follow_up.get("content"):
            other_role = hit.follow_up.get("role") or "?"
            lines.append(
                f"   ↳ {other_role}: {_truncate(hit.follow_up['content'], max_len=180)}"
            )
    return "\n".join(lines)
