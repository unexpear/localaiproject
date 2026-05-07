import collections
import copy
import json
import os
import threading
from uuid import uuid4
from datetime import datetime, timezone

from app.config import SESSION_EXECUTION_HISTORY_LIMIT
from app.memory.storage import get_storage

# ============================================================
# In-memory undo / redo stacks (per-process).
# Each entry is a deep-copied snapshot of the FULL memory blob
# captured BEFORE a mutating operation. Lost on restart.
# ============================================================
UNDO_STACK_CAPACITY = 30
_undo_lock = threading.Lock()
_undo_stack: collections.deque = collections.deque(maxlen=UNDO_STACK_CAPACITY)
_redo_stack: collections.deque = collections.deque(maxlen=UNDO_STACK_CAPACITY)


def _push_undo(snapshot):
    """Record `snapshot` (a deep-copy of pre-mutation state) onto the undo stack
    and clear the redo stack — any new edit invalidates pending redos."""
    with _undo_lock:
        _undo_stack.append(snapshot)
        _redo_stack.clear()


def _reset_undo_stacks_for_tests():
    with _undo_lock:
        _undo_stack.clear()
        _redo_stack.clear()

# `MEMORY_FILE` is preserved for compatibility with any external code that
# checks the on-disk path; it now reflects whichever backend is active. With
# the JSON backend (default) it points to local_memory.json; with the DuckDB
# backend it points to local_memory.duckdb.
_storage_init_lock = threading.Lock()
_storage_instance = None


def _storage():
    """Lazy-initialise the active storage backend so tests can override env."""
    global _storage_instance
    if _storage_instance is None:
        with _storage_init_lock:
            if _storage_instance is None:
                _storage_instance = get_storage()
    return _storage_instance


def _reset_storage_for_tests(instance=None):
    """Test hook: drop the cached storage instance, optionally inject a new one."""
    global _storage_instance
    with _storage_init_lock:
        _storage_instance = instance


# Compatibility shim — some readers import this constant.
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "local_memory.json")


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_summary():
    return {
        "facts": [],
        "preferences": [],
        "open_tasks": [],
        "updated_at": None,
        "compacted_messages": 0,
    }


VALID_MODES = ("plan", "build")
DEFAULT_MODE = "plan"


#: Cap on the number of write_backups retained per session. Older
#: backups get evicted at the head of the list once we cross this.
WRITE_BACKUP_CAP = 50

#: Default soft cap on workspace writes per session, before the user
#: must explicitly bump it. Catches "model went off the rails and
#: tries to rewrite 200 files" runaways.
DEFAULT_WRITE_CAP = 20

#: Hard cap on `session.context.notes` length. 32 KB is roughly 6,000
#: words — plenty for a working scratchpad without dominating the chat
#: system prompt. Anything longer is truncated with a marker.
NOTES_MAX_BYTES = 32 * 1024

#: Phase 2 (Letta-inspired) — structured memory blocks. Each block is
#: a labelled, length-bounded slot the model sees as a separate system
#: message; the dispatcher's ``note`` command edits them by name. The
#: defaults below ship with every fresh session; user-defined blocks
#: are merged in alongside.
DEFAULT_MEMORY_BLOCKS = {
    "persona": {
        "description": "How the assistant should present itself",
        "value": "",
        "char_limit": 1024,
        "priority": 0,  # never evicted; ordered first
    },
    "user_preferences": {
        "description": "Stable preferences the user has stated",
        "value": "",
        "char_limit": 4096,
        "priority": 1,
    },
    "current_goal": {
        "description": "What we're working on right now",
        "value": "",
        "char_limit": 2048,
        "priority": 2,
    },
    "working_memory": {
        "description": (
            "Per-turn scratchpad. Backwards-compatible alias of "
            "the legacy `notes` field; flushed on `compact`."
        ),
        "value": "",
        "char_limit": NOTES_MAX_BYTES,  # 32 KB — matches old notes cap
        "priority": 3,
    },
}

#: A user-supplied block label must be a slug (a-z, 0-9, _, -); same
#: rules as template / rule ids so the surfaces feel uniform.
import re as _re
_BLOCK_LABEL_RE = _re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def _default_context():
    return {
        "workspace_path": None,
        "active_skills": [],
        "pending_approval": None,
        "approved_search": None,
        "mode": DEFAULT_MODE,
        # tags: free-form labels attached to the session, used by the
        # rules predicate matcher (`session_tag: production` etc.).
        # Lowercase, deduplicated, no spaces. See agent-rules.md.
        "tags": [],
        # lane_providers: per-lane opt-in cloud routing.
        # Shape: {"reasoning": "anthropic:claude-sonnet-4-5", ...}.
        # Lanes WITHOUT a binding fall back to local Ollama via the
        # existing model registry. See chat-providers.md.
        "lane_providers": {},
        # browser: state for the playwright-driven browser skill.
        # Shape: {approved_origins: [...], active_session_id, page_snapshot}.
        # See browser-automation.md.
        "browser": {
            "approved_origins": [],
            "active_session_id": None,
            "page_snapshot": None,
        },
        # notes: free-form markdown scratchpad for the agent. Read on
        # every chat turn (when non-empty); writable via the `notes`
        # dispatcher command. See `docs/design/notes-buffer.md`.
        # PHASE 2: backwards-compat alias for memory_blocks["working_memory"].
        "notes": "",
        # memory_blocks: Phase 2 structured memory (Letta-inspired).
        # Dict of {label: {description, value, char_limit, priority}}.
        # Empty by default; populated lazily on first read by
        # `_normalize_memory_blocks`, which seeds DEFAULT_MEMORY_BLOCKS
        # and migrates the legacy `notes` string into `working_memory`.
        "memory_blocks": {},
        # write_backups: list of {id, path, previous_content,
        # previous_existed, applied_at, kind} entries — populated when
        # an approved write/delete is applied. See workspace-writes.md.
        "write_backups": [],
        # write_count: total approved writes so far this session
        # (incremented monotonically). When it crosses write_cap the
        # next write is refused with `write_cap_reached`.
        "write_count": 0,
        # write_cap: per-session soft cap. Defaults to DEFAULT_WRITE_CAP;
        # raised on demand via `workspace writes-bump <n>`.
        "write_cap": DEFAULT_WRITE_CAP,
        # prompt_budgets: per-slot character budgets for the layered
        # prompt assembly. Empty {} = use SLOT_BUDGET_DEFAULTS from
        # prompt_assembly.py. Tuned via `prompt set-budget <slot> <n>`.
        # See `docs/design/prompt-assembly.md` Phase 2.
        "prompt_budgets": {},
        # prompt_total_budget: total character cap for the assembled
        # system messages. None = use prompt_assembly.DEFAULT_TOTAL_BUDGET.
        # Tuned via `prompt set-budget total <n>`.
        "prompt_total_budget": None,
        # proactive: state for the patch-taxonomy.md Phase 3 proactive
        # trigger. Tracks when we last checked, when we last emitted a
        # proposal, the consecutive `null` streak, and recent user
        # actions. Empty {} on a fresh session; populated lazily by
        # proactive_service. See `docs/design/patch-taxonomy.md`.
        "proactive": {},
        # classifier: a learned Naive Bayes lane classifier persisted
        # by `app/services/lane_classifier.py`. Empty {} on a fresh
        # session; populated by `classifier train`. See
        # `docs/design/agent-rules.md` (planned Phase 2 — learned
        # router supplementing the rule-based one).
        "classifier": {},
        # classifier_autoroute: when True, the classifier's predictions
        # are blended into the live router as additional lane biases
        # (Phase 5b of agent-rules.md). Off by default — user opts in
        # via ``classifier autoroute on``.
        "classifier_autoroute": False,
        # tool_calls_enabled: when True, the prompt assembly injects
        # a `tool_calls` system slot teaching the model the
        # ```dispatcher``` fence format. Off by default — user opts
        # in via ``tool enable on``. See `docs/design/tool-calling.md`
        # Phase 2.
        "tool_calls_enabled": False,
        # compact_format: when True, prompt-assembly slot producers
        # use dense renderers (strip prose framing, single-line for
        # small dicts, short keys) to fit more information per
        # token. Off by default — user opts in via
        # ``prompt compact on``. See `docs/design/compact-prompt.md`.
        "compact_format": False,
        # instructions: typed, conditional, addressable behavioural
        # rules the model sees on every turn (Phase 1 of
        # custom-instructions.md). List of dicts; see
        # ``_normalize_instruction`` for the shape. Empty by
        # default — opt in via ``instructions add``.
        "instructions": [],
    }


def _normalize_tags(value):
    """Coerce stored tags to a deduplicated list of lowercase strings."""
    if not isinstance(value, list):
        return []
    seen = set()
    cleaned = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        tag = entry.strip().lower().replace(" ", "-")
        if not tag or tag in seen:
            continue
        # Restrict to a-z, 0-9, _, - so tags can be matched cheaply.
        if not all(ch.isalnum() or ch in "_-" for ch in tag):
            continue
        seen.add(tag)
        cleaned.append(tag)
    return cleaned


def _normalize_lane_providers(value):
    """Coerce stored lane→provider bindings to a clean dict.

    Each value must be a non-empty string of the shape ``"provider:model"``.
    Bare ``"model"`` strings are accepted and treated as ``"ollama:<model>"``
    so old data round-trips. Empty / malformed entries are dropped.
    """
    if not isinstance(value, dict):
        return {}
    cleaned = {}
    for lane, binding in value.items():
        if not isinstance(lane, str) or not lane.strip():
            continue
        if not isinstance(binding, str) or not binding.strip():
            continue
        spec = binding.strip()
        if ":" not in spec:
            spec = "ollama:" + spec
        cleaned[lane.strip().lower()] = spec
    return cleaned


def _normalize_browser_state(value):
    """Coerce stored browser state to a clean dict shape."""
    default = {"approved_origins": [], "active_session_id": None, "page_snapshot": None}
    if not isinstance(value, dict):
        return default
    raw_origins = value.get("approved_origins") or []
    if not isinstance(raw_origins, list):
        raw_origins = []
    seen = set()
    origins = []
    for entry in raw_origins:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        origins.append(cleaned)
    active = value.get("active_session_id")
    if active is not None:
        active = str(active).strip() or None
    snapshot = value.get("page_snapshot")
    if snapshot is not None and not isinstance(snapshot, dict):
        snapshot = None
    return {
        "approved_origins": origins,
        "active_session_id": active,
        "page_snapshot": snapshot,
    }


def _normalize_notes(value):
    """Coerce stored notes to a bounded string."""
    if value is None:
        return ""
    text = str(value)
    if len(text.encode("utf-8")) > NOTES_MAX_BYTES:
        # Truncate by character count first, then verify byte length.
        # Append a clear marker so the agent knows content was clipped.
        marker = "\n\n…[truncated]\n"
        budget = NOTES_MAX_BYTES - len(marker.encode("utf-8"))
        # Walk back until we fit; UTF-8 safe.
        encoded = text.encode("utf-8")[:budget]
        text = encoded.decode("utf-8", errors="ignore") + marker
    return text


def _truncate_to_byte_budget(text, byte_limit):
    """Clip ``text`` so its UTF-8 encoding fits in ``byte_limit`` bytes.

    Appends a `\\n\\n…[truncated]\\n` marker when clipping happened so
    the model + the user can tell what they're looking at."""
    if text is None:
        return ""
    text = str(text)
    if len(text.encode("utf-8")) <= byte_limit:
        return text
    marker = "\n\n…[truncated]\n"
    budget = max(0, byte_limit - len(marker.encode("utf-8")))
    encoded = text.encode("utf-8")[:budget]
    return encoded.decode("utf-8", errors="ignore") + marker


def _normalize_memory_block(label, raw, default=None):
    """One block's worth of normalisation.

    ``default`` is the matching entry from ``DEFAULT_MEMORY_BLOCKS``
    (or None for user-defined blocks). Falls back to safe defaults
    when fields are missing / malformed.
    """
    if not isinstance(raw, dict):
        raw = {}
    description = str(
        raw.get("description")
        if raw.get("description") is not None
        else (default or {}).get("description") or ""
    ).strip()
    raw_limit = raw.get("char_limit")
    if raw_limit is None:
        raw_limit = (default or {}).get("char_limit") or 2048
    try:
        char_limit = max(64, min(NOTES_MAX_BYTES, int(raw_limit)))
    except (TypeError, ValueError):
        char_limit = (default or {}).get("char_limit") or 2048
    raw_priority = raw.get("priority")
    if raw_priority is None:
        raw_priority = (default or {}).get("priority")
        if raw_priority is None:
            raw_priority = 100  # user-defined blocks rank after defaults
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        priority = 100
    value = _truncate_to_byte_budget(raw.get("value"), char_limit)
    return {
        "description": description,
        "value": value,
        "char_limit": char_limit,
        "priority": priority,
    }


def _normalize_memory_blocks(value, *, legacy_notes="", migrate_legacy=False):
    """Normalise the `memory_blocks` dict.

    On a fresh / pre-Phase-2 session the dict is empty (or missing).
    We seed it with ``DEFAULT_MEMORY_BLOCKS`` so the predictable
    labels exist for the dispatcher.

    ``migrate_legacy=True`` fires the one-time migration of a legacy
    ``notes`` string into the ``working_memory`` block. The caller
    decides when this is safe — typically only when the input value
    was missing entirely (None / no key in the source dict). Once
    memory_blocks exists we treat it as the source of truth and DON'T
    let the legacy mirror re-introduce cleared content.
    """
    if not isinstance(value, dict):
        value = {}
    cleaned = {}
    # Seed every default block (so dispatcher commands always find a target).
    for label, default in DEFAULT_MEMORY_BLOCKS.items():
        raw = value.get(label) or {}
        cleaned[label] = _normalize_memory_block(label, raw, default=default)
    # Pass-through user-defined blocks; reject malformed labels.
    for label, raw in value.items():
        if label in cleaned:
            continue
        if not isinstance(label, str) or not _BLOCK_LABEL_RE.match(label):
            continue
        cleaned[label] = _normalize_memory_block(label, raw, default=None)

    if migrate_legacy and legacy_notes and not cleaned["working_memory"]["value"]:
        wm = cleaned["working_memory"]
        wm["value"] = _truncate_to_byte_budget(legacy_notes, wm["char_limit"])

    return cleaned


def _default_session():
    return {
        "history": [],
        "executions": [],
        "summary": _default_summary(),
        "context": _default_context(),
    }


def _coerce_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _clean_summary_items(items):
    cleaned = []
    seen = set()
    for item in items:
        text = str(item or "").replace("\r", " ").replace("\n", " ").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _legacy_text_to_facts(text):
    lines = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = line.lower().rstrip(":")
        if normalized in {"facts", "preferences", "open tasks", "recent compacted context"}:
            continue
        line = line.lstrip("-*• ").strip()
        if line:
            lines.append(line)
    return _clean_summary_items(lines)


def _normalize_summary(summary):
    if not isinstance(summary, dict):
        summary = {}

    legacy_facts = _legacy_text_to_facts(summary.get("text"))
    normalized = _default_summary()
    normalized["facts"] = _clean_summary_items(_coerce_list(summary.get("facts")) + legacy_facts)
    normalized["preferences"] = _clean_summary_items(_coerce_list(summary.get("preferences")))
    normalized["open_tasks"] = _clean_summary_items(_coerce_list(summary.get("open_tasks")))
    normalized["updated_at"] = summary.get("updated_at")
    normalized["compacted_messages"] = int(summary.get("compacted_messages", 0) or 0)
    return normalized


def _normalize_context(context):
    if not isinstance(context, dict):
        context = {}

    workspace_path = context.get("workspace_path")
    if workspace_path is not None:
        workspace_path = str(workspace_path).strip() or None

    active_skills = []
    seen = set()
    for skill in _coerce_list(context.get("active_skills")):
        normalized_skill = str(skill or "").strip().lower()
        if not normalized_skill or normalized_skill in seen:
            continue
        seen.add(normalized_skill)
        active_skills.append(normalized_skill)

    normalized = _default_context()
    normalized["workspace_path"] = workspace_path
    normalized["active_skills"] = active_skills
    normalized["pending_approval"] = _normalize_pending_approval(context.get("pending_approval"))
    normalized["approved_search"] = _normalize_approved_search(context.get("approved_search"))
    raw_mode = str(context.get("mode") or DEFAULT_MODE).strip().lower()
    normalized["mode"] = raw_mode if raw_mode in VALID_MODES else DEFAULT_MODE

    normalized["tags"] = _normalize_tags(context.get("tags"))
    normalized["lane_providers"] = _normalize_lane_providers(context.get("lane_providers"))
    normalized["browser"] = _normalize_browser_state(context.get("browser"))
    normalized["write_backups"] = _normalize_write_backups(context.get("write_backups"))
    try:
        write_count = int(context.get("write_count") or 0)
    except (TypeError, ValueError):
        write_count = 0
    normalized["write_count"] = max(0, write_count)
    try:
        write_cap = int(context.get("write_cap") or DEFAULT_WRITE_CAP)
    except (TypeError, ValueError):
        write_cap = DEFAULT_WRITE_CAP
    normalized["write_cap"] = max(1, write_cap)
    # Notes + memory_blocks (Phase 2): seed defaults; one-time migrate
    # the legacy `notes` string into `working_memory` ONLY when no
    # memory_blocks dict has been persisted yet (i.e. pre-Phase-2 data
    # is being read for the first time). After that, memory_blocks is
    # the source of truth — clearing working_memory must not be
    # re-populated from the legacy mirror.
    legacy_notes = _normalize_notes(context.get("notes"))
    raw_blocks = context.get("memory_blocks")
    is_first_migration = not isinstance(raw_blocks, dict) or not raw_blocks
    normalized["memory_blocks"] = _normalize_memory_blocks(
        raw_blocks,
        legacy_notes=legacy_notes,
        migrate_legacy=is_first_migration,
    )
    normalized["notes"] = normalized["memory_blocks"]["working_memory"]["value"]
    normalized["prompt_budgets"] = _normalize_prompt_budgets(context.get("prompt_budgets"))
    normalized["prompt_total_budget"] = _normalize_prompt_total_budget(
        context.get("prompt_total_budget")
    )
    normalized["proactive"] = _normalize_proactive_state(context.get("proactive"))
    normalized["classifier"] = _normalize_classifier_state(context.get("classifier"))
    normalized["classifier_autoroute"] = bool(context.get("classifier_autoroute"))
    normalized["tool_calls_enabled"] = bool(context.get("tool_calls_enabled"))
    normalized["compact_format"] = bool(context.get("compact_format"))
    normalized["instructions"] = _normalize_instructions(context.get("instructions"))
    return normalized


def _normalize_classifier_state(value):
    """Coerce ``context.classifier`` (a serialised
    ``NaiveBayesClassifier`` dict) to a JSON-safe shape. Drops
    anything malformed; partial / unrecognised values become an
    empty dict (= "no model trained yet")."""
    if not isinstance(value, dict):
        return {}
    # All fields are optional, but we accept only the recognised
    # ones so caller can't smuggle arbitrary keys into context.
    out = {}
    vocab = value.get("vocab")
    if isinstance(vocab, list):
        out["vocab"] = [str(t) for t in vocab if isinstance(t, str)]
    log_priors = value.get("log_priors")
    if isinstance(log_priors, dict):
        out["log_priors"] = {
            str(k): float(v) for k, v in log_priors.items()
            if isinstance(v, (int, float))
        }
    log_counts = value.get("log_counts")
    if isinstance(log_counts, dict):
        out["log_counts"] = {}
        for label, counts in log_counts.items():
            if isinstance(counts, list):
                out["log_counts"][str(label)] = [
                    float(c) for c in counts
                    if isinstance(c, (int, float))
                ]
    label_doc_counts = value.get("label_doc_counts")
    if isinstance(label_doc_counts, dict):
        out["label_doc_counts"] = {
            str(k): int(v) for k, v in label_doc_counts.items()
            if isinstance(v, (int, float))
        }
    try:
        out["vocab_limit"] = max(0, int(value.get("vocab_limit") or 0))
    except (TypeError, ValueError):
        out["vocab_limit"] = 0
    return out


def _normalize_proactive_state(value):
    """Coerce ``context.proactive`` to the shape proactive_service
    expects: {last_check_at: float, last_proposal_at: float,
    null_streak: int, recent_action_times: [float], last_response:
    dict|None}. Drops anything else; out-of-range numerics clamp."""
    if not isinstance(value, dict):
        return {}
    out = {}
    try:
        out["last_check_at"] = max(0.0, float(value.get("last_check_at") or 0.0))
    except (TypeError, ValueError):
        out["last_check_at"] = 0.0
    try:
        out["last_proposal_at"] = max(0.0, float(value.get("last_proposal_at") or 0.0))
    except (TypeError, ValueError):
        out["last_proposal_at"] = 0.0
    try:
        out["null_streak"] = max(0, int(value.get("null_streak") or 0))
    except (TypeError, ValueError):
        out["null_streak"] = 0
    times = value.get("recent_action_times")
    cleaned_times = []
    if isinstance(times, list):
        for entry in times:
            try:
                cleaned_times.append(float(entry))
            except (TypeError, ValueError):
                continue
    # Cap stored history (matches proactive_service.MAX_RECENT_ACTION_HISTORY).
    out["recent_action_times"] = cleaned_times[-64:]
    last_response = value.get("last_response")
    if isinstance(last_response, dict):
        out["last_response"] = last_response
    else:
        out["last_response"] = None
    return out


def _normalize_prompt_budgets(value):
    """Coerce stored ``prompt_budgets`` to a {slot_name: int>=0} dict.

    Slot names: lowercase ``[a-z][a-z0-9:_-]{0,40}`` so they match
    the slot producer naming (including ``memory:<label>``)."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for raw_name, raw_budget in value.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip().lower()
        if not name or not _PROMPT_SLOT_NAME_RE.match(name):
            continue
        try:
            budget = int(raw_budget or 0)
        except (TypeError, ValueError):
            continue
        if budget < 0:
            continue
        out[name] = budget
    return out


def _normalize_prompt_total_budget(value):
    if value is None:
        return None
    try:
        budget = int(value or 0)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    return budget


_PROMPT_SLOT_NAME_RE = _re.compile(r"^[a-z][a-z0-9:_-]{0,40}$")


# ============================================================
# Custom instructions (docs/design/custom-instructions.md Phase 1)
# ============================================================

#: The closed set of accepted ``kind`` values. Any other value gets
#: the entry rejected by ``_normalize_instruction``.
CUSTOM_INSTRUCTION_KINDS = (
    "constraint",
    "style",
    "persona",
    "knowledge",
    "override",
)

#: Hard cap on body length per instruction (8 KB). The per-entry
#: ``char_limit`` is clamped between MIN and this MAX; a body longer
#: than the entry's char_limit is truncated at normalisation time.
INSTRUCTION_BODY_MIN_CHARS = 64
INSTRUCTION_BODY_MAX_CHARS = 8_192
INSTRUCTION_DEFAULT_CHAR_LIMIT = 2_048

#: ``id`` slug rules — same shape as block labels but a touch
#: longer to allow ``i_<8 hex>`` defaults plus user-supplied
#: descriptive ids.
_INSTRUCTION_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")

#: ``label`` is optional; same slug rules but trimmed.
_INSTRUCTION_LABEL_RE = _re.compile(r"^[a-z0-9][a-z0-9 _-]{0,60}$")

#: Allowed keys inside ``applies_to`` — anything else is dropped.
_APPLIES_TO_KEYS = ("mode", "workspace", "tags", "lanes")


def _coerce_instruction_id(value):
    """Return a normalised id slug, or None if the input is unusable."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if not _INSTRUCTION_ID_RE.match(cleaned):
        return None
    return cleaned


def _mint_instruction_id(existing_ids):
    """Generate a fresh ``i_<8 hex>`` id that isn't already taken."""
    import secrets
    for _ in range(64):
        candidate = "i_" + secrets.token_hex(4)
        if candidate not in existing_ids:
            return candidate
    # Astronomical odds of getting here; fall back to a longer suffix.
    return "i_" + secrets.token_hex(8)


def _normalize_applies_to(value):
    """Coerce ``applies_to`` to a {key: <validated value>} dict.

    All keys are optional. Unknown keys are dropped. Empty / missing
    keys mean "no constraint on this dimension."
    """
    if not isinstance(value, dict):
        return {}
    out = {}
    mode = value.get("mode")
    if isinstance(mode, str):
        m = mode.strip().lower()
        if m in VALID_MODES:
            out["mode"] = m
    workspace = value.get("workspace")
    if isinstance(workspace, str) and workspace.strip():
        out["workspace"] = workspace.strip()
    tags = value.get("tags")
    if isinstance(tags, list):
        cleaned_tags = []
        seen = set()
        for t in tags:
            if not isinstance(t, str):
                continue
            tag = t.strip().lower().replace(" ", "-")
            if not tag or tag in seen:
                continue
            seen.add(tag)
            cleaned_tags.append(tag)
        if cleaned_tags:
            out["tags"] = cleaned_tags
    lanes = value.get("lanes")
    if isinstance(lanes, list):
        cleaned_lanes = []
        seen = set()
        for ln in lanes:
            if not isinstance(ln, str):
                continue
            lane = ln.strip().lower()
            if not lane or lane in seen:
                continue
            seen.add(lane)
            cleaned_lanes.append(lane)
        if cleaned_lanes:
            out["lanes"] = cleaned_lanes
    return out


def _normalize_instruction(entry, *, existing_ids=()):
    """Validate one instruction entry. Returns the normalised dict
    or None if the entry is unsalvageable.

    Rules:
    * ``kind`` must be in ``CUSTOM_INSTRUCTION_KINDS``.
    * ``body`` must be a non-empty string after strip; truncated to
      the entry's effective ``char_limit``.
    * ``char_limit`` clamped to [MIN, MAX]; 0 / missing → default.
    * ``priority`` clamped to [0, 100]; default 50.
    * ``id`` minted if missing or invalid; collisions resolved by
      minting a fresh one.
    """
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "").strip().lower()
    if kind not in CUSTOM_INSTRUCTION_KINDS:
        return None
    body = str(entry.get("body") or "").strip()
    if not body:
        return None

    try:
        raw_limit = int(entry.get("char_limit") or INSTRUCTION_DEFAULT_CHAR_LIMIT)
    except (TypeError, ValueError):
        raw_limit = INSTRUCTION_DEFAULT_CHAR_LIMIT
    if raw_limit <= 0:
        char_limit = INSTRUCTION_DEFAULT_CHAR_LIMIT
    else:
        char_limit = max(
            INSTRUCTION_BODY_MIN_CHARS,
            min(INSTRUCTION_BODY_MAX_CHARS, raw_limit),
        )
    if len(body) > char_limit:
        body = body[:char_limit]

    try:
        raw_priority = int(entry.get("priority") if entry.get("priority") is not None else 50)
    except (TypeError, ValueError):
        raw_priority = 50
    priority = max(0, min(100, raw_priority))

    enabled = entry.get("enabled")
    if enabled is None:
        enabled = True
    enabled = bool(enabled)

    # Phase 2.5 of custom-instructions.md — `instructions hush
    # <id> --turns N`. When > 0 the entry is treated as disabled
    # by ``_filter_applicable_instructions``; ``handle_chat``
    # decrements every positive value by 1 at end-of-turn so the
    # hush expires after exactly N effective turns.
    try:
        raw_hush = int(entry.get("hushed_remaining") or 0)
    except (TypeError, ValueError):
        raw_hush = 0
    hushed_remaining = max(0, min(1000, raw_hush))

    label_raw = entry.get("label")
    if isinstance(label_raw, str) and label_raw.strip():
        label = label_raw.strip()
    else:
        # Default label = first 40 chars of the body, single-lined.
        flat = " ".join(body.split())
        label = flat[:40]

    instruction_id = _coerce_instruction_id(entry.get("id"))
    if not instruction_id or instruction_id in existing_ids:
        instruction_id = _mint_instruction_id(existing_ids)

    applies_to = _normalize_applies_to(entry.get("applies_to"))

    return {
        "id": instruction_id,
        "label": label,
        "kind": kind,
        "body": body,
        "enabled": enabled,
        "applies_to": applies_to,
        "priority": priority,
        "char_limit": char_limit,
        "hushed_remaining": hushed_remaining,
    }


def _normalize_instructions(value):
    """Coerce stored ``instructions`` to a list of validated dicts.

    Drops entries that fail ``_normalize_instruction``. Rebuilds
    ``existing_ids`` as we go so duplicates within the input get a
    fresh mint.
    """
    if not isinstance(value, list):
        return []
    cleaned = []
    seen_ids = set()
    for entry in value:
        normalised = _normalize_instruction(entry, existing_ids=seen_ids)
        if normalised is None:
            continue
        seen_ids.add(normalised["id"])
        cleaned.append(normalised)
    return cleaned


def _normalize_write_backup(entry):
    if not isinstance(entry, dict):
        return None
    backup_id = str(entry.get("id") or "").strip()
    path = str(entry.get("path") or "").strip()
    if not backup_id or not path:
        return None
    kind = str(entry.get("kind") or "").strip().lower()
    if kind not in {"write", "delete"}:
        return None
    return {
        "id": backup_id,
        "path": path,
        "kind": kind,
        "previous_content": entry.get("previous_content"),  # may be None for create
        "previous_existed": bool(entry.get("previous_existed")),
        "previous_sha256": str(entry.get("previous_sha256") or "") or None,
        "applied_at": entry.get("applied_at") or _now(),
    }


def _normalize_write_backups(value):
    if not isinstance(value, list):
        return []
    cleaned = []
    for entry in value:
        normalized = _normalize_write_backup(entry)
        if normalized is not None:
            cleaned.append(normalized)
    # Cap at WRITE_BACKUP_CAP, keeping the newest entries.
    if len(cleaned) > WRITE_BACKUP_CAP:
        cleaned = cleaned[-WRITE_BACKUP_CAP:]
    return cleaned


def _normalize_pending_approval(action):
    if not isinstance(action, dict):
        return None

    kind = str(action.get("kind") or "").strip().lower()
    if kind not in {"read", "search", "write", "delete", "cowork_plan", "browser_navigate", "bundle", "tool_call"}:
        return None

    origin = str(action.get("origin") or "").strip().lower()
    if origin not in {"chat", "command"}:
        origin = "command"

    # `cowork_plan` is unique among pending-approval kinds in that it's
    # not bound to a workspace path — approval gates a plan, not a file
    # operation. Carry the run_id instead. Other kinds still require a
    # workspace_path (see below).
    if kind == "cowork_plan":
        run_id = str(action.get("run_id") or "").strip() or None
        if not run_id:
            return None
        return {
            "kind": kind,
            "origin": origin,
            "run_id": run_id,
            "requested_at": action.get("requested_at") or _now(),
        }

    # `browser_navigate` gates the FIRST navigation to a new origin —
    # subsequent navigations within an approved origin skip approval.
    if kind == "browser_navigate":
        url = str(action.get("url") or "").strip()
        origin_str = str(action.get("origin_host") or "").strip().lower()
        if not url or not origin_str:
            return None
        return {
            "kind": kind,
            "origin": origin,
            "url": url,
            "origin_host": origin_str,
            "browser_session_id": str(action.get("browser_session_id") or "default").strip(),
            "requested_at": action.get("requested_at") or _now(),
        }

    # `bundle` (patch-taxonomy.md Phase 1, extended in
    # tool-calling.md Phase 3b): N coordinated actions
    # approved/denied/rolled-back atomically. Sub-action kinds:
    #   * ``write`` / ``delete`` — workspace-bound; bundle MUST
    #     carry a ``workspace_path`` and every sub-action shares it.
    #     Atomic rollback via ``write_backups`` on failure.
    #   * ``tool_call`` (Phase 3b) — workspace-less; the bundle
    #     can omit ``workspace_path`` if every sub-action is a
    #     tool_call. Dispatched recursively in order; **fail-stop**
    #     semantics — previously-dispatched tool calls are NOT
    #     undone (no rollback machinery for them).
    # Mixed bundles (write + tool_call) DO accept a workspace; the
    # apply path rolls back writes but leaves tool-call effects.
    if kind == "bundle":
        sub_raw = action.get("sub_actions")
        if not isinstance(sub_raw, list) or not sub_raw:
            return None
        if len(sub_raw) > 10:  # Mirrors patches.BUNDLE_MAX_SUB_PATCHES
            return None

        # First pass: classify the kinds present so we know whether
        # workspace_path is required.
        kinds_present = set()
        for entry in sub_raw:
            if not isinstance(entry, dict):
                return None
            entry_kind = str(entry.get("kind") or "").strip().lower()
            if entry_kind not in {"write", "delete", "tool_call"}:
                return None
            kinds_present.add(entry_kind)

        needs_workspace = bool(kinds_present & {"write", "delete"})
        workspace_path_b = str(action.get("workspace_path") or "").strip() or None
        if needs_workspace and not workspace_path_b:
            return None

        sub_normalised = []
        for entry in sub_raw:
            entry_kind = str(entry.get("kind") or "").strip().lower()
            child = dict(entry)
            if entry_kind in {"write", "delete"}:
                # Inherit workspace_path so callers don't repeat it.
                child.setdefault("workspace_path", workspace_path_b)
            child.setdefault("origin", origin)
            normalised_child = _normalize_pending_approval(child)
            if not normalised_child:
                return None
            # write/delete must share the bundle's workspace_path.
            if entry_kind in {"write", "delete"} and \
               normalised_child.get("workspace_path") != workspace_path_b:
                return None
            sub_normalised.append(normalised_child)

        normalised_bundle = {
            "kind": kind,
            "origin": origin,
            "sub_actions": sub_normalised,
            "requested_at": action.get("requested_at") or _now(),
        }
        if workspace_path_b:
            normalised_bundle["workspace_path"] = workspace_path_b
        rationale = str(action.get("rationale") or "").strip()
        if rationale:
            normalised_bundle["rationale"] = rationale
        original_message = str(action.get("original_message") or "").strip()
        if original_message:
            normalised_bundle["original_message"] = original_message
        reason = str(action.get("reason") or "").strip()
        if reason:
            normalised_bundle["reason"] = reason
        bundle_tier = str(action.get("tier") or "").strip().upper()
        if bundle_tier in {"L1", "L2", "L3"}:
            normalised_bundle["tier"] = bundle_tier
        return normalised_bundle

    # `tool_call` (tool-calling.md Phase 3): the model emitted a
    # ```dispatcher fenced block and the chat-flow detector is
    # surfacing it as an approval-gated proposal. The body becomes
    # ``command`` (a single dispatcher input that runs verbatim on
    # approve via ``dispatch_input``). No workspace required —
    # tool calls can be workspace-less (e.g. ``mode plan``,
    # ``status``, ``models``).
    if kind == "tool_call":
        command = str(action.get("command") or "").strip()
        if not command:
            return None
        normalised_call = {
            "kind": kind,
            "origin": origin,
            "command": command,
            "requested_at": action.get("requested_at") or _now(),
        }
        original_message = str(action.get("original_message") or "").strip()
        if original_message:
            normalised_call["original_message"] = original_message
        reason = str(action.get("reason") or "").strip()
        if reason:
            normalised_call["reason"] = reason
        call_tier = str(action.get("tier") or "").strip().upper()
        if call_tier in {"L1", "L2", "L3"}:
            normalised_call["tier"] = call_tier
        return normalised_call

    workspace_path = str(action.get("workspace_path") or "").strip() or None
    if not workspace_path:
        return None

    normalized = {
        "kind": kind,
        "origin": origin,
        "workspace_path": workspace_path,
        "requested_at": action.get("requested_at") or _now(),
    }

    if kind == "read":
        paths = []
        seen = set()
        for path in _coerce_list(action.get("paths")):
            cleaned = str(path or "").replace("\\", "/").strip().lstrip("./")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            paths.append(cleaned)
        if not paths:
            return None
        normalized["paths"] = paths

    if kind == "search":
        query = str(action.get("query") or "").strip()
        if not query:
            return None
        normalized["query"] = query

    if kind == "write":
        path = str(action.get("path") or "").replace("\\", "/").strip().lstrip("./")
        if not path:
            return None
        normalized["path"] = path
        # `content` MAY be None for chat-driven proposals where the
        # builder will supply content in a follow-up. Phase 1 commands
        # always pass content as a string.
        content = action.get("content")
        if content is not None:
            normalized["content"] = str(content)
        normalized["create"] = bool(action.get("create"))
        prev_hash = str(action.get("previous_content_hash") or "") or None
        if prev_hash:
            normalized["previous_content_hash"] = prev_hash

    if kind == "delete":
        path = str(action.get("path") or "").replace("\\", "/").strip().lstrip("./")
        if not path:
            return None
        normalized["path"] = path

    original_message = str(action.get("original_message") or "").strip()
    if original_message:
        normalized["original_message"] = original_message

    reason = str(action.get("reason") or "").strip()
    if reason:
        normalized["reason"] = reason

    # Phase 2 of patch-taxonomy.md — preserve an explicit ``tier``
    # field if the emitter supplied one. Validators only allow the
    # three known values; anything else is dropped (the dispatcher
    # falls back to KIND_DEFAULT_TIER from impact_statements.py).
    tier = str(action.get("tier") or "").strip().upper()
    if tier in {"L1", "L2", "L3"}:
        normalized["tier"] = tier

    return normalized


def _normalize_approved_search(data):
    if not isinstance(data, dict):
        return None

    query = str(data.get("query") or "").strip() or None
    files = []
    seen = set()
    for path in _coerce_list(data.get("files")):
        cleaned = str(path or "").replace("\\", "/").strip().lstrip("./")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        files.append(cleaned)

    if not query and not files:
        return None

    return {
        "query": query,
        "files": files[:8],
        "updated_at": data.get("updated_at") or _now(),
    }


def _normalize_execution_message(message):
    if not isinstance(message, dict):
        return None

    role = str(message.get("role") or "").strip().lower()
    content = str(message.get("content") or "").strip()
    if role not in {"system", "user", "assistant"} or not content:
        return None

    return {
        "role": role,
        "content": content,
    }


def _normalize_execution_messages(messages):
    normalized = []
    for message in _coerce_list(messages):
        rendered = _normalize_execution_message(message)
        if not rendered:
            continue
        normalized.append(rendered)
    return normalized


def _normalize_execution_workspace_result(result):
    if not isinstance(result, dict) or not result.get("ok"):
        return None

    kind = str(result.get("kind") or "").strip().lower()
    if kind == "read":
        files = []
        for file_result in result.get("files", []):
            if not isinstance(file_result, dict) or not file_result.get("ok"):
                continue
            relative_path = str(file_result.get("relative_path") or "").replace("\\", "/").strip()
            content = str(file_result.get("content_with_line_numbers") or "").strip()
            if not relative_path or not content:
                continue
            files.append(
                {
                    "ok": True,
                    "relative_path": relative_path,
                    "content_with_line_numbers": content,
                    "truncated": bool(file_result.get("truncated")),
                }
            )
        if not files:
            return None
        return {
            "ok": True,
            "kind": "read",
            "files": files[:2],
        }

    if kind == "search":
        matches = []
        for match in result.get("matches", []):
            if not isinstance(match, dict):
                continue
            relative_path = str(match.get("relative_path") or "").replace("\\", "/").strip()
            if not relative_path:
                continue
            matches.append(
                {
                    "relative_path": relative_path,
                    "line_number": int(match.get("line_number", 0) or 0),
                    "line": str(match.get("line") or "").strip(),
                }
            )
        files = []
        seen = set()
        for path in _coerce_list(result.get("files_with_matches")):
            cleaned = str(path or "").replace("\\", "/").strip().lstrip("./")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            files.append(cleaned)
        return {
            "ok": True,
            "kind": "search",
            "query": str(result.get("query") or "").strip() or None,
            "raw_query": str(result.get("raw_query") or "").strip() or None,
            "matches": matches[:12],
            "files_with_matches": files[:6],
            "truncated": bool(result.get("truncated")),
        }

    return None


def _normalize_execution_replay(replay):
    if not isinstance(replay, dict):
        return {"model": None, "messages": [], "approved_workspace_result": None}

    model = str(replay.get("model") or "").strip() or None
    return {
        "model": model,
        "messages": _normalize_execution_messages(replay.get("messages")),
        "approved_workspace_result": _normalize_execution_workspace_result(replay.get("approved_workspace_result")),
    }


def _normalize_execution_approval(approval):
    if not isinstance(approval, dict):
        return {"state": "none", "description": "none"}

    state = str(approval.get("state") or "").strip().lower()
    if state not in {"none", "pending", "used"}:
        state = "none"
    description = str(approval.get("description") or "").strip() or "none"
    return {
        "state": state,
        "description": description,
    }


def _normalize_execution(execution):
    if not isinstance(execution, dict):
        return None

    status = str(execution.get("status") or "").strip().lower()
    if status not in {"completed", "failed", "needs_approval", "no_model"}:
        status = "failed"

    message = str(execution.get("message") or "").strip()
    if not message:
        return None

    return {
        "id": str(execution.get("id") or f"exe_{uuid4().hex[:10]}").strip(),
        "created_at": execution.get("created_at") or _now(),
        "status": status,
        "message": message,
        "message_preview": str(execution.get("message_preview") or message).replace("\r", " ").replace("\n", " ").strip(),
        "requested_lane": str(execution.get("requested_lane") or "").strip() or None,
        "resolved_lane": str(execution.get("resolved_lane") or "").strip() or None,
        "model": str(execution.get("model") or "").strip() or None,
        "error": str(execution.get("error") or "").strip() or None,
        "fallback_used": bool(execution.get("fallback_used")),
        "route_confidence": str(execution.get("route_confidence") or "").strip() or None,
        "route_scores": execution.get("route_scores") if isinstance(execution.get("route_scores"), dict) else {},
        "route_primary_signal": str(execution.get("route_primary_signal") or "").strip() or None,
        "route_context_used": bool(execution.get("route_context_used")),
        "route_context_reason": str(execution.get("route_context_reason") or "").strip() or None,
        "inference_message_count": int(execution.get("inference_message_count", 0) or 0)
        if execution.get("inference_message_count") is not None
        else None,
        "summary_included": bool(execution.get("summary_included")),
        "compaction_performed": bool(execution.get("compaction_performed")),
        "approval": _normalize_execution_approval(execution.get("approval")),
        "replay": _normalize_execution_replay(execution.get("replay")),
        "retry_source_execution_id": str(execution.get("retry_source_execution_id") or "").strip() or None,
    }


def _normalize_executions(executions):
    normalized = []
    for execution in _coerce_list(executions):
        rendered = _normalize_execution(execution)
        if not rendered:
            continue
        normalized.append(rendered)
    return normalized[-SESSION_EXECUTION_HISTORY_LIMIT:]


def _ensure_session(session):
    if not isinstance(session, dict):
        session = {}
    session.setdefault("history", [])
    session["executions"] = _normalize_executions(session.get("executions"))
    session["summary"] = _normalize_summary(session.get("summary"))
    session["context"] = _normalize_context(session.get("context"))
    return session


def _ensure_store(data):
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    for session_id, session in list(data["sessions"].items()):
        data["sessions"][session_id] = _ensure_session(session)
    return data


def _read_store():
    """Backwards-compatible: returns (data, needs_repair_flag).

    The needs-repair branch is no longer reachable now that I/O routes
    through the storage layer (which already returns sanitised data), but
    keeping the tuple shape avoids breaking any caller that still reads it.
    """
    return _ensure_store(_storage().read_store()), False


def load_memory(session_id=None):
    data = _ensure_store(_storage().read_store())
    if session_id is not None:
        return _ensure_session(data["sessions"].get(session_id, _default_session()))
    return data


def save_memory(data):
    _storage().write_store(_ensure_store(data))


def _mutate(fn):
    """Atomic read-modify-write: serialises with other writers via the storage lock.

    `fn(data)` may mutate `data` in place or return a replacement dict.
    Also captures the pre-mutation state into the in-memory undo stack so
    `/undo` can roll back. Captured snapshot is a deep copy taken inside
    the storage lock, so it's a consistent point-in-time view.
    """
    pre = {"snapshot": None}

    def wrapped(raw):
        data = _ensure_store(raw)
        pre["snapshot"] = copy.deepcopy(data)
        result = fn(data)
        if result is None:
            return data
        return _ensure_store(result)

    new_state = _storage().mutate(wrapped)
    if pre["snapshot"] is not None and pre["snapshot"] != new_state:
        _push_undo(pre["snapshot"])
    return new_state


def can_undo():
    with _undo_lock:
        return len(_undo_stack) > 0


def can_redo():
    with _undo_lock:
        return len(_redo_stack) > 0


def undo():
    """Roll back the most recent mutation. Returns True if a state was restored."""
    with _undo_lock:
        if not _undo_stack:
            return False
        target = _undo_stack.pop()

    captured = {"current": None}

    def fn(current):
        captured["current"] = copy.deepcopy(_ensure_store(current))
        return _ensure_store(target)

    # Bypass the local _mutate (which would push another undo snapshot) and go
    # straight to the storage lock so this swap is atomic.
    _storage().mutate(fn)

    if captured["current"] is not None:
        with _undo_lock:
            _redo_stack.append(captured["current"])
    return True


def redo():
    """Re-apply the most recently undone mutation. Returns True on success."""
    with _undo_lock:
        if not _redo_stack:
            return False
        target = _redo_stack.pop()

    captured = {"current": None}

    def fn(current):
        captured["current"] = copy.deepcopy(_ensure_store(current))
        return _ensure_store(target)

    _storage().mutate(fn)

    if captured["current"] is not None:
        with _undo_lock:
            _undo_stack.append(captured["current"])
    return True


def list_sessions():
    data = load_memory()
    return sorted(data.get("sessions", {}).keys())


# ============================================================
# Session linking + task coordination (planner / builder pairs)
# ============================================================

VALID_TASK_STATUSES = ("queued", "in_progress", "done", "failed", "skipped")

#: Lifecycle states for a CoWork run. A run is "active" when status is
#: in {planning, approved, executing, synthesizing}; the dispatcher
#: refuses a second `cowork plan` while the session has an active run.
VALID_COWORK_STATUSES = (
    "planning",
    "approved",
    "executing",
    "synthesizing",
    "done",
    "failed",
    "cancelled",
)
COWORK_ACTIVE_STATUSES = frozenset({"planning", "approved", "executing", "synthesizing"})

#: Hard cap on sub-tasks per cowork plan. The plan parser refuses
#: anything bigger; the design doc fixes the user-facing limit at 10.
MAX_SUBTASKS = 10


def _ensure_coordination_bucket(data):
    """Make sure the top-level `coordination` bucket exists with both keys.

    `links` is a dict mapping `planner_id -> [builder_id, ...]`. For backward
    compatibility we transparently upgrade any string-valued entry written by
    the single-builder Phase-1 schema into a single-element list on read.
    """
    bucket = data.get("coordination")
    if not isinstance(bucket, dict):
        bucket = {}
        data["coordination"] = bucket
    if not isinstance(bucket.get("links"), dict):
        bucket["links"] = {}
    if not isinstance(bucket.get("tasks"), list):
        bucket["tasks"] = []
    # Migrate legacy single-string values into list form.
    for planner, builders in list(bucket["links"].items()):
        if isinstance(builders, str):
            bucket["links"][planner] = [builders] if builders else []
        elif not isinstance(builders, list):
            bucket["links"][planner] = []
        else:
            # Strip empties + dedupe while preserving insertion order.
            seen = set()
            cleaned = []
            for b in builders:
                if not isinstance(b, str) or not b.strip():
                    continue
                bid = b.strip()
                if bid in seen:
                    continue
                seen.add(bid)
                cleaned.append(bid)
            bucket["links"][planner] = cleaned
    return bucket


def _new_task_id():
    return f"task_{uuid4().hex[:12]}"


def get_builders(planner_session):
    """Return the list of builders linked to `planner_session` (insertion order)."""
    sid = str(planner_session or "").strip()
    if not sid:
        return []
    bucket = _ensure_coordination_bucket(load_memory())
    return list((bucket.get("links") or {}).get(sid) or [])


def get_planners(builder_session):
    """Return every planner that has `builder_session` in its builder list."""
    sid = str(builder_session or "").strip()
    if not sid:
        return []
    bucket = _ensure_coordination_bucket(load_memory())
    out = []
    for planner, builders in (bucket.get("links") or {}).items():
        if sid in (builders or []):
            out.append(planner)
    return sorted(out)


def get_link_partners(session_id):
    """Convenience: return both views of `session_id`'s link state.

    Returns: {"as_planner": [builder, ...], "as_builder": [planner, ...]}
    """
    return {
        "as_planner": get_builders(session_id),
        "as_builder": get_planners(session_id),
    }


def get_link(session_id):
    """Backward-compatible single-partner view.

    Returns the FIRST linked builder if `session_id` is a planner, else the
    first planner that links to it. Returns None when nothing is linked.
    Prefer `get_builders` / `get_planners` in new code.
    """
    builders = get_builders(session_id)
    if builders:
        return builders[0]
    planners = get_planners(session_id)
    return planners[0] if planners else None


def add_builder(planner_session, builder_session):
    """Append `builder_session` to `planner_session`'s builder list.

    Idempotent — adding the same builder twice is a no-op. Both sessions
    are auto-created. Returns the planner's full builder list after the
    operation.
    """
    p = str(planner_session or "").strip()
    b = str(builder_session or "").strip()
    if not p or not b:
        raise ValueError("both planner and builder session names are required")
    if p == b:
        raise ValueError("planner and builder must be different sessions")

    captured = {"builders": []}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        current = list(bucket["links"].get(p) or [])
        if b not in current:
            current.append(b)
        bucket["links"][p] = current
        # Persist both sessions.
        data["sessions"].setdefault(p, _default_session())
        data["sessions"].setdefault(b, _default_session())
        data["sessions"][p] = _ensure_session(data["sessions"][p])
        data["sessions"][b] = _ensure_session(data["sessions"][b])
        captured["builders"] = list(current)

    _mutate(fn)
    return {"planner": p, "builders": captured["builders"]}


# Backward-compat alias — old `set_link` semantics are preserved (idempotent add).
def set_link(planner_session, builder_session):
    result = add_builder(planner_session, builder_session)
    # Old return shape: {"planner": ..., "builder": ...} — keep it for callers
    # that expected a single builder.
    return {"planner": result["planner"], "builder": builder_session}


def remove_builder(planner_session, builder_session):
    """Remove ONE builder from a planner's list. Returns True if anything changed."""
    p = str(planner_session or "").strip()
    b = str(builder_session or "").strip()
    if not p or not b:
        return False
    captured = {"removed": False}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        current = list(bucket["links"].get(p) or [])
        if b in current:
            current.remove(b)
            captured["removed"] = True
        if current:
            bucket["links"][p] = current
        else:
            bucket["links"].pop(p, None)

    _mutate(fn)
    return captured["removed"]


def clear_builders(planner_session):
    """Drop ALL builders linked from `planner_session`. Returns True if any existed."""
    p = str(planner_session or "").strip()
    if not p:
        return False
    captured = {"removed": False}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        if p in bucket["links"] and bucket["links"][p]:
            captured["removed"] = True
        bucket["links"].pop(p, None)

    _mutate(fn)
    return captured["removed"]


# Backward-compat alias.
def clear_link(planner_session):
    return clear_builders(planner_session)


def _new_task_record(planner, builder, text):
    return {
        "id": _new_task_id(),
        "planner_session": planner,
        "builder_session": builder,
        "task": text,
        "status": "queued",
        "result": None,
        "created_at": _now(),
        "started_at": None,
        "completed_at": None,
        "execution_id": None,
    }


def add_task(planner_session, task_text, builder=None):
    """Queue a task from `planner_session` to one of its linked builders.

    Selection rules:
      - explicit `builder=` argument → must be in the planner's builder list
      - otherwise: if the planner has exactly one linked builder, use it
      - otherwise: raise ValueError (use `dispatch_to_all` to fan out)

    Returns the new task dict. Raises ValueError on bad input or unresolvable
    builder.
    """
    p = str(planner_session or "").strip()
    text = str(task_text or "").strip()
    target = str(builder or "").strip() if builder else None
    if not p:
        raise ValueError("planner session is required")
    if not text:
        raise ValueError("task text cannot be empty")

    captured = {"task": None}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        builders = list((bucket.get("links") or {}).get(p) or [])
        if not builders:
            raise ValueError(f"session '{p}' is not linked to any builder yet — use `link <builder>`")
        if target is not None:
            if target not in builders:
                raise ValueError(
                    f"builder '{target}' is not linked to '{p}' "
                    f"(linked builders: {', '.join(builders)})"
                )
            chosen = target
        else:
            if len(builders) > 1:
                raise ValueError(
                    f"'{p}' has {len(builders)} linked builders "
                    f"({', '.join(builders)}) — specify @<builder> or use dispatch_to_all"
                )
            chosen = builders[0]
        task = _new_task_record(p, chosen, text)
        bucket["tasks"].append(task)
        captured["task"] = dict(task)

    _mutate(fn)
    return captured["task"]


def dispatch_to_all(planner_session, task_text):
    """Create one queued task per linked builder. Returns the list of new tasks."""
    p = str(planner_session or "").strip()
    text = str(task_text or "").strip()
    if not p:
        raise ValueError("planner session is required")
    if not text:
        raise ValueError("task text cannot be empty")

    captured = {"tasks": []}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        builders = list((bucket.get("links") or {}).get(p) or [])
        if not builders:
            raise ValueError(f"session '{p}' is not linked to any builder yet — use `link <builder>`")
        new_tasks = []
        for b in builders:
            task = _new_task_record(p, b, text)
            bucket["tasks"].append(task)
            new_tasks.append(dict(task))
        captured["tasks"] = new_tasks

    _mutate(fn)
    return captured["tasks"]


def list_tasks(session_id=None, *, status=None):
    """Return tasks involving `session_id` (either side), optionally filtered by status.

    If `session_id` is None, returns ALL tasks — useful for `tasks` admin command.
    Newest-first ordering.
    """
    bucket = _ensure_coordination_bucket(load_memory())
    tasks = list(bucket.get("tasks") or [])
    if session_id:
        sid = str(session_id).strip()
        tasks = [t for t in tasks if t.get("planner_session") == sid or t.get("builder_session") == sid]
    if status:
        s = str(status).strip().lower()
        tasks = [t for t in tasks if t.get("status") == s]
    return list(reversed(tasks))


def get_inbox(builder_session, *, status="queued"):
    """Return tasks queued for `builder_session`, oldest-first (FIFO consume order)."""
    sid = str(builder_session or "").strip()
    if not sid:
        return []
    bucket = _ensure_coordination_bucket(load_memory())
    return [
        t for t in (bucket.get("tasks") or [])
        if t.get("builder_session") == sid and (status is None or t.get("status") == status)
    ]


def next_task(builder_session):
    """Pop the next queued task for `builder_session`, marking it `in_progress`.

    Returns the task dict, or None if the inbox is empty.
    """
    sid = str(builder_session or "").strip()
    if not sid:
        return None

    captured = {"task": None}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        for task in bucket.get("tasks") or []:
            if task.get("builder_session") == sid and task.get("status") == "queued":
                task["status"] = "in_progress"
                task["started_at"] = _now()
                captured["task"] = dict(task)
                return

    _mutate(fn)
    return captured["task"]


def mark_task(task_id, *, status, result=None, execution_id=None):
    """Update a task's status and optionally its result and the execution that ran it."""
    tid = str(task_id or "").strip()
    if not tid:
        return None
    if status not in VALID_TASK_STATUSES:
        raise ValueError(f"unknown status '{status}' (expected: {', '.join(VALID_TASK_STATUSES)})")

    captured = {"task": None}

    def fn(data):
        bucket = _ensure_coordination_bucket(data)
        for task in bucket.get("tasks") or []:
            if task.get("id") == tid:
                task["status"] = status
                if result is not None:
                    task["result"] = str(result)
                if execution_id is not None:
                    task["execution_id"] = execution_id
                if status in ("done", "failed", "skipped"):
                    task["completed_at"] = _now()
                captured["task"] = dict(task)
                return

    _mutate(fn)
    return captured["task"]


def get_task(task_id):
    tid = str(task_id or "").strip()
    if not tid:
        return None
    bucket = _ensure_coordination_bucket(load_memory())
    for task in bucket.get("tasks") or []:
        if task.get("id") == tid:
            return dict(task)
    return None


def in_progress_task_for(builder_session):
    """Return the currently in-progress task for a builder, if any."""
    sid = str(builder_session or "").strip()
    if not sid:
        return None
    bucket = _ensure_coordination_bucket(load_memory())
    for task in bucket.get("tasks") or []:
        if task.get("builder_session") == sid and task.get("status") == "in_progress":
            return dict(task)
    return None


# ============================================================
# Custom slash commands (app-level, not per-session)
# ============================================================

CUSTOM_COMMAND_NAME_RE = "^[a-z][a-z0-9_-]{0,40}$"


def _ensure_custom_commands_bucket(data):
    if not isinstance(data.get("custom_commands"), dict):
        data["custom_commands"] = {}
    return data["custom_commands"]


def list_custom_commands():
    data = load_memory()
    bucket = data.get("custom_commands") or {}
    return {
        name: dict(entry)
        for name, entry in sorted(bucket.items())
        if isinstance(entry, dict) and isinstance(entry.get("prompt"), str)
    }


def get_custom_command(name):
    cleaned = str(name or "").strip().lower()
    if not cleaned:
        return None
    bucket = (load_memory().get("custom_commands") or {})
    entry = bucket.get(cleaned)
    if not isinstance(entry, dict):
        return None
    if not isinstance(entry.get("prompt"), str):
        return None
    return dict(entry)


def add_custom_command(name, prompt):
    import re

    cleaned_name = str(name or "").strip().lower()
    cleaned_prompt = str(prompt or "").strip()
    if not re.match(CUSTOM_COMMAND_NAME_RE, cleaned_name):
        raise ValueError(f"invalid command name '{name}' — must be lowercase a-z, 0-9, _ or -, max 40 chars")
    if not cleaned_prompt:
        raise ValueError("prompt cannot be empty")

    captured = {}

    def fn(data):
        bucket = _ensure_custom_commands_bucket(data)
        bucket[cleaned_name] = {
            "name": cleaned_name,
            "prompt": cleaned_prompt,
            "created_at": _now(),
        }
        captured["entry"] = dict(bucket[cleaned_name])

    _mutate(fn)
    return captured["entry"]


def remove_custom_command(name):
    cleaned = str(name or "").strip().lower()
    if not cleaned:
        return False

    captured = {"removed": False}

    def fn(data):
        bucket = _ensure_custom_commands_bucket(data)
        if cleaned in bucket:
            del bucket[cleaned]
            captured["removed"] = True

    _mutate(fn)
    return captured["removed"]


def create_session(session_id):
    def fn(data):
        data["sessions"].setdefault(session_id, _default_session())
        data["sessions"][session_id] = _ensure_session(data["sessions"][session_id])
    final = _mutate(fn)
    return final["sessions"][session_id]


def get_session(session_id="default"):
    return load_memory(session_id=session_id)


def get_history(session_id="default", limit=None):
    history = load_memory(session_id=session_id).get("history", [])
    if limit is None:
        return history
    return history[-limit:]


def get_executions(session_id="default", limit=None):
    executions = load_memory(session_id=session_id).get("executions", [])
    if limit is None:
        return executions
    return executions[-limit:]


def get_execution(session_id="default", execution_id=None):
    executions = get_executions(session_id=session_id)
    if not executions:
        return None

    if execution_id in {None, "", "last", "latest"}:
        return executions[-1]

    target = str(execution_id).strip()
    for execution in executions:
        if execution.get("id") == target:
            return execution
    return None


def search_executions(
    session_id="default",
    *,
    query=None,
    lane=None,
    status=None,
    since=None,
    limit=20,
):
    """Return recent executions matching the supplied filters, newest first.

    Routes through the active storage backend's `search_executions` so that
    DuckDB can answer with SQL while JSON falls back to a linear scan.
    """
    return _storage().search_executions(
        session_id,
        query=query,
        lane=lane,
        status=status,
        since=since,
        limit=limit,
    )


def get_session_summary(session_id="default"):
    return load_memory(session_id=session_id).get("summary", _default_summary())


def get_session_context(session_id="default"):
    return load_memory(session_id=session_id).get("context", _default_context())


def get_pending_approval(session_id="default"):
    return get_session_context(session_id=session_id).get("pending_approval")


def get_approved_search(session_id="default"):
    return get_session_context(session_id=session_id).get("approved_search")


def update_session_summary(session_id="default", summary=None, compacted_messages=0):
    captured = {}

    def fn(data):
        session = _ensure_session(data["sessions"].setdefault(session_id, _default_session()))
        current_summary = session["summary"]
        next_summary = _normalize_summary(summary if summary is not None else current_summary)
        next_summary["updated_at"] = _now()
        next_summary["compacted_messages"] = current_summary.get("compacted_messages", 0) + compacted_messages
        session["summary"] = next_summary
        data["sessions"][session_id] = session
        captured["summary"] = next_summary

    _mutate(fn)
    return captured["summary"]


def update_session_context(session_id="default", context=None):
    captured = {}

    def fn(data):
        session = _ensure_session(data["sessions"].setdefault(session_id, _default_session()))
        next_context = _normalize_context(context if context is not None else session.get("context"))
        session["context"] = next_context
        data["sessions"][session_id] = session
        captured["context"] = next_context

    _mutate(fn)
    return captured["context"]


def set_workspace(session_id="default", workspace_path=None):
    context = get_session_context(session_id)
    context["workspace_path"] = workspace_path
    context["approved_search"] = None
    return update_session_context(session_id=session_id, context=context)


def set_active_skills(session_id="default", skills=None):
    context = get_session_context(session_id)
    context["active_skills"] = list(skills or [])
    return update_session_context(session_id=session_id, context=context)


def get_mode(session_id="default"):
    return get_session_context(session_id=session_id).get("mode", DEFAULT_MODE)


def set_mode(session_id="default", mode=DEFAULT_MODE):
    cleaned = str(mode or "").strip().lower()
    if cleaned not in VALID_MODES:
        raise ValueError(f"unknown mode '{mode}' (expected: {', '.join(VALID_MODES)})")
    context = get_session_context(session_id)
    context["mode"] = cleaned
    update_session_context(session_id=session_id, context=context)
    return cleaned


def set_pending_approval(session_id="default", pending_approval=None):
    context = get_session_context(session_id)
    context["pending_approval"] = pending_approval
    return update_session_context(session_id=session_id, context=context)


def clear_pending_approval(session_id="default"):
    return set_pending_approval(session_id=session_id, pending_approval=None)


def set_approved_search(session_id="default", approved_search=None):
    context = get_session_context(session_id)
    context["approved_search"] = approved_search
    return update_session_context(session_id=session_id, context=context)


def clear_approved_search(session_id="default"):
    return set_approved_search(session_id=session_id, approved_search=None)


# ============================================================
# Workspace write_backups + cap
# ============================================================

def get_write_backups(session_id="default"):
    """Return the list of write backups for a session (most-recent last)."""
    backups = get_session_context(session_id=session_id).get("write_backups") or []
    return list(backups)


def get_write_count(session_id="default"):
    """Total approved writes recorded so far this session."""
    return int(get_session_context(session_id=session_id).get("write_count") or 0)


def get_write_cap(session_id="default"):
    """Current per-session soft cap on writes."""
    return int(
        get_session_context(session_id=session_id).get("write_cap") or DEFAULT_WRITE_CAP
    )


def set_write_cap(session_id="default", cap=DEFAULT_WRITE_CAP):
    """Bump (or lower) the per-session write cap. Refuses non-positive caps."""
    cleaned = max(1, int(cap or DEFAULT_WRITE_CAP))
    context = get_session_context(session_id)
    context["write_cap"] = cleaned
    update_session_context(session_id=session_id, context=context)
    return cleaned


def add_write_backup(session_id, backup):
    """Append a backup entry and bump the write_count by 1.

    Caller is responsible for providing a normalised dict (see
    `_normalize_write_backup`). Older entries past WRITE_BACKUP_CAP get
    dropped by the normaliser the next time the context is rehydrated.
    """
    context = get_session_context(session_id)
    backups = list(context.get("write_backups") or [])
    backups.append(backup)
    if len(backups) > WRITE_BACKUP_CAP:
        backups = backups[-WRITE_BACKUP_CAP:]
    context["write_backups"] = backups
    context["write_count"] = int(context.get("write_count") or 0) + 1
    update_session_context(session_id=session_id, context=context)
    return backup


def pop_write_backup(session_id="default"):
    """Remove and return the most recent backup, or None if list is empty."""
    context = get_session_context(session_id)
    backups = list(context.get("write_backups") or [])
    if not backups:
        return None
    popped = backups.pop()
    context["write_backups"] = backups
    update_session_context(session_id=session_id, context=context)
    return popped


def clear_write_backups(session_id="default"):
    context = get_session_context(session_id)
    context["write_backups"] = []
    context["write_count"] = 0
    update_session_context(session_id=session_id, context=context)


def decrement_write_count(session_id="default"):
    """Decrement the per-session write_count by 1, floored at 0.

    Used by the undo path so the soft cap reflects writes that were
    rolled back. Symmetric with `add_write_backup` bumping the count
    by 1 on apply.
    """
    context = get_session_context(session_id)
    current = int(context.get("write_count") or 0)
    context["write_count"] = max(0, current - 1)
    update_session_context(session_id=session_id, context=context)
    return context["write_count"]


# ============================================================
# Notes buffer (free-form per-session markdown scratchpad)
# ============================================================

def get_notes(session_id="default"):
    """Return the notes buffer for a session (always a string).

    Phase 2: backwards-compat alias — returns the value of the
    ``working_memory`` memory block. Old callers see the same content
    they always have."""
    return get_memory_block_value(session_id=session_id, label="working_memory")


def set_notes(session_id="default", notes=""):
    """Replace the notes buffer wholesale. Truncates above the
    ``working_memory`` block's char_limit (= NOTES_MAX_BYTES = 32 KB).

    Phase 2: backwards-compat alias — writes to the working_memory
    block."""
    return set_memory_block_value(
        session_id=session_id, label="working_memory", value=notes,
    )


def append_notes(session_id="default", text="", *, prefix_timestamp=False):
    """Append text to the end of the working_memory block.

    A leading newline separator is inserted between existing content
    and the new text. With ``prefix_timestamp=True`` an ISO timestamp
    appears as a markdown subheading above the new text.
    """
    if not text:
        return get_notes(session_id=session_id)
    current = get_notes(session_id=session_id)
    chunks = []
    if current:
        chunks.append(current.rstrip("\n"))
    if prefix_timestamp:
        chunks.append(f"\n## {_now()}\n{text}")
    else:
        chunks.append(text)
    next_text = "\n".join(chunks)
    if not next_text.endswith("\n"):
        next_text += "\n"
    return set_notes(session_id=session_id, notes=next_text)


def clear_notes(session_id="default"):
    """Clear the working_memory block."""
    return set_notes(session_id=session_id, notes="")


# ============================================================
# Memory blocks (Phase 2 — Letta-inspired structured memory)
# ============================================================

def get_memory_blocks(session_id="default"):
    """Return the full ``{label: block}`` dict for a session."""
    return dict(
        get_session_context(session_id=session_id).get("memory_blocks") or {}
    )


def get_memory_block(session_id="default", label=""):
    """Return one block's full dict, or ``None`` if not registered."""
    if not label:
        return None
    blocks = get_memory_blocks(session_id=session_id)
    block = blocks.get(label)
    return dict(block) if block else None


def get_memory_block_value(session_id="default", label=""):
    """Return one block's value (string), or ``""`` if missing."""
    block = get_memory_block(session_id=session_id, label=label)
    if block is None:
        return ""
    return block.get("value") or ""


def set_memory_block_value(session_id="default", label="", value=""):
    """Replace a block's value (truncated to its char_limit)."""
    if not label or not _BLOCK_LABEL_RE.match(label):
        raise ValueError(f"invalid block label {label!r}")
    context = get_session_context(session_id)
    blocks = dict(context.get("memory_blocks") or {})
    existing = blocks.get(label) or {}
    default = DEFAULT_MEMORY_BLOCKS.get(label)
    char_limit = (existing or {}).get("char_limit") or (
        (default or {}).get("char_limit") or 2048
    )
    blocks[label] = _normalize_memory_block(
        label,
        {**existing, "value": _truncate_to_byte_budget(value, char_limit)},
        default=default,
    )
    context["memory_blocks"] = blocks
    update_session_context(session_id=session_id, context=context)
    return blocks[label]["value"]


def set_memory_block_description(session_id="default", label="", description=""):
    """Update a block's description (no length limit but normalised)."""
    if not label or not _BLOCK_LABEL_RE.match(label):
        raise ValueError(f"invalid block label {label!r}")
    context = get_session_context(session_id)
    blocks = dict(context.get("memory_blocks") or {})
    existing = blocks.get(label) or {}
    default = DEFAULT_MEMORY_BLOCKS.get(label)
    blocks[label] = _normalize_memory_block(
        label,
        {**existing, "description": str(description or "").strip()},
        default=default,
    )
    context["memory_blocks"] = blocks
    update_session_context(session_id=session_id, context=context)
    return blocks[label]["description"]


def set_memory_block_budget(session_id="default", label="", char_limit=2048):
    """Raise / lower a block's char_limit. Re-truncates the existing
    value if the new limit is tighter."""
    if not label or not _BLOCK_LABEL_RE.match(label):
        raise ValueError(f"invalid block label {label!r}")
    try:
        cleaned = max(64, min(NOTES_MAX_BYTES, int(char_limit)))
    except (TypeError, ValueError):
        raise ValueError(f"char_limit must be an integer (got {char_limit!r})")
    context = get_session_context(session_id)
    blocks = dict(context.get("memory_blocks") or {})
    existing = blocks.get(label) or {}
    default = DEFAULT_MEMORY_BLOCKS.get(label)
    blocks[label] = _normalize_memory_block(
        label,
        {**existing, "char_limit": cleaned},
        default=default,
    )
    context["memory_blocks"] = blocks
    update_session_context(session_id=session_id, context=context)
    return blocks[label]["char_limit"]


def clear_memory_block(session_id="default", label=""):
    """Clear a block's VALUE (keeps description + char_limit)."""
    return set_memory_block_value(session_id=session_id, label=label, value="")


def remove_memory_block(session_id="default", label=""):
    """Drop a user-defined block entirely. Default blocks are
    re-seeded by the normaliser; calling this on a default block
    is a clear-only operation. Returns True if a block was removed."""
    if not label:
        return False
    if label in DEFAULT_MEMORY_BLOCKS:
        # Don't let users delete the seeded blocks; just clear them.
        clear_memory_block(session_id=session_id, label=label)
        return False
    context = get_session_context(session_id)
    blocks = dict(context.get("memory_blocks") or {})
    if label not in blocks:
        return False
    del blocks[label]
    context["memory_blocks"] = blocks
    update_session_context(session_id=session_id, context=context)
    return True


# ============================================================
# Custom instructions CRUD (custom-instructions.md Phase 1)
# ============================================================

def get_instructions(session_id="default"):
    """Return the full ordered list of instruction dicts for a
    session. Always returns a list (empty when none configured)."""
    return list(
        get_session_context(session_id=session_id).get("instructions") or []
    )


def get_instruction(session_id="default", instruction_id=""):
    """Return a single instruction dict by id, or None if missing."""
    if not instruction_id:
        return None
    target = str(instruction_id).strip().lower()
    for entry in get_instructions(session_id=session_id):
        if entry.get("id") == target:
            return dict(entry)
    return None


def add_instruction(session_id="default", **fields):
    """Append a new instruction. Returns the normalised dict (with
    its assigned ``id``) or raises ``ValueError`` when the input is
    invalid (unknown kind / blank body / etc.)."""
    context = get_session_context(session_id)
    instructions = list(context.get("instructions") or [])
    existing_ids = {e.get("id") for e in instructions if e.get("id")}
    normalised = _normalize_instruction(fields, existing_ids=existing_ids)
    if normalised is None:
        raise ValueError(
            "invalid instruction: kind must be one of "
            f"{CUSTOM_INSTRUCTION_KINDS} and body must be non-empty"
        )
    instructions.append(normalised)
    context["instructions"] = instructions
    update_session_context(session_id=session_id, context=context)
    return dict(normalised)


def update_instruction(session_id="default", instruction_id="", **changes):
    """Mutate an existing instruction's fields in place. Preserves
    the original id. Returns the new normalised dict, or None if no
    entry matches.

    ``changes`` is a partial dict of any subset of the instruction
    fields (``label``, ``kind``, ``body``, ``enabled``,
    ``applies_to``, ``priority``, ``char_limit``).
    """
    if not instruction_id:
        return None
    target = str(instruction_id).strip().lower()
    context = get_session_context(session_id)
    instructions = list(context.get("instructions") or [])
    found_index = None
    for idx, entry in enumerate(instructions):
        if entry.get("id") == target:
            found_index = idx
            break
    if found_index is None:
        return None
    merged = dict(instructions[found_index])
    merged.update(changes)
    merged["id"] = target  # never let an update mint a new id
    other_ids = {
        e.get("id") for i, e in enumerate(instructions)
        if i != found_index and e.get("id")
    }
    normalised = _normalize_instruction(merged, existing_ids=other_ids)
    if normalised is None:
        return None
    # Preserve the original id even if the normaliser would have
    # minted a new one for some edge case (it shouldn't, given the
    # collision guard above, but be defensive).
    normalised["id"] = target
    instructions[found_index] = normalised
    context["instructions"] = instructions
    update_session_context(session_id=session_id, context=context)
    return dict(normalised)


def remove_instruction(session_id="default", instruction_id=""):
    """Drop an instruction by id. Returns True if removed, False
    when no entry matched."""
    if not instruction_id:
        return False
    target = str(instruction_id).strip().lower()
    context = get_session_context(session_id)
    instructions = list(context.get("instructions") or [])
    new_list = [e for e in instructions if e.get("id") != target]
    if len(new_list) == len(instructions):
        return False
    context["instructions"] = new_list
    update_session_context(session_id=session_id, context=context)
    return True


def set_instruction_enabled(session_id="default", instruction_id="", enabled=True):
    """Toggle a single instruction's enabled flag. Returns the new
    value, or None if no entry matched."""
    updated = update_instruction(
        session_id=session_id,
        instruction_id=instruction_id,
        enabled=bool(enabled),
    )
    if updated is None:
        return None
    return updated["enabled"]


def clear_instructions(session_id="default"):
    """Drop every instruction. Returns the count removed."""
    context = get_session_context(session_id)
    instructions = list(context.get("instructions") or [])
    if not instructions:
        return 0
    context["instructions"] = []
    update_session_context(session_id=session_id, context=context)
    return len(instructions)


def hush_instruction(session_id="default", instruction_id="", turns=1):
    """Phase 2.5: temporarily mute an instruction for ``turns`` chat
    turns. The entry stays in the list with ``enabled=True`` but is
    treated as disabled by the filter while ``hushed_remaining > 0``.
    ``handle_chat`` decrements at end-of-turn so the entry re-arms
    automatically after ``turns`` turns.

    Returns the new ``hushed_remaining`` value, or ``None`` when no
    entry matches.
    """
    if not instruction_id:
        return None
    try:
        turn_count = max(0, min(1000, int(turns or 0)))
    except (TypeError, ValueError):
        return None
    updated = update_instruction(
        session_id=session_id,
        instruction_id=instruction_id,
        hushed_remaining=turn_count,
    )
    if updated is None:
        return None
    return updated["hushed_remaining"]


def tick_hushed_instructions(session_id="default"):
    """Decrement every positive ``hushed_remaining`` by 1. Called at
    the end of each chat turn so hushes expire automatically.

    No-op when no entry has a positive counter (the common case),
    so the cost is one read per turn unless the user actually
    hushed something. Returns the count of entries that were ticked.
    """
    context = get_session_context(session_id)
    instructions = list(context.get("instructions") or [])
    ticked = 0
    changed = False
    new_list = []
    for entry in instructions:
        remaining = int(entry.get("hushed_remaining") or 0)
        if remaining > 0:
            new_entry = dict(entry)
            new_entry["hushed_remaining"] = remaining - 1
            new_list.append(new_entry)
            ticked += 1
            changed = True
        else:
            new_list.append(entry)
    if changed:
        context["instructions"] = new_list
        update_session_context(session_id=session_id, context=context)
    return ticked


# ============================================================
# Prompt-assembly budgets (Phase 2 of prompt-assembly.md)
# ============================================================

def get_prompt_budgets(session_id="default"):
    """Return the {slot_name: char_budget} override dict. Empty when
    the session uses the SLOT_BUDGET_DEFAULTS verbatim."""
    return dict(get_session_context(session_id=session_id).get("prompt_budgets") or {})


def get_prompt_total_budget(session_id="default"):
    """Return the override total cap, or None if using DEFAULT_TOTAL_BUDGET."""
    return get_session_context(session_id=session_id).get("prompt_total_budget")


def set_prompt_budget(session_id="default", slot="", char_budget=0):
    """Override one slot's char_budget for this session. Pass
    char_budget <= 0 to clear the override (the slot then uses the
    SLOT_BUDGET_DEFAULTS entry instead)."""
    name = str(slot or "").strip().lower()
    if not name or not _PROMPT_SLOT_NAME_RE.match(name):
        raise ValueError(
            f"invalid slot name {slot!r} (lowercase a-z/0-9/:_- only)"
        )
    try:
        budget = int(char_budget or 0)
    except (TypeError, ValueError):
        raise ValueError(f"char_budget must be an integer, got {char_budget!r}")
    context = get_session_context(session_id)
    budgets = dict(context.get("prompt_budgets") or {})
    if budget <= 0:
        budgets.pop(name, None)
    else:
        budgets[name] = budget
    context["prompt_budgets"] = budgets
    update_session_context(session_id=session_id, context=context)
    return budgets


def clear_prompt_budgets(session_id="default"):
    """Drop all per-slot overrides. The session reverts to the
    SLOT_BUDGET_DEFAULTS table from prompt_assembly.py."""
    context = get_session_context(session_id)
    context["prompt_budgets"] = {}
    update_session_context(session_id=session_id, context=context)
    return {}


def set_prompt_total_budget(session_id="default", total_budget=None):
    """Override the total-budget cap for this session. Pass None or
    <= 0 to revert to DEFAULT_TOTAL_BUDGET."""
    if total_budget is None:
        normalised = None
    else:
        try:
            value = int(total_budget or 0)
        except (TypeError, ValueError):
            raise ValueError(
                f"total_budget must be an integer, got {total_budget!r}"
            )
        normalised = value if value > 0 else None
    context = get_session_context(session_id)
    context["prompt_total_budget"] = normalised
    update_session_context(session_id=session_id, context=context)
    return normalised


# ============================================================
# Session tags (used by the agent-rules predicate matcher)
# ============================================================

def get_tags(session_id="default"):
    """Return the list of tags attached to a session."""
    return list(get_session_context(session_id=session_id).get("tags") or [])


def add_tag(session_id="default", tag=""):
    """Append a tag to the session if not already present. Returns the
    full updated tag list. Tags are normalised (lowercase, no spaces)."""
    cleaned = _normalize_tags([tag])
    if not cleaned:
        raise ValueError(f"invalid tag {tag!r} (lowercase a-z/0-9/_/- only)")
    new_tag = cleaned[0]
    context = get_session_context(session_id)
    tags = list(context.get("tags") or [])
    if new_tag not in tags:
        tags.append(new_tag)
    context["tags"] = tags
    update_session_context(session_id=session_id, context=context)
    return tags


def remove_tag(session_id="default", tag=""):
    """Drop a tag from the session. Returns True if removed."""
    target = (tag or "").strip().lower().replace(" ", "-")
    context = get_session_context(session_id)
    tags = list(context.get("tags") or [])
    if target not in tags:
        return False
    tags = [t for t in tags if t != target]
    context["tags"] = tags
    update_session_context(session_id=session_id, context=context)
    return True


def set_tags(session_id="default", tags=None):
    """Replace the tag list wholesale."""
    context = get_session_context(session_id)
    context["tags"] = _normalize_tags(list(tags or []))
    return update_session_context(session_id=session_id, context=context)["tags"]


# ============================================================
# Lane → provider bindings (chat-providers.md)
# ============================================================

def get_lane_providers(session_id="default"):
    """Return the lane→provider binding dict for a session."""
    return dict(get_session_context(session_id=session_id).get("lane_providers") or {})


def get_lane_provider(session_id="default", lane=""):
    """Return the binding string for a lane, or None if unbound."""
    if not lane:
        return None
    return get_lane_providers(session_id=session_id).get(lane.strip().lower())


def set_lane_provider(session_id="default", lane="", binding=""):
    """Bind a lane to ``provider:model`` (e.g. ``anthropic:claude-sonnet-4-5``).

    Raises ValueError on a malformed binding. Pass ``binding=""`` to
    unbind via the dedicated `clear_lane_provider` instead.
    """
    lane_clean = (lane or "").strip().lower()
    if not lane_clean:
        raise ValueError("lane is required")
    spec = (binding or "").strip()
    if not spec:
        raise ValueError("binding is required (use clear_lane_provider to unbind)")
    if ":" not in spec:
        # Bare model name → assume local ollama. Reject empty model.
        spec = "ollama:" + spec
    provider_part, _, model_part = spec.partition(":")
    if not provider_part.strip() or not model_part.strip():
        raise ValueError(f"binding must be 'provider:model' (got {binding!r})")

    context = get_session_context(session_id)
    bindings = dict(context.get("lane_providers") or {})
    bindings[lane_clean] = spec
    context["lane_providers"] = bindings
    update_session_context(session_id=session_id, context=context)
    return spec


def clear_lane_provider(session_id="default", lane=""):
    """Drop a lane's binding. Returns True if a binding was removed."""
    lane_clean = (lane or "").strip().lower()
    if not lane_clean:
        return False
    context = get_session_context(session_id)
    bindings = dict(context.get("lane_providers") or {})
    if lane_clean not in bindings:
        return False
    del bindings[lane_clean]
    context["lane_providers"] = bindings
    update_session_context(session_id=session_id, context=context)
    return True


def clear_all_lane_providers(session_id="default"):
    """Remove every binding on a session."""
    context = get_session_context(session_id)
    context["lane_providers"] = {}
    update_session_context(session_id=session_id, context=context)


# ============================================================
# Browser state (browser-automation.md)
# ============================================================

def get_browser_state(session_id="default"):
    """Return the browser-state dict (always shape-stable)."""
    return _normalize_browser_state(
        get_session_context(session_id=session_id).get("browser")
    )


def get_approved_origins(session_id="default"):
    return list(get_browser_state(session_id=session_id).get("approved_origins") or [])


def add_approved_origin(session_id="default", origin=""):
    """Append `origin` (e.g. ``"https://github.com"``) to the session's
    approved-origins whitelist. Returns the updated list."""
    cleaned = (origin or "").strip().lower()
    if not cleaned:
        raise ValueError("origin is required")
    context = get_session_context(session_id)
    state = _normalize_browser_state(context.get("browser"))
    if cleaned not in state["approved_origins"]:
        state["approved_origins"].append(cleaned)
    context["browser"] = state
    update_session_context(session_id=session_id, context=context)
    return list(state["approved_origins"])


def remove_approved_origin(session_id="default", origin=""):
    cleaned = (origin or "").strip().lower()
    context = get_session_context(session_id)
    state = _normalize_browser_state(context.get("browser"))
    if cleaned not in state["approved_origins"]:
        return False
    state["approved_origins"] = [o for o in state["approved_origins"] if o != cleaned]
    context["browser"] = state
    update_session_context(session_id=session_id, context=context)
    return True


def set_browser_active_session(session_id="default", browser_session_id=None):
    context = get_session_context(session_id)
    state = _normalize_browser_state(context.get("browser"))
    state["active_session_id"] = (
        str(browser_session_id).strip() if browser_session_id else None
    ) or None
    context["browser"] = state
    update_session_context(session_id=session_id, context=context)


def record_page_snapshot(session_id="default", snapshot=None):
    context = get_session_context(session_id)
    state = _normalize_browser_state(context.get("browser"))
    if snapshot is None:
        state["page_snapshot"] = None
    elif isinstance(snapshot, dict):
        state["page_snapshot"] = {
            "url": snapshot.get("url"),
            "title": snapshot.get("title"),
            "screenshot_path": snapshot.get("screenshot_path"),
            "text_excerpt": snapshot.get("text_excerpt"),
            "captured_at": snapshot.get("captured_at") or _now(),
        }
    context["browser"] = state
    update_session_context(session_id=session_id, context=context)
    return state["page_snapshot"]


def clear_browser_state(session_id="default"):
    context = get_session_context(session_id)
    context["browser"] = {
        "approved_origins": [],
        "active_session_id": None,
        "page_snapshot": None,
    }
    update_session_context(session_id=session_id, context=context)


def replace_history(session_id="default", history=None):
    captured = {}

    def fn(data):
        session = _ensure_session(data["sessions"].setdefault(session_id, _default_session()))
        session["history"] = list(history or [])
        data["sessions"][session_id] = session
        captured["history"] = session["history"]

    _mutate(fn)
    return captured["history"]


def append_message(role, content, model=None, session_id="default", metadata=None):
    msg = {"role": role, "content": content, "timestamp": _now()}
    if model:
        msg["model"] = model
    if metadata:
        msg["metadata"] = metadata

    def fn(data):
        session = _ensure_session(data["sessions"].setdefault(session_id, _default_session()))
        session["history"].append(msg)
        data["sessions"][session_id] = session

    _mutate(fn)
    return msg


def append_execution(execution, session_id="default"):
    normalized = _normalize_execution(execution)
    if not normalized:
        return None

    def fn(data):
        session = _ensure_session(data["sessions"].setdefault(session_id, _default_session()))
        session["executions"].append(normalized)
        session["executions"] = _normalize_executions(session["executions"])
        data["sessions"][session_id] = session

    _mutate(fn)
    return normalized


# ============================================================
# CoWork runs (top-level bucket) — see docs/design/cowork-mode.md
# ============================================================

def _ensure_cowork_bucket(data):
    """Make sure the top-level `cowork_runs` list exists."""
    runs = data.get("cowork_runs")
    if not isinstance(runs, list):
        runs = []
        data["cowork_runs"] = runs
    return runs


def _new_cowork_run_id():
    return f"cw_{uuid4().hex[:8]}"


def _normalize_cowork_subtask(entry):
    if not isinstance(entry, dict):
        return None
    sub_id = str(entry.get("id") or "").strip()
    task_text = str(entry.get("task") or "").strip()
    builder = str(entry.get("builder") or "").strip()
    if not sub_id or not task_text or not builder:
        return None
    depends_on = []
    for dep in entry.get("depends_on") or []:
        dep_clean = str(dep or "").strip()
        if dep_clean:
            depends_on.append(dep_clean)
    return {
        "id": sub_id,
        "task": task_text,
        "builder": builder,
        "depends_on": depends_on,
        "acceptance": str(entry.get("acceptance") or "").strip(),
        # Phase 5 lifecycle hooks (optional dispatcher commands run
        # before / after the main sub-task body). Empty string means
        # "no hook" — preserves backward-compat with existing runs.
        "before": str(entry.get("before") or "").strip(),
        "after": str(entry.get("after") or "").strip(),
    }


def _normalize_cowork_run(entry):
    if not isinstance(entry, dict):
        return None
    run_id = str(entry.get("id") or "").strip()
    session_id = str(entry.get("session_id") or "").strip()
    goal = str(entry.get("goal") or "").strip()
    if not run_id or not session_id or not goal:
        return None
    plan = []
    for sub in entry.get("plan") or []:
        normalized_sub = _normalize_cowork_subtask(sub)
        if normalized_sub is not None:
            plan.append(normalized_sub)
    raw_status = str(entry.get("status") or "planning").strip().lower()
    status = raw_status if raw_status in VALID_COWORK_STATUSES else "planning"
    task_ids = [
        str(t).strip()
        for t in (entry.get("task_ids") or [])
        if str(t or "").strip()
    ]
    deliverables = entry.get("deliverables") or []
    if not isinstance(deliverables, list):
        deliverables = []
    raw_strategy = str(entry.get("branch_strategy") or "head").strip().lower()
    if raw_strategy not in {"head", "merge-to-head", "branch"}:
        raw_strategy = "head"
    git_state = entry.get("git_state")
    if not isinstance(git_state, dict):
        git_state = {}
    return {
        "id": run_id,
        "session_id": session_id,
        "goal": goal,
        "plan": plan,
        "status": status,
        "created_at": entry.get("created_at") or _now(),
        "approved_at": entry.get("approved_at"),
        "completed_at": entry.get("completed_at"),
        "task_ids": task_ids,
        "subtask_results": entry.get("subtask_results") or {},
        "deliverables": deliverables,
        "synthesis": entry.get("synthesis") or "",
        # Phase 5: branch strategy + git state (target_branch,
        # original_branch, commits, merge_status). Default "head"
        # means "no git involvement" — backward compatible.
        "branch_strategy": raw_strategy,
        "branch_name": str(entry.get("branch_name") or "").strip(),
        "git_state": git_state,
    }


def add_cowork_run(session_id, goal, plan, *, branch_strategy="head", branch_name=""):
    """Persist a fresh cowork run with status `planning`. Returns the run."""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if not goal or not str(goal).strip():
        raise ValueError("goal is required")
    if not isinstance(plan, list) or not plan:
        raise ValueError("plan must be a non-empty list of sub-tasks")
    if len(plan) > MAX_SUBTASKS:
        raise ValueError(f"plan has {len(plan)} sub-tasks (max {MAX_SUBTASKS})")
    cleaned_strategy = str(branch_strategy or "head").strip().lower()
    if cleaned_strategy not in {"head", "merge-to-head", "branch"}:
        raise ValueError(f"unknown branch_strategy {cleaned_strategy!r}")

    run = {
        "id": _new_cowork_run_id(),
        "session_id": sid,
        "goal": str(goal).strip(),
        "plan": plan,
        "status": "planning",
        "created_at": _now(),
        "approved_at": None,
        "completed_at": None,
        "task_ids": [],
        "subtask_results": {},
        "deliverables": [],
        "synthesis": "",
        "branch_strategy": cleaned_strategy,
        "branch_name": str(branch_name or "").strip(),
        "git_state": {},
    }

    captured = {"run": None}

    def fn(data):
        runs = _ensure_cowork_bucket(data)
        normalized = _normalize_cowork_run(run)
        runs.append(normalized)
        captured["run"] = normalized

    _mutate(fn)
    return captured["run"]


def get_cowork_run(run_id):
    if not run_id:
        return None
    runs = _ensure_cowork_bucket(load_memory())
    for entry in runs:
        if entry.get("id") == run_id:
            return _normalize_cowork_run(entry)
    return None


def list_cowork_runs(session_id=None):
    """List runs for a session, or all runs if `session_id` is None."""
    runs = _ensure_cowork_bucket(load_memory())
    cleaned = [_normalize_cowork_run(r) for r in runs]
    cleaned = [r for r in cleaned if r is not None]
    if session_id is None:
        return cleaned
    sid = str(session_id).strip()
    return [r for r in cleaned if r["session_id"] == sid]


def active_cowork_run(session_id):
    """Return the current run for a session whose status is in
    COWORK_ACTIVE_STATUSES, or None."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    for run in list_cowork_runs(session_id=sid):
        if run["status"] in COWORK_ACTIVE_STATUSES:
            return run
    return None


_COWORK_ALLOWED_FIELDS = frozenset({
    "status", "approved_at", "completed_at",
    "task_ids", "subtask_results", "deliverables", "synthesis",
    # Phase 5: callers (cowork.execute_run) update these as the
    # branch-strategy state machine progresses.
    "git_state",
})


def update_cowork_run(run_id, **fields):
    """Partial update of a stored cowork run. Returns the updated run.

    Only keys in `_COWORK_ALLOWED_FIELDS` may be modified; unknown
    fields raise. Status transitions are validated against
    VALID_COWORK_STATUSES.
    """
    if not run_id:
        raise ValueError("run_id is required")
    for key in fields:
        if key not in _COWORK_ALLOWED_FIELDS:
            raise ValueError(
                f"unknown cowork field {key!r} (allowed: {sorted(_COWORK_ALLOWED_FIELDS)})"
            )
    if "status" in fields and fields["status"] not in VALID_COWORK_STATUSES:
        raise ValueError(
            f"invalid cowork status {fields['status']!r} "
            f"(expected: {', '.join(VALID_COWORK_STATUSES)})"
        )

    captured = {"run": None}

    def fn(data):
        runs = _ensure_cowork_bucket(data)
        for index, entry in enumerate(runs):
            if entry.get("id") != run_id:
                continue
            entry.update(fields)
            normalized = _normalize_cowork_run(entry)
            runs[index] = normalized
            captured["run"] = normalized
            return

    _mutate(fn)
    return captured["run"]


def append_cowork_task_id(run_id, task_id):
    """Append a builder-task id to a run's task_ids list."""
    run = get_cowork_run(run_id)
    if not run:
        return None
    task_ids = list(run.get("task_ids") or [])
    if task_id not in task_ids:
        task_ids.append(task_id)
    return update_cowork_run(run_id, task_ids=task_ids)


def record_cowork_subtask_result(run_id, sub_id, result):
    """Stash the per-sub-task result dict on the run."""
    run = get_cowork_run(run_id)
    if not run:
        return None
    results = dict(run.get("subtask_results") or {})
    results[sub_id] = result
    return update_cowork_run(run_id, subtask_results=results)
