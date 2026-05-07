"""Workflow templates — saved + parameterised dispatcher-command sequences.

Phase 1 surface:
  - Templates load from ``templates/*.toml`` and ``templates/*.json``
    in the project root. TOML is preferred for hand-authoring (nicer
    multi-line strings); JSON is supported too.
  - Each template has an ``id``, optional ``description``, optional
    ``params`` list, and a ``steps`` list. Phase 1 supports the
    ``command`` step kind only — the step's ``cmd`` is dispatched
    through ``dispatch_input`` after parameter substitution. To run a
    cowork plan from a template, set ``cmd = 'cowork plan "..."'``.
  - Substitution: ``{{name}}`` for declared params, ``{user.name}`` /
    ``{user.email}`` / ``{date}`` / ``{session_id}`` for built-ins.

Phase 2 (planned) adds: dedicated ``cowork_plan`` step kind, per-session
inline templates, template-invokes-template chaining, ``!`<cmd>``
expression substitution. See ``docs/design/workflow-templates.md``.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import subprocess
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Template data model
# --------------------------------------------------------------------------

VALID_STEP_KINDS = frozenset({"command"})  # Phase 2 adds: cowork_plan, template, wait

#: Hard cap on number of params a template may declare.
MAX_PARAMS = 16

#: Hard cap on number of steps in a template.
MAX_STEPS = 32


@dataclass
class TemplateParam:
    name: str
    description: str = ""
    required: bool = False
    default: Optional[str] = None


@dataclass
class TemplateStep:
    kind: str
    cmd: str = ""
    require_success: bool = False


@dataclass
class Template:
    id: str
    description: str
    params: List[TemplateParam] = field(default_factory=list)
    steps: List[TemplateStep] = field(default_factory=list)
    source_path: Optional[str] = None


class TemplateError(ValueError):
    """Raised on parse / param / substitution errors. Carries a canonical
    ``reason`` string from ``error_vocabulary.py`` so the dispatcher can
    surface it without re-classifying."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_template(data: Dict[str, Any], source_path: Optional[str] = None) -> Template:
    """Validate a parsed TOML/JSON dict and produce a Template object.

    Raises TemplateError with a canonical reason on every failure path.
    """
    if not isinstance(data, dict):
        raise TemplateError("invalid_argument", "template root must be a mapping")

    raw_id = str(data.get("id") or "").strip()
    if not raw_id or not re.match(r"^[a-z][a-z0-9_-]{0,40}$", raw_id):
        raise TemplateError(
            "invalid_argument",
            f"template id must be lowercase a-z/0-9/_/-, max 40 chars (got {raw_id!r})",
        )

    description = str(data.get("description") or "").strip()

    raw_params = data.get("params") or []
    if not isinstance(raw_params, list):
        raise TemplateError("invalid_argument", "params must be a list")
    if len(raw_params) > MAX_PARAMS:
        raise TemplateError(
            "invalid_argument",
            f"template has {len(raw_params)} params (max {MAX_PARAMS})",
        )

    params: List[TemplateParam] = []
    seen_names: set[str] = set()
    for entry in raw_params:
        if not isinstance(entry, dict):
            raise TemplateError("invalid_argument", "each param must be a mapping")
        name = str(entry.get("name") or "").strip()
        if not name or not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
            raise TemplateError(
                "invalid_argument",
                f"param name {name!r} must be a valid identifier",
            )
        if name in seen_names:
            raise TemplateError(
                "invalid_argument",
                f"duplicate param name {name!r}",
            )
        seen_names.add(name)
        params.append(
            TemplateParam(
                name=name,
                description=str(entry.get("description") or "").strip(),
                required=bool(entry.get("required") or False),
                default=str(entry["default"]) if "default" in entry else None,
            )
        )

    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TemplateError("invalid_argument", "template needs at least one step")
    if len(raw_steps) > MAX_STEPS:
        raise TemplateError(
            "invalid_argument",
            f"template has {len(raw_steps)} steps (max {MAX_STEPS})",
        )

    steps: List[TemplateStep] = []
    for index, entry in enumerate(raw_steps):
        if not isinstance(entry, dict):
            raise TemplateError("invalid_argument", f"step {index} must be a mapping")
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in VALID_STEP_KINDS:
            raise TemplateError(
                "template_unknown_step",
                f"step {index} kind {kind!r} not supported "
                f"(Phase 1 supports: {sorted(VALID_STEP_KINDS)})",
            )
        cmd = str(entry.get("cmd") or "").strip()
        if not cmd:
            raise TemplateError(
                "invalid_argument",
                f"step {index} kind={kind!r} needs a non-empty cmd",
            )
        steps.append(
            TemplateStep(
                kind=kind,
                cmd=cmd,
                require_success=bool(entry.get("require_success") or False),
            )
        )

    return Template(
        id=raw_id,
        description=description,
        params=params,
        steps=steps,
        source_path=source_path,
    )


def load_template_file(path: Path) -> Template:
    """Read a single template file from disk and parse it."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".toml":
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        elif suffix == ".json":
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            raise TemplateError(
                "invalid_argument",
                f"unsupported template extension {suffix!r} (use .toml or .json)",
            )
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise TemplateError(
            "invalid_argument",
            f"failed to parse {path.name}: {exc}",
        ) from exc

    return parse_template(data, source_path=str(path))


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_registry: Dict[str, Template] = {}
_registry_lock = threading.Lock()
_registry_loaded = False


def _default_templates_dir() -> Path:
    """Resolve the project-root ``templates/`` directory.

    Walks up from this file's location until we find a parent containing
    a ``templates/`` directory or a ``project_plan.txt`` (project root
    marker). Falls back to CWD/templates if neither is found.
    """
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if (parent / "project_plan.txt").exists():
            return parent / "templates"
    return Path.cwd() / "templates"


def reload_templates(*, templates_dir: Optional[Path] = None) -> Dict[str, Template]:
    """Re-scan the templates directory and rebuild the registry.

    Returns the new registry as a dict. Templates that fail to parse
    are logged and skipped; one bad template doesn't take down the rest.
    """
    global _registry_loaded
    target = Path(templates_dir) if templates_dir is not None else _default_templates_dir()
    new_registry: Dict[str, Template] = {}
    if target.exists() and target.is_dir():
        for entry in sorted(target.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in {".toml", ".json"}:
                continue
            try:
                template = load_template_file(entry)
            except TemplateError as exc:
                logger.warning("Skipping template %s: %s", entry.name, exc)
                continue
            if template.id in new_registry:
                logger.warning(
                    "Duplicate template id %r in %s — keeping first",
                    template.id, entry.name,
                )
                continue
            new_registry[template.id] = template
    with _registry_lock:
        _registry.clear()
        _registry.update(new_registry)
        _registry_loaded = True
    return dict(new_registry)


def list_templates() -> List[Template]:
    with _registry_lock:
        if not _registry_loaded:
            # Lazy-load on first access so tests / scripts don't have
            # to call reload explicitly.
            need_reload = True
        else:
            need_reload = False
    if need_reload:
        reload_templates()
    with _registry_lock:
        return sorted(_registry.values(), key=lambda t: t.id)


def get_template(template_id: str) -> Optional[Template]:
    with _registry_lock:
        if not _registry_loaded:
            need_reload = True
        else:
            need_reload = False
    if need_reload:
        reload_templates()
    with _registry_lock:
        return _registry.get(template_id)


def _reset_registry_for_tests():
    """Clear the in-memory template registry. Test-only.

    Sets ``_registry_loaded = True`` so the lazy-loader in
    ``list_templates`` / ``get_template`` doesn't re-read the project's
    ``templates/`` directory mid-test. Tests that want to load specific
    templates should call ``reload_templates(templates_dir=...)`` with a
    tempdir, or ``register_template_for_tests`` with an in-memory
    Template object.
    """
    global _registry_loaded
    with _registry_lock:
        _registry.clear()
        _registry_loaded = True


def register_template_for_tests(template: Template):
    """Inject a Template into the registry without disk I/O. Test-only."""
    global _registry_loaded
    with _registry_lock:
        _registry[template.id] = template
        _registry_loaded = True


# --------------------------------------------------------------------------
# Parameter substitution
# --------------------------------------------------------------------------

# {{name}} — explicit param reference
_PARAM_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# {user.name} / {date} / {session_id} — single-brace built-in expansion
_BUILTIN_RE = re.compile(r"\{(user\.(?:name|email)|date|session_id)\}")


def _git_user(field: str) -> str:
    """Read git user.name or user.email; tolerant of missing config."""
    if field not in {"name", "email"}:
        return ""
    try:
        result = subprocess.run(
            ["git", "config", "--get", f"user.{field}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return ""


def _resolve_builtin(token: str, *, session_id: str) -> str:
    if token == "date":
        return _dt.date.today().isoformat()
    if token == "session_id":
        return session_id
    if token.startswith("user."):
        return _git_user(token.split(".", 1)[1])
    return ""


def substitute(text: str, params: Dict[str, str], *, session_id: str = "default") -> str:
    """Apply ``{{name}}`` and ``{user.*}`` / ``{date}`` substitutions.

    Unresolved ``{{name}}`` references raise TemplateError with reason
    ``template_param_missing``. Built-in expansions that fail (e.g.
    ``{user.email}`` with no git config) substitute as empty strings —
    the template author can guard with ``--`` flags or the like.
    """
    if not isinstance(text, str) or not text:
        return text

    missing: List[str] = []

    def _param_replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            missing.append(name)
            return match.group(0)  # leave as-is so failure message is clear
        return str(params[name])

    text = _PARAM_RE.sub(_param_replace, text)
    if missing:
        raise TemplateError(
            "template_param_missing",
            f"missing template parameter(s): {', '.join(sorted(set(missing)))}",
        )

    def _builtin_replace(match: re.Match) -> str:
        return _resolve_builtin(match.group(1), session_id=session_id)

    return _BUILTIN_RE.sub(_builtin_replace, text)


def resolve_params(template: Template, supplied: Dict[str, str]) -> Dict[str, str]:
    """Merge user-supplied params with template defaults.

    Returns the merged dict ready to feed `substitute`. Raises
    TemplateError if a required param is missing.
    """
    merged: Dict[str, str] = {}
    missing_required: List[str] = []
    for param in template.params:
        if param.name in supplied:
            merged[param.name] = str(supplied[param.name])
        elif param.default is not None:
            merged[param.name] = str(param.default)
        elif param.required:
            missing_required.append(param.name)
        # else: unset optional param, no default — caller may still
        # reference it; substitution will raise template_param_missing.
    if missing_required:
        raise TemplateError(
            "template_param_missing",
            f"required parameter(s) missing: {', '.join(missing_required)}",
        )
    # Pass through unknown-but-supplied params too (let the user
    # over-supply without erroring; they may be useful in {{...}}
    # references that weren't declared).
    for name, value in supplied.items():
        if name not in merged:
            merged[name] = str(value)
    return merged


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run_template(
    template: Template,
    params: Dict[str, str],
    session_id: str,
    *,
    dispatch: Optional[Callable[[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Execute every step in order. Returns a summary dict.

    ``dispatch(text, session_id) -> event`` is the per-step dispatcher
    callable; defaults to ``command_dispatcher.dispatch_input`` (lazy
    imported to avoid circulars). Tests inject a fake dispatch so they
    don't touch real handlers.

    Behaviour:
      - Param substitution applies to every step's ``cmd`` first.
      - Each step is dispatched and its event captured.
      - If a step's event has ``reason == "approval_required"`` the
        run **pauses** there — the rest of the steps are skipped, and
        the summary's ``stopped_reason`` is ``"paused"``.
      - If a step has ``require_success: true`` and the event carries
        any ``reason``, the run aborts with ``stopped_reason: "step_failed"``.
      - Otherwise the run continues to the next step.

    Returns ``{template_id, params, steps: [...], stopped_reason,
    completed: bool}``. Each entry in ``steps`` is the captured event.
    """
    if dispatch is None:
        from app.services.command_dispatcher import dispatch_input as _real
        dispatch = _real

    resolved = resolve_params(template, params)
    step_events: List[Dict[str, Any]] = []
    stopped_reason = "completed"

    for index, step in enumerate(template.steps):
        cmd = substitute(step.cmd, resolved, session_id=session_id)
        event = dispatch(cmd, session_id)
        step_events.append(
            {
                "index": index,
                "kind": step.kind,
                "cmd": cmd,
                "event": event,
            }
        )

        # Approval pause is a SOFT stop — we don't error, we just halt
        # so the user can approve; they re-run the template (or the
        # dispatcher event for `approve` continues the cowork run on
        # its own, depending on the step type).
        event_reason = (event or {}).get("reason")
        if event_reason == "approval_required":
            stopped_reason = "paused"
            break

        if step.require_success and event_reason is not None:
            stopped_reason = "step_failed"
            break

    return {
        "template_id": template.id,
        "params": resolved,
        "steps": step_events,
        "stopped_reason": stopped_reason,
        "completed": stopped_reason == "completed",
    }
