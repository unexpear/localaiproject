import json
import re

from app.config import (
    SESSION_COMPACT_KEEP_RECENT_MESSAGES,
    SESSION_COMPACT_TRIGGER_MESSAGES,
    SESSION_SUMMARY_MAX_CHARS,
)
from app.memory import local_memory
from app.models import model_registry, runtime_client

#: System prompt for the compaction model. Substantially expanded over
#: the original one-paragraph version because small local models (the
#: ones running compaction) reliably emit generic dreck like
#: "user discussed code" without explicit good-vs-bad guidance + an
#: example. The goal is a summary that lets a future turn pick up where
#: this one left off in 5-15x fewer tokens than the raw transcript —
#: which means PRESERVING IDENTIFIERS (file paths, function names,
#: error messages, decisions) rather than abstracting them away.
SUMMARY_SYSTEM_PROMPT = (
    "You maintain compact conversation memory for a local AI assistant. "
    "Produce JSON that lets a future turn pick up where this conversation left off "
    "in 5-15x fewer tokens than the raw transcript.\n\n"
    "Strict JSON only with three keys:\n"
    "- facts: stable, persistent context (tech stack, file paths, component names, decisions made). "
    "Up to 8 items.\n"
    "- preferences: how the user wants to be answered (tone, format, approach). Up to 6 items.\n"
    "- open_tasks: unfinished work or follow-ups. Up to 6 items.\n\n"
    "Each item: one short sentence. Concrete, not generic. PRESERVE SPECIFIC IDENTIFIERS "
    "(file paths, function names, error messages, decisions) — they're what makes the memory "
    "useful next turn.\n\n"
    "Good vs bad:\n"
    "  good: \"Debugging OAuth in app/auth.py:142 — JWT decoding throws InvalidSignature\"\n"
    "  bad:  \"User discussed authentication\"\n"
    "  good: \"Project uses Postgres + SQLAlchemy; migrations at db/migrations/\"\n"
    "  bad:  \"Project has a database\"\n\n"
    "Consolidate, don't enumerate. Three turns about the same bug = ONE fact, not three. "
    "Drop the obvious — 'user asked a question' is not a fact.\n\n"
    "Return JSON only. No markdown fences, no explanation."
)


def _empty_summary():
    return {
        "facts": [],
        "preferences": [],
        "open_tasks": [],
    }


def _clean_content(text, limit=220):
    cleaned = (text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _normalize_items(items, max_items):
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        items = []

    cleaned = []
    seen = set()
    for item in items:
        text = _clean_content(str(item or ""), limit=160)
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned[-max_items:]


def _normalize_summary(summary):
    summary = summary or {}
    return {
        "facts": _normalize_items(summary.get("facts", []), max_items=8),
        "preferences": _normalize_items(summary.get("preferences", []), max_items=6),
        "open_tasks": _normalize_items(summary.get("open_tasks", []), max_items=6),
    }


def has_compact_summary(summary):
    normalized = _normalize_summary(summary)
    return any(normalized[key] for key in normalized)


def render_compact_summary(summary):
    normalized = _normalize_summary(summary)
    sections = []
    for key, label in (
        ("facts", "Facts"),
        ("preferences", "Preferences"),
        ("open_tasks", "Open Tasks"),
    ):
        items = normalized[key]
        if not items:
            continue
        sections.append(label + ":\n" + "\n".join(f"- {item}" for item in items))
    return "\n\n".join(sections)


def _render_history(history):
    lines = []
    for entry in history:
        role = entry.get("role", "unknown").capitalize()
        content = _clean_content(entry.get("content", ""))
        if not content:
            continue
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def _summary_for_prompt(summary):
    return json.dumps(_normalize_summary(summary), indent=2)


def build_summary_system_message(summary):
    if not has_compact_summary(summary):
        return None
    return {
        "role": "system",
        "content": (
            "Compact session memory from earlier in the conversation:\n"
            f"{render_compact_summary(summary)}\n\n"
            "Use this as background context. If the recent raw conversation conflicts with it, trust the recent raw conversation."
        ),
    }


def _strip_code_fence(text):
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_summary_response(text):
    if not text or not text.strip():
        return None

    decoder = json.JSONDecoder()
    candidates = [text.strip(), _strip_code_fence(text)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return _normalize_summary(parsed)
        except json.JSONDecodeError:
            pass

        start = candidate.find("{")
        if start == -1:
            continue
        try:
            parsed, _ = decoder.raw_decode(candidate[start:])
            if isinstance(parsed, dict):
                return _normalize_summary(parsed)
        except json.JSONDecodeError:
            continue
    return None


def _merge_summary(existing_summary, additions):
    existing = _normalize_summary(existing_summary)
    additions = _normalize_summary(additions)
    return {
        "facts": _normalize_items(existing["facts"] + additions["facts"], max_items=8),
        "preferences": _normalize_items(existing["preferences"] + additions["preferences"], max_items=6),
        "open_tasks": _normalize_items(existing["open_tasks"] + additions["open_tasks"], max_items=6),
    }


def _classify_item(content):
    lowered = content.lower()
    if re.search(r"\b(prefer|preference|like|love|hate|avoid|want|do not want|don't want)\b", lowered):
        return "preferences"
    if re.search(r"\b(todo|to do|next step|milestone|need to|needs to|pending|follow up|follow-up|task)\b", lowered):
        return "open_tasks"
    return "facts"


def _fallback_summary(existing_summary, history):
    additions = _empty_summary()
    for entry in history[-8:]:
        if entry.get("role") != "user":
            continue
        content = _clean_content(entry.get("content", ""))
        if not content:
            continue
        section = _classify_item(content)
        additions[section].append(content)
    return _merge_summary(existing_summary, additions)


def _generate_summary(existing_summary, history):
    model_choice = model_registry.resolve_model_for_role("reasoning")
    model_name = model_choice.get("model")
    transcript = _render_history(history)
    heuristic_summary = _fallback_summary(existing_summary, history)
    if not transcript:
        return {
            "ok": False,
            "summary": _normalize_summary(existing_summary),
            "method": "noop",
            "model": model_name,
            "error": "no_history_to_compact",
        }

    if model_name:
        summary_request = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Existing compact memory JSON:\n"
                    f"{_summary_for_prompt(existing_summary)}\n\n"
                    "Older conversation turns to compact:\n"
                    f"{transcript}\n\n"
                    "Return updated compact memory JSON only."
                ),
            },
        ]
        result = runtime_client.infer_chat(model_name, messages=summary_request)
        parsed = _parse_summary_response(result["content"]) if result["ok"] else None
        if parsed is not None:
            # Model-as-primary, heuristic-as-fallback-per-bucket.
            # The model produces a real consolidated summary; the
            # heuristic just dumps verbatim user messages by regex
            # bucket. So when the model returns content for a
            # bucket, USE IT — even if the heuristic also has
            # something there, the model's version is consolidated
            # and identifier-preserving.
            #
            # The heuristic still has a job: when the model leaves
            # a bucket empty, an explicit user statement that the
            # heuristic caught (e.g., "I prefer concise answers")
            # shouldn't be lost. So we fill model-empty buckets
            # from the heuristic.
            #
            # Earlier this was inverted (heuristic-as-primary). That
            # blocked model improvements: a generic heuristic
            # `facts=["User said X"]` overrode a much better model
            # `facts=["Debugging OAuth in auth.py:142"]`. Fixed
            # 2026-05-07.
            grounded = _normalize_summary(parsed)
            fallback = _normalize_summary(heuristic_summary)
            for key in grounded:
                if not grounded[key] and fallback[key]:
                    grounded[key] = fallback[key]
            return {
                "ok": True,
                "summary": grounded,
                "method": "model",
                "model": model_name,
                "error": None,
            }

    if has_compact_summary(heuristic_summary):
        return {
            "ok": True,
            "summary": heuristic_summary,
            "method": "fallback",
            "model": model_name,
            "error": None if model_name else "no_summary_model_available",
        }

    return {
        "ok": False,
        "summary": _normalize_summary(existing_summary),
        "method": "failed",
        "model": model_name,
        "error": "summary_generation_failed",
    }


def get_compact_summary(session_id="default"):
    return local_memory.get_session_summary(session_id)


def maybe_compact_session(session_id="default", force=False):
    session = local_memory.get_session(session_id)
    history = list(session.get("history", []))
    existing_summary = session.get("summary", {})

    if len(history) <= SESSION_COMPACT_KEEP_RECENT_MESSAGES:
        return {
            "performed": False,
            "session_id": session_id,
            "reason": "not_enough_history",
            "history_count": len(history),
        }

    if not force and len(history) <= SESSION_COMPACT_TRIGGER_MESSAGES:
        return {
            "performed": False,
            "session_id": session_id,
            "reason": "below_threshold",
            "history_count": len(history),
        }

    compacted_history = history[:-SESSION_COMPACT_KEEP_RECENT_MESSAGES]
    kept_history = history[-SESSION_COMPACT_KEEP_RECENT_MESSAGES:]
    summary_result = _generate_summary(existing_summary, compacted_history)

    if not summary_result["ok"] or not has_compact_summary(summary_result["summary"]):
        return {
            "performed": False,
            "session_id": session_id,
            "reason": summary_result["error"] or "summary_generation_failed",
            "history_count": len(history),
            "model": summary_result.get("model"),
        }

    local_memory.update_session_summary(
        session_id=session_id,
        summary=summary_result["summary"],
        compacted_messages=len(compacted_history),
    )
    local_memory.replace_history(session_id=session_id, history=kept_history)

    return {
        "performed": True,
        "session_id": session_id,
        "method": summary_result["method"],
        "model": summary_result.get("model"),
        "compacted_messages": len(compacted_history),
        "kept_messages": len(kept_history),
        "summary": summary_result["summary"],
    }


def format_compaction_result(result):
    if not result.get("performed"):
        reason = result.get("reason", "skipped")
        if reason == "below_threshold":
            return "Nothing to compact yet. The session is still short enough to keep verbatim."
        if reason == "not_enough_history":
            return "Nothing to compact yet. There are not enough saved turns."
        return f"Compaction skipped: {reason}."

    method = result.get("method", "model")
    model = result.get("model")
    via = f" using {model}" if model and method == "model" else ""
    return (
        f"Compacted {result['compacted_messages']} older messages into structured session memory{via}. "
        f"Kept {result['kept_messages']} recent messages live."
    )
