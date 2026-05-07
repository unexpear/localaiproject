"""Theme registry + persistence for the Qt UI.

Five theme kinds are supported:

  - "default":     no extra stylesheet; Qt's native style.
  - "qt_material": apply_stylesheet(app, theme=<xml-name>).
  - "qdarkstyle":  load_stylesheet(qt_api='pyside6', palette=Dark|Light).
  - "qss":         a raw QSS string set on the QApplication.
  - "qss_file":    a path to a .qss file under app/ui/themes_qss/ (relative,
                   sandboxed to that directory).

Themes are persisted to `themes.json` next to this module:

    {
      "active": "Midnight",
      "presets": {
        "Default": {"kind": "default", "options": {}},
        "Midnight": {"kind": "qt_material", "options": {"theme": "dark_teal.xml"}},
        "MyDark": {"kind": "qdarkstyle", "options": {"palette": "dark"}},
        "MyAccent": {"kind": "qss", "options": {"qss": "QWidget { background: #111; color: #ddd; }"}}
      }
    }

The Qt-importing pieces (apply_to_app, theme listings) are kept lazy so this
module can be imported in headless test environments without pulling Qt.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
from typing import Any, Dict, Iterable, Optional

THEMES_FILE = os.path.join(os.path.dirname(__file__), "themes.json")
QSS_DIR = os.path.join(os.path.dirname(__file__), "themes_qss")

VALID_KINDS = ("default", "qt_material", "qdarkstyle", "qss", "qss_file")

DEFAULT_PRESETS: Dict[str, Dict[str, Any]] = {
    "Default": {"kind": "default", "options": {}},
    "Foundry": {"kind": "qss_file", "options": {"path": "foundry.qss"}},
    "Material Dark Teal": {"kind": "qt_material", "options": {"theme": "dark_teal.xml"}},
    "Material Light Blue": {"kind": "qt_material", "options": {"theme": "light_blue.xml"}},
    "QDarkStyle Dark": {"kind": "qdarkstyle", "options": {"palette": "dark"}},
    "QDarkStyle Light": {"kind": "qdarkstyle", "options": {"palette": "light"}},
}

DEFAULT_ACTIVE = "Default"

DEFAULT_TYPOGRAPHY: Dict[str, Any] = {
    # Empty family means "leave Qt's native default font alone".
    "ui_family": "",
    "base_size_pt": 0,        # 0 means "do not override"
}

UI_FAMILY_SUGGESTIONS = (
    "",  # native (no override)
    "Inter",
    "Segoe UI",
    "Helvetica Neue",
    "Söhne",
    "IBM Plex Sans",
    "Source Sans 3",
    "system-ui",
)

BASE_SIZE_RANGE = (8, 18)  # min/max pt clamp

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _empty_state() -> Dict[str, Any]:
    return {
        "active": DEFAULT_ACTIVE,
        "presets": copy.deepcopy(DEFAULT_PRESETS),
        "typography": copy.deepcopy(DEFAULT_TYPOGRAPHY),
    }


def _normalize_typography(payload: Any) -> Dict[str, Any]:
    out = dict(DEFAULT_TYPOGRAPHY)
    if isinstance(payload, dict):
        family = payload.get("ui_family")
        if isinstance(family, str):
            out["ui_family"] = family.strip()
        size = payload.get("base_size_pt")
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            size_int = 0
        if size_int <= 0:
            out["base_size_pt"] = 0
        else:
            lo, hi = BASE_SIZE_RANGE
            out["base_size_pt"] = max(lo, min(hi, size_int))
    return out


def load_state(path: str = THEMES_FILE) -> Dict[str, Any]:
    """Load the themes file, merging defaults for any missing built-in presets."""
    if not os.path.exists(path):
        return _empty_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _empty_state()

    state = _empty_state()
    presets = state["presets"]
    user_presets = data.get("presets") or {}
    if isinstance(user_presets, dict):
        for name, preset in user_presets.items():
            if not isinstance(name, str) or not name:
                continue
            normalized = _normalize_preset(preset)
            if normalized is not None:
                presets[name] = normalized

    active = data.get("active")
    if isinstance(active, str) and active in presets:
        state["active"] = active

    state["typography"] = _normalize_typography(data.get("typography"))
    return state


def save_state(state: Dict[str, Any], path: str = THEMES_FILE) -> None:
    """Atomic write of the themes file."""
    sanitized = _sanitize_state(state)
    tmp = f"{path}.tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sanitized, fh, indent=2)
        os.replace(tmp, path)


def _sanitize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    presets = {}
    for name, preset in (state.get("presets") or {}).items():
        if not isinstance(name, str) or not name:
            continue
        normalized = _normalize_preset(preset)
        if normalized is not None:
            presets[name] = normalized
    if not presets:
        presets = copy.deepcopy(DEFAULT_PRESETS)
    active = state.get("active")
    if not isinstance(active, str) or active not in presets:
        active = DEFAULT_ACTIVE if DEFAULT_ACTIVE in presets else next(iter(presets))
    return {
        "active": active,
        "presets": presets,
        "typography": _normalize_typography(state.get("typography")),
    }


def _normalize_preset(preset: Any) -> Dict[str, Any] | None:
    if not isinstance(preset, dict):
        return None
    kind = preset.get("kind")
    if kind not in VALID_KINDS:
        return None
    options = preset.get("options") or {}
    if not isinstance(options, dict):
        options = {}
    cleaned = {"kind": kind, "options": {}}

    if kind == "qt_material":
        theme = options.get("theme")
        if not isinstance(theme, str) or not theme:
            return None
        cleaned["options"]["theme"] = theme
    elif kind == "qdarkstyle":
        palette = options.get("palette") or "dark"
        if palette not in ("dark", "light"):
            palette = "dark"
        cleaned["options"]["palette"] = palette
    elif kind == "qss":
        qss = options.get("qss")
        if not isinstance(qss, str):
            return None
        cleaned["options"]["qss"] = qss
    elif kind == "qss_file":
        path = options.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        # Sandbox to QSS_DIR — no traversal, must end with .qss
        candidate = os.path.normpath(path).replace("\\", "/").lstrip("/")
        if ".." in candidate.split("/") or not candidate.lower().endswith(".qss"):
            return None
        cleaned["options"]["path"] = candidate
    # default: no options
    return cleaned


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------

def upsert_preset(state: Dict[str, Any], name: str, preset: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("preset name must be a non-empty string")
    normalized = _normalize_preset(preset)
    if normalized is None:
        raise ValueError(f"invalid preset payload for '{name}'")
    new_state = copy.deepcopy(state)
    new_state.setdefault("presets", {})[name] = normalized
    return _sanitize_state(new_state)


def delete_preset(state: Dict[str, Any], name: str) -> Dict[str, Any]:
    if name in DEFAULT_PRESETS:
        raise ValueError(f"cannot delete built-in preset '{name}'")
    new_state = copy.deepcopy(state)
    presets = new_state.get("presets") or {}
    presets.pop(name, None)
    new_state["presets"] = presets
    if new_state.get("active") == name:
        new_state["active"] = DEFAULT_ACTIVE
    return _sanitize_state(new_state)


def set_active(state: Dict[str, Any], name: str) -> Dict[str, Any]:
    new_state = copy.deepcopy(state)
    if name not in (new_state.get("presets") or {}):
        raise ValueError(f"unknown preset '{name}'")
    new_state["active"] = name
    return _sanitize_state(new_state)


def get_active_preset(state: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    presets = state.get("presets") or {}
    name = state.get("active") or DEFAULT_ACTIVE
    if name not in presets:
        name = DEFAULT_ACTIVE if DEFAULT_ACTIVE in presets else next(iter(presets))
    return name, copy.deepcopy(presets[name])


def get_typography(state: Dict[str, Any]) -> Dict[str, Any]:
    return _normalize_typography(state.get("typography"))


def set_typography(state: Dict[str, Any], ui_family: str = "", base_size_pt: int = 0) -> Dict[str, Any]:
    """Update the app-level typography overrides applied after every theme."""
    new_state = copy.deepcopy(state)
    new_state["typography"] = _normalize_typography(
        {"ui_family": ui_family, "base_size_pt": base_size_pt}
    )
    return _sanitize_state(new_state)


def rename_preset(state: Dict[str, Any], old_name: str, new_name: str) -> Dict[str, Any]:
    """Rename a personal preset. Built-ins cannot be renamed."""
    if old_name in DEFAULT_PRESETS:
        raise ValueError(f"cannot rename built-in preset '{old_name}'")
    if not isinstance(new_name, str) or not new_name.strip():
        raise ValueError("new preset name must be a non-empty string")
    new_name = new_name.strip()
    if new_name in DEFAULT_PRESETS:
        raise ValueError(f"name '{new_name}' collides with a built-in preset")
    new_state = copy.deepcopy(state)
    presets = new_state.get("presets") or {}
    if old_name not in presets:
        raise ValueError(f"unknown preset '{old_name}'")
    if new_name == old_name:
        return _sanitize_state(new_state)
    if new_name in presets:
        raise ValueError(f"a preset named '{new_name}' already exists")
    # Preserve insertion order by rebuilding the dict.
    rebuilt: Dict[str, Dict[str, Any]] = {}
    for k, v in presets.items():
        rebuilt[new_name if k == old_name else k] = v
    new_state["presets"] = rebuilt
    if new_state.get("active") == old_name:
        new_state["active"] = new_name
    return _sanitize_state(new_state)


def read_qss(preset: Dict[str, Any]) -> Optional[str]:
    """Return the QSS string a preset would apply, or None for non-text kinds."""
    if not isinstance(preset, dict):
        return None
    kind = preset.get("kind")
    options = preset.get("options") or {}
    if kind == "qss":
        qss = options.get("qss")
        return qss if isinstance(qss, str) else None
    if kind == "qss_file":
        path = options.get("path") or ""
        full = os.path.join(QSS_DIR, path)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    return None


_FILENAME_SAFE = re.compile(r"[^a-z0-9_-]+")


def _slug(name: str) -> str:
    base = _FILENAME_SAFE.sub("-", (name or "").lower()).strip("-")
    return base or "imported"


def import_qss_from_file(state: Dict[str, Any], source_path: str, preset_name: str) -> Dict[str, Any]:
    """Copy an external .qss file into QSS_DIR and register a `qss_file` preset.

    Filename collisions are resolved by appending `-N`. Returns the updated
    state; the caller is responsible for save_state(). Raises ValueError on
    invalid input.
    """
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("source path is required")
    if not source_path.lower().endswith(".qss"):
        raise ValueError("only .qss files can be imported")
    if not os.path.isfile(source_path):
        raise ValueError(f"file not found: {source_path}")
    if not isinstance(preset_name, str) or not preset_name.strip():
        raise ValueError("preset name is required")
    cleaned_name = preset_name.strip()
    if cleaned_name in DEFAULT_PRESETS:
        raise ValueError(f"name '{cleaned_name}' collides with a built-in preset")

    os.makedirs(QSS_DIR, exist_ok=True)
    base = _slug(cleaned_name)
    candidate = f"{base}.qss"
    counter = 1
    while os.path.exists(os.path.join(QSS_DIR, candidate)):
        # If the existing file is identical, reuse it; otherwise append a suffix.
        try:
            with open(source_path, "rb") as src, open(os.path.join(QSS_DIR, candidate), "rb") as dst:
                if src.read() == dst.read():
                    break
        except OSError:
            pass
        candidate = f"{base}-{counter}.qss"
        counter += 1
    dest = os.path.join(QSS_DIR, candidate)
    if not os.path.exists(dest) or os.path.abspath(dest) != os.path.abspath(source_path):
        shutil.copyfile(source_path, dest)

    return upsert_preset(state, cleaned_name, {"kind": "qss_file", "options": {"path": candidate}})


# ---------------------------------------------------------------------------
# Qt application hookup (lazy imports so this module is test-friendly)
# ---------------------------------------------------------------------------

def list_qt_material_themes() -> Iterable[str]:
    try:
        import qt_material
        return tuple(sorted(qt_material.list_themes()))
    except Exception:
        return ()


def _typography_qss(typography: Optional[Dict[str, Any]]) -> str:
    """Build a small QSS snippet that applies app-level font overrides."""
    if not typography:
        return ""
    family = (typography.get("ui_family") or "").strip()
    size = int(typography.get("base_size_pt") or 0)
    parts = []
    if family:
        # Use a quoted family + a fallback to keep things robust.
        parts.append(f'font-family: "{family}", "Segoe UI", system-ui, sans-serif;')
    if size > 0:
        parts.append(f"font-size: {size}pt;")
    if not parts:
        return ""
    return "\nQWidget { " + " ".join(parts) + " }\n"


def apply_to_app(app, preset: Dict[str, Any], typography: Optional[Dict[str, Any]] = None) -> None:
    """Apply a preset to a running QApplication, with optional typography override.

    `typography` is the app-level overrides dict (see `get_typography`). Pass
    `None` to skip overrides — callers that want to honour user typography
    should pass `themes.get_typography(state)`.
    """
    kind = preset.get("kind")
    options = preset.get("options") or {}
    base_qss = ""
    if kind == "qt_material":
        try:
            from qt_material import apply_stylesheet
            apply_stylesheet(app, theme=options.get("theme") or "dark_teal.xml")
            # qt_material sets the stylesheet itself; capture it so we can append.
            base_qss = app.styleSheet() or ""
        except Exception:
            base_qss = ""
    elif kind == "qdarkstyle":
        try:
            import qdarkstyle
            palette_name = options.get("palette") or "dark"
            if palette_name == "light":
                from qdarkstyle.light.palette import LightPalette as Palette
            else:
                from qdarkstyle.dark.palette import DarkPalette as Palette
            base_qss = qdarkstyle.load_stylesheet(qt_api="pyside6", palette=Palette)
        except Exception:
            base_qss = ""
    elif kind == "qss":
        base_qss = options.get("qss") or ""
    elif kind == "qss_file":
        path = options.get("path") or ""
        full = os.path.join(QSS_DIR, path)
        try:
            with open(full, "r", encoding="utf-8") as fh:
                base_qss = fh.read()
        except OSError:
            base_qss = ""
    else:
        # default / fallback: clear any previous stylesheet so we get the native look.
        base_qss = ""

    typo_qss = _typography_qss(typography)
    app.setStyleSheet(base_qss + typo_qss)


def list_qss_files() -> Iterable[str]:
    """Return the names of bundled .qss files (relative to QSS_DIR)."""
    if not os.path.isdir(QSS_DIR):
        return ()
    return tuple(sorted(f for f in os.listdir(QSS_DIR) if f.lower().endswith(".qss")))
