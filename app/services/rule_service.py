"""Agent rules — declarative dispatcher constraints.

Phase 1 surface (see ``docs/design/agent-rules.md``):

  - Rules load from ``rules/*.toml`` and ``rules/*.json`` at the
    project root. TOML is preferred for hand-authoring.
  - Each rule has an ``id``, ``description``, a ``when`` predicate,
    and a ``then`` action (one of ``refuse`` or ``forbid_provider``).
  - ``evaluate_pre_dispatch(text, session_id)`` is called from the
    dispatcher BEFORE any handler routes. If a rule matches the
    candidate event with a ``refuse`` action, the dispatch short-
    circuits with the rule's reason + output.
  - ``forbid_provider`` is parser-validated but a no-op until the
    chat-providers feature ships (it then becomes "strip provider
    binding from the lane and fall back to the allowed list").

Phase 2 (planned) adds: post-dispatch evaluation (``audit``,
``notify``, ``dispatch`` actions), bus subscription so rules fire on
any KIND_TASK / KIND_WORKSPACE_WRITE event, per-session inline rules,
and the full predicate language (``result.<json.path>`` comparators).

Tests inject fake events / rules via ``register_rule_for_tests`` and
``_reset_registry_for_tests``; see ``tests/test_rule_service.py``.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


# ============================================================
# Constants + canonical actions
# ============================================================

#: The action keys the parser recognises in `then:`. Phase 1 implements
#: `refuse` (short-circuit dispatch) and `forbid_provider` (parser-
#: validated no-op until chat-providers ships).
VALID_ACTION_KEYS = frozenset({"refuse", "forbid_provider"})

#: Predicate keys allowed in `when:`. Anything else fails parse.
VALID_PREDICATE_KEYS = frozenset({
    "event_kind",
    "action",
    "session_id",
    "session_tag",
    "text_match",     # substring match against the dispatch input text
    "text_regex",     # regex match against the dispatch input text
    "path_glob",      # post-dispatch only (workspace_write events) — Phase 1 ignores
})

#: Hard cap on rules registered at once (parsing aborts if exceeded).
MAX_RULES = 256


# ============================================================
# Rule data model
# ============================================================

@dataclass
class Rule:
    """A parsed, validated rule. ``when_keys`` and ``then`` carry the
    raw dicts; the predicate evaluator is in ``rule_matches`` below."""
    id: str
    description: str
    when: Dict[str, Any]
    then: Dict[str, Any]
    source_path: Optional[str] = None


class RuleParseError(ValueError):
    """Raised on parse / validation errors. Carries a canonical
    ``reason`` for the dispatcher to surface."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# ============================================================
# Parsing
# ============================================================

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def parse_rule(data: Dict[str, Any], source_path: Optional[str] = None) -> Rule:
    """Validate a rule dict (loaded from TOML/JSON) and produce a Rule.

    Raises RuleParseError with a canonical reason on failure.
    """
    if not isinstance(data, dict):
        raise RuleParseError("invalid_argument", "rule must be a mapping")

    rule_id = str(data.get("id") or "").strip()
    if not _ID_RE.match(rule_id):
        raise RuleParseError(
            "invalid_argument",
            f"rule id must be lowercase a-z/0-9/_/-, max 40 chars (got {rule_id!r})",
        )
    description = str(data.get("description") or "").strip()

    when = data.get("when") or {}
    if not isinstance(when, dict):
        raise RuleParseError("invalid_argument", "`when` must be a mapping")
    if not when:
        raise RuleParseError(
            "invalid_argument",
            "`when` cannot be empty — a rule with no predicate would always fire",
        )
    for key in when:
        if key not in VALID_PREDICATE_KEYS:
            raise RuleParseError(
                "invalid_argument",
                f"unknown when-key {key!r} (allowed: {sorted(VALID_PREDICATE_KEYS)})",
            )
    if "text_regex" in when:
        try:
            re.compile(str(when["text_regex"]))
        except re.error as exc:
            raise RuleParseError(
                "invalid_argument", f"invalid text_regex: {exc}"
            ) from exc

    then = data.get("then") or {}
    if not isinstance(then, dict):
        raise RuleParseError("invalid_argument", "`then` must be a mapping")
    if not any(k in then for k in VALID_ACTION_KEYS):
        raise RuleParseError(
            "invalid_argument",
            f"`then` must specify one of: {sorted(VALID_ACTION_KEYS)}",
        )
    for key in then:
        if key not in VALID_ACTION_KEYS:
            raise RuleParseError(
                "invalid_argument",
                f"unknown then-key {key!r} (allowed: {sorted(VALID_ACTION_KEYS)})",
            )

    if "refuse" in then:
        refuse = then["refuse"]
        if not isinstance(refuse, dict):
            raise RuleParseError(
                "invalid_argument", "`then.refuse` must be a mapping"
            )
        reason_tag = str(refuse.get("reason") or "").strip()
        if not reason_tag:
            raise RuleParseError(
                "invalid_argument", "`then.refuse.reason` is required"
            )
        # Loose validation — the dispatcher's lint will catch unknown
        # tags at startup if we register them, so just shape-check here.

    if "forbid_provider" in then:
        forbid = then["forbid_provider"]
        if not isinstance(forbid, dict):
            raise RuleParseError(
                "invalid_argument", "`then.forbid_provider` must be a mapping"
            )
        not_in = forbid.get("not_in") or forbid.get("allow_only") or []
        if not isinstance(not_in, list) or not all(isinstance(p, str) for p in not_in):
            raise RuleParseError(
                "invalid_argument",
                "`then.forbid_provider.not_in` must be a list of provider id strings",
            )

    return Rule(
        id=rule_id,
        description=description,
        when=dict(when),
        then=dict(then),
        source_path=source_path,
    )


def load_rule_file(path: Path) -> Rule:
    suffix = path.suffix.lower()
    try:
        if suffix == ".toml":
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        elif suffix == ".json":
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            raise RuleParseError(
                "invalid_argument",
                f"unsupported rule extension {suffix!r} (use .toml or .json)",
            )
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise RuleParseError(
            "invalid_argument", f"failed to parse {path.name}: {exc}"
        ) from exc

    return parse_rule(data, source_path=str(path))


# ============================================================
# Registry
# ============================================================

_registry: Dict[str, Rule] = {}
_disabled: set[str] = set()
_registry_lock = threading.Lock()
_registry_loaded = False


def _default_rules_dir() -> Path:
    """Resolve the project-root ``rules/`` directory."""
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if (parent / "project_plan.txt").exists():
            return parent / "rules"
    return Path.cwd() / "rules"


def reload_rules(*, rules_dir: Optional[Path] = None) -> Dict[str, Rule]:
    """Re-scan the rules directory and rebuild the registry.

    Bad rules are logged and skipped. Returns the new registry.
    Disabled-rule IDs survive a reload (so disabling a rule sticks
    even if its source file is edited and the file is re-parsed).
    """
    global _registry_loaded
    target = Path(rules_dir) if rules_dir is not None else _default_rules_dir()
    new_registry: Dict[str, Rule] = {}
    if target.exists() and target.is_dir():
        for entry in sorted(target.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in {".toml", ".json"}:
                continue
            try:
                rule = load_rule_file(entry)
            except RuleParseError as exc:
                logger.warning("Skipping rule %s: %s", entry.name, exc)
                continue
            if rule.id in new_registry:
                logger.warning(
                    "Duplicate rule id %r in %s — keeping first",
                    rule.id, entry.name,
                )
                continue
            if len(new_registry) >= MAX_RULES:
                logger.warning(
                    "Rule cap (%d) reached — skipping %s",
                    MAX_RULES, entry.name,
                )
                break
            new_registry[rule.id] = rule
    with _registry_lock:
        _registry.clear()
        _registry.update(new_registry)
        _registry_loaded = True
    return dict(new_registry)


def list_rules() -> List[Rule]:
    with _registry_lock:
        need_reload = not _registry_loaded
    if need_reload:
        reload_rules()
    with _registry_lock:
        return sorted(_registry.values(), key=lambda r: r.id)


def get_rule(rule_id: str) -> Optional[Rule]:
    with _registry_lock:
        need_reload = not _registry_loaded
    if need_reload:
        reload_rules()
    with _registry_lock:
        return _registry.get(rule_id)


def disable_rule(rule_id: str) -> bool:
    """Mark a rule as disabled (won't fire). Returns True if it was
    enabled before. Disabled state survives reload but not process
    restart."""
    with _registry_lock:
        if rule_id not in _registry:
            return False
        if rule_id in _disabled:
            return False
        _disabled.add(rule_id)
        return True


def enable_rule(rule_id: str) -> bool:
    """Re-enable a previously disabled rule. Returns True if changed."""
    with _registry_lock:
        if rule_id not in _disabled:
            return False
        _disabled.discard(rule_id)
        return True


def is_disabled(rule_id: str) -> bool:
    with _registry_lock:
        return rule_id in _disabled


def list_disabled() -> List[str]:
    with _registry_lock:
        return sorted(_disabled)


def _reset_registry_for_tests() -> None:
    """Test-only: clear the in-memory registry + disabled set."""
    global _registry_loaded
    with _registry_lock:
        _registry.clear()
        _disabled.clear()
        _registry_loaded = True  # so list_rules() doesn't lazy-reload


def register_rule_for_tests(rule: Rule) -> None:
    """Test-only: inject a Rule without touching disk."""
    global _registry_loaded
    with _registry_lock:
        _registry[rule.id] = rule
        _registry_loaded = True


# ============================================================
# Predicate evaluation
# ============================================================

def _matches_predicate(rule: Rule, event: Dict[str, Any], session_tags: List[str]) -> bool:
    """True if every key in ``rule.when`` matches the candidate event."""
    for key, expected in rule.when.items():
        if key == "event_kind":
            if event.get("event_kind") != expected:
                return False
        elif key == "action":
            if event.get("action") != expected:
                return False
        elif key == "session_id":
            if event.get("session_id") != expected:
                return False
        elif key == "session_tag":
            if str(expected) not in session_tags:
                return False
        elif key == "text_match":
            text = event.get("text") or ""
            if str(expected) not in text:
                return False
        elif key == "text_regex":
            text = event.get("text") or ""
            try:
                if not re.search(str(expected), text):
                    return False
            except re.error:
                return False
        elif key == "path_glob":
            # Phase 1: only meaningful for workspace_write events. For
            # pre-dispatch evaluation, we extract the path from `write
            # <path>` text (best-effort; fragile but useful).
            path = event.get("path") or ""
            if not path and event.get("event_kind") == "dispatch":
                # Try to pull path from `write <path> ...` text.
                text = (event.get("text") or "").strip()
                if text.lower().startswith(("write ", "/write ", "delete ", "/delete ")):
                    parts = text.split(maxsplit=2)
                    if len(parts) >= 2:
                        path = parts[1].strip().strip('"').strip("'")
            if not fnmatch.fnmatch(path, str(expected)):
                return False
        # Unknown keys would have been rejected at parse time.
    return True


def _build_candidate_event(text: str, session_id: str) -> Dict[str, Any]:
    """Construct the event-shaped dict used for pre-dispatch matching.

    The "action" we infer is the first whitespace-separated token of
    the input (after stripping any leading ``/``) — same as the
    dispatcher's own routing key. "chat" is the catch-all for inputs
    that don't start with a recognised command verb, but we report the
    raw verb here so rules can refuse e.g. ``write`` specifically.
    """
    head = (text or "").strip().split(maxsplit=1)[:1]
    raw = head[0].lstrip("/").lower() if head else ""
    return {
        "event_kind": "dispatch",
        "action": raw,
        "session_id": session_id,
        "text": text,
    }


# ============================================================
# Pre-dispatch evaluation
# ============================================================

def _publish_rule_event(rule_id: str, action: str, session_id: str, target_event: Dict[str, Any]) -> None:
    """Best-effort publish of a `KIND_RULE` event."""
    try:
        from app.services.gateway.bus import GatewayEvent, KIND_RULE, default_bus

        default_bus().publish(
            GatewayEvent(
                kind=KIND_RULE,
                session_id=session_id,
                payload={
                    "rule_id": rule_id,
                    "action": action,
                    # Send a minimal target_event dict so subscribers
                    # can correlate without seeing the entire input
                    # text (some inputs may be long / sensitive).
                    "target": {
                        "event_kind": target_event.get("event_kind"),
                        "action": target_event.get("action"),
                        "session_id": target_event.get("session_id"),
                    },
                },
            )
        )
    except Exception:  # noqa: BLE001
        pass


def evaluate_pre_dispatch(
    text: str,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """Evaluate every active rule against the dispatch input.

    Returns:
      - None if no rule matched (or only matched rules with no
        short-circuiting action).
      - A dispatcher event dict if a `refuse` rule fired — the dict
        has the rule-supplied reason and output, ready to be returned
        directly from the dispatcher.

    `forbid_provider` matches publish a `KIND_RULE` event but don't
    short-circuit (Phase 1 stub; no provider routing exists yet).
    """
    # Lazy-load on first call so the dispatcher doesn't have to
    # explicitly bootstrap rules at startup.
    with _registry_lock:
        need_reload = not _registry_loaded
    if need_reload:
        reload_rules()

    # Read session tags up front so we don't hit storage per rule.
    try:
        from app.memory import local_memory
        tags = local_memory.get_tags(session_id=session_id) or []
    except Exception:  # noqa: BLE001
        tags = []

    event = _build_candidate_event(text, session_id)

    refusal: Optional[Dict[str, Any]] = None

    with _registry_lock:
        # Iterate over a snapshot to avoid lock contention while we
        # publish events / read storage.
        active_rules = [
            r for r in _registry.values() if r.id not in _disabled
        ]

    for rule in sorted(active_rules, key=lambda r: r.id):
        if not _matches_predicate(rule, event, tags):
            continue

        if "refuse" in rule.then:
            refuse_spec = rule.then["refuse"]
            reason_tag = str(refuse_spec.get("reason") or "invalid_argument")
            output = str(refuse_spec.get("output") or f"Refused by rule {rule.id!r}.")
            _publish_rule_event(rule.id, "refuse", session_id, event)
            if refusal is None:
                # First refuse wins; subsequent rules can still notify
                # via the bus but won't change the response shape.
                refusal = {
                    "action": event.get("action") or "rule",
                    "session_id": session_id,
                    "output": (
                        f"[refused by rule {rule.id!r}] {output}"
                    ),
                    "reason": reason_tag,
                    "rule_id": rule.id,
                }
            continue

        if "forbid_provider" in rule.then:
            # Phase 1: emit the bus event so subscribers see the match.
            # The dispatcher does NOT change its routing — this becomes
            # active when chat-providers ships.
            _publish_rule_event(rule.id, "forbid_provider", session_id, event)
            continue

    return refusal


def evaluate_post_dispatch(event: Dict[str, Any], session_id: str) -> None:
    """Stub for Phase 2 post-dispatch actions. Not yet wired."""
    return None
