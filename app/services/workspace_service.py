import os
import re

TOP_LEVEL_LIMIT = 12
WORKSPACE_READ_MAX_CHARS = 6000
WORKSPACE_READ_MAX_LINES = 200
WORKSPACE_SEARCH_MAX_MATCHES = 20
WORKSPACE_SEARCH_MAX_FILES = 8
WORKSPACE_SEARCH_MAX_FILE_BYTES = 400000
WORKSPACE_PROMPT_MAX_CHARS = 3500

MARKER_FILES = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "docker-compose.yml",
)

IGNORED_DIRS = {
    ".git",
    ".idea",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

SUPPORTED_FILE_EXTENSIONS = (
    "c",
    "cpp",
    "cs",
    "css",
    "go",
    "html",
    "java",
    "js",
    "json",
    "jsx",
    "md",
    "php",
    "ps1",
    "py",
    "rb",
    "rs",
    "sh",
    "sql",
    "toml",
    "ts",
    "tsx",
    "txt",
    "yaml",
    "yml",
)

CONFIG_DATA_EXTENSIONS = {
    "json",
    "toml",
    "yaml",
    "yml",
}

CONFIG_DATA_FILENAMES = {
    ".env",
    ".env.example",
    "cargo.toml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "go.mod",
    "local_memory.json",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
    "yarn.lock",
}

CONFIG_DATA_HINTS = {
    "all",
    "config",
    "configs",
    "configuration",
    "configurations",
    "data",
    "env",
    "json",
    "lockfile",
    "lockfiles",
    "manifest",
    "manifests",
    "schema",
    "schemas",
    "settings",
    "toml",
    "yaml",
    "yml",
}

PATH_PATTERN = re.compile(
    rf"\b(?:[A-Za-z]:)?[\w./\\-]+\.({'|'.join(SUPPORTED_FILE_EXTENSIONS)})\b",
    re.IGNORECASE,
)
SEARCH_CUE_PATTERN = re.compile(r"\b(find|search|grep|locate|look for|where is|where are|which file)\b", re.IGNORECASE)
FOLLOW_UP_FILE_VERB_PATTERN = re.compile(
    r"\b(explain|inspect|open|read|show|review|analyze|summarize|fix|refactor|edit|update)\b",
    re.IGNORECASE,
)
BACKTICK_PATTERN = re.compile(r"`([^`]{2,120})`")
QUOTE_PATTERN = re.compile(r'"([^"]{2,120})"|\'([^\']{2,120})\'')
IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]{2,}\b")


def resolve_workspace_path(raw_path, cwd=None):
    text = str(raw_path or "").strip().strip('"').strip("'")
    if not text:
        return None, "Usage: workspace <path>"

    expanded = os.path.expandvars(os.path.expanduser(text))
    if cwd and not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)

    resolved = os.path.abspath(expanded)
    if not os.path.exists(resolved):
        return None, f"Workspace path does not exist: {resolved}"
    if not os.path.isdir(resolved):
        return None, f"Workspace path is not a directory: {resolved}"
    return resolved, None


def _detect_stack(path):
    names = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                names.add(entry.name)
    except OSError:
        return "directory"

    if "pyproject.toml" in names or "requirements.txt" in names:
        return "python"
    if "package.json" in names:
        return "node"
    if "Cargo.toml" in names:
        return "rust"
    if "go.mod" in names:
        return "go"
    if any(name.endswith(".sln") for name in names):
        return "dotnet"
    if {"app", "src", "tests"} & names:
        return "code"
    return "directory"


def inspect_workspace(path):
    if not path:
        return {
            "ok": False,
            "error": "no_workspace_configured",
            "path": None,
        }

    resolved = os.path.abspath(path)
    if not os.path.isdir(resolved):
        return {
            "ok": False,
            "error": "workspace_unavailable",
            "path": resolved,
        }

    top_level = []
    detected_markers = []
    try:
        with os.scandir(resolved) as entries:
            children = sorted(entries, key=lambda entry: (not entry.is_dir(), entry.name.lower()))
            for entry in children:
                display_name = entry.name + ("/" if entry.is_dir() else "")
                if len(top_level) < TOP_LEVEL_LIMIT:
                    top_level.append(display_name)
                if entry.name in MARKER_FILES and entry.name not in detected_markers:
                    detected_markers.append(entry.name)
    except OSError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "path": resolved,
        }

    return {
        "ok": True,
        "path": resolved,
        "stack": _detect_stack(resolved),
        "markers": detected_markers,
        "top_level": top_level,
    }


def render_workspace_status(path):
    snapshot = inspect_workspace(path)
    if not snapshot["ok"]:
        if snapshot["error"] == "no_workspace_configured":
            return "No workspace configured for this session."
        return f"Workspace unavailable: {snapshot['path']}"

    lines = [
        f"Workspace: {snapshot['path']}",
        f"Detected project shape: {snapshot['stack']}",
    ]
    lines.append(f"Markers: {', '.join(snapshot['markers']) if snapshot['markers'] else 'none detected'}")
    lines.append(f"Top level: {', '.join(snapshot['top_level']) if snapshot['top_level'] else '(empty)'}")
    lines.append("")
    lines.append("Workspace reads and searches require approval before they run.")
    lines.append("The model only sees this compact snapshot until you approve a scoped action.")
    return "\n".join(lines)


def build_workspace_system_message(path):
    snapshot = inspect_workspace(path)
    if not snapshot["ok"]:
        if snapshot["error"] == "no_workspace_configured":
            return None
        return {
            "role": "system",
            "content": (
                f"The session has a configured workspace path, but it is unavailable: {snapshot['path']}. "
                "Do not assume file contents or claim to have inspected files there."
            ),
        }

    lines = [
        "Pinned workspace context for this session:",
        f"- Root: {snapshot['path']}",
        f"- Detected project shape: {snapshot['stack']}",
        f"- Key markers: {', '.join(snapshot['markers']) if snapshot['markers'] else 'none detected'}",
        f"- Top-level entries: {', '.join(snapshot['top_level']) if snapshot['top_level'] else '(empty)'}",
        "- Workspace reads and searches require explicit user approval before they run.",
        "- You only have this compact workspace snapshot until an approved scoped action provides more context.",
        "- Do not claim to have opened, edited, or executed files unless approved inspection results are present.",
        "- Prefer suggestions that fit this workspace layout and technology stack.",
    ]
    return {
        "role": "system",
        "content": "\n".join(lines),
    }


def _normalize_workspace_root(workspace_root):
    if not workspace_root:
        return None, "no_workspace_configured"
    resolved = os.path.abspath(workspace_root)
    if not os.path.isdir(resolved):
        return None, "workspace_unavailable"
    return resolved, None


def _is_within_workspace(workspace_root, target_path):
    try:
        return os.path.commonpath([workspace_root, target_path]) == workspace_root
    except ValueError:
        return False


def resolve_workspace_item_path(workspace_root, requested_path):
    root, error = _normalize_workspace_root(workspace_root)
    if error:
        return {"ok": False, "error": error}

    raw = str(requested_path or "").strip().strip('"').strip("'")
    if not raw:
        return {"ok": False, "error": "missing_path"}

    candidate = os.path.normpath(raw)
    if not os.path.isabs(candidate):
        candidate = os.path.join(root, candidate)
    candidate = os.path.abspath(candidate)

    if not _is_within_workspace(root, candidate):
        return {"ok": False, "error": "outside_workspace", "workspace_path": root}
    if not os.path.exists(candidate):
        return {"ok": False, "error": "path_not_found", "workspace_path": root, "path": candidate}

    return {
        "ok": True,
        "workspace_path": root,
        "absolute_path": candidate,
        "relative_path": os.path.relpath(candidate, root).replace("\\", "/"),
        "is_dir": os.path.isdir(candidate),
    }


def _is_probably_binary(path):
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
    except OSError:
        return True
    return b"\x00" in chunk


def _render_numbered_lines(text):
    lines = text.splitlines()
    return "\n".join(f"{index + 1}: {line}" for index, line in enumerate(lines))


def _clip_prompt_text(text, max_chars=WORKSPACE_PROMPT_MAX_CHARS):
    cleaned = str(text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "\n..."


def read_workspace_file(workspace_root, requested_path):
    resolved = resolve_workspace_item_path(workspace_root, requested_path)
    if not resolved.get("ok"):
        return {
            "ok": False,
            "kind": "read",
            "error": resolved.get("error"),
            "workspace_path": resolved.get("workspace_path"),
            "requested_path": requested_path,
        }

    if resolved["is_dir"]:
        return {
            "ok": False,
            "kind": "read",
            "error": "path_is_directory",
            "workspace_path": resolved["workspace_path"],
            "relative_path": resolved["relative_path"],
        }

    if _is_probably_binary(resolved["absolute_path"]):
        return {
            "ok": False,
            "kind": "read",
            "error": "binary_file",
            "workspace_path": resolved["workspace_path"],
            "relative_path": resolved["relative_path"],
        }

    try:
        with open(resolved["absolute_path"], "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as exc:
        return {
            "ok": False,
            "kind": "read",
            "error": str(exc),
            "workspace_path": resolved["workspace_path"],
            "relative_path": resolved["relative_path"],
        }

    truncated = False
    if len(lines) > WORKSPACE_READ_MAX_LINES:
        lines = lines[:WORKSPACE_READ_MAX_LINES]
        truncated = True

    content = "".join(lines)
    if len(content) > WORKSPACE_READ_MAX_CHARS:
        content = content[:WORKSPACE_READ_MAX_CHARS].rstrip() + "\n..."
        truncated = True

    return {
        "ok": True,
        "kind": "read",
        "workspace_path": resolved["workspace_path"],
        "absolute_path": resolved["absolute_path"],
        "relative_path": resolved["relative_path"],
        "content": content,
        "content_with_line_numbers": _render_numbered_lines(content),
        "truncated": truncated,
    }


def read_workspace_files(workspace_root, requested_paths):
    results = []
    for path in requested_paths or []:
        result = read_workspace_file(workspace_root, path)
        results.append(result)
        if len(results) >= 2:
            break
    return {
        "ok": any(result.get("ok") for result in results),
        "kind": "read",
        "workspace_path": workspace_root,
        "files": results,
        "error": None if any(result.get("ok") for result in results) else (results[0].get("error") if results else "missing_path"),
    }


def _iter_workspace_files(workspace_root):
    for current_root, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS and not name.startswith(".")]
        for filename in sorted(filenames):
            full_path = os.path.join(current_root, filename)
            relative_path = os.path.relpath(full_path, workspace_root).replace("\\", "/")
            yield full_path, relative_path


def _path_preference_score(relative_path):
    lowered = str(relative_path or "").lower()
    score = 0
    if lowered.startswith(("app/", "src/")):
        score += 3
    if lowered.startswith("tests/") or "/tests/" in lowered:
        score -= 3
    if lowered.endswith(("_test.py", ".spec.js", ".spec.ts", ".test.js", ".test.ts")):
        score -= 2
    return score


def _is_config_or_data_path(relative_path):
    lowered = str(relative_path or "").replace("\\", "/").lower()
    basename = os.path.basename(lowered)
    _, ext = os.path.splitext(basename)
    ext = ext.lstrip(".")

    if basename in CONFIG_DATA_FILENAMES:
        return True
    if ext in CONFIG_DATA_EXTENSIONS:
        return True
    if lowered.endswith("/local_memory.json"):
        return True
    return False


def _parse_search_request(query):
    raw_query = str(query or "").strip()
    lowered = raw_query.lower()
    include_config_data = False

    if re.search(r"\b(include|in)\s+(json|toml|yaml|yml|config|configs|data|all)\b", lowered):
        include_config_data = True

    tokens = raw_query.split()
    cleaned_tokens = []
    for token in tokens:
        lowered_token = token.strip(" ,").lower()
        if lowered_token in CONFIG_DATA_HINTS:
            include_config_data = True
            continue
        if lowered_token in CONFIG_DATA_FILENAMES or any(
            lowered_token.endswith("." + extension) for extension in CONFIG_DATA_EXTENSIONS
        ):
            include_config_data = True
            continue
        cleaned_tokens.append(token)

    match_text = " ".join(cleaned_tokens).strip()
    if not match_text:
        match_text = raw_query

    if not include_config_data and any(hint in lowered for hint in CONFIG_DATA_FILENAMES):
        include_config_data = True

    return {
        "raw_query": raw_query,
        "match_text": match_text,
        "include_config_data": include_config_data,
    }


def _definition_hit_score(query, line):
    escaped = re.escape(str(query or "").strip())
    if not escaped:
        return 0

    patterns = (
        rf"\bdef\s+{escaped}\b",
        rf"\bclass\s+{escaped}\b",
        rf"\bfunction\s+{escaped}\b",
        rf"\b(const|let|var)\s+{escaped}\b",
    )
    return 1 if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns) else 0


def search_workspace(workspace_root, query):
    root, error = _normalize_workspace_root(workspace_root)
    if error:
        return {
            "ok": False,
            "kind": "search",
            "error": error,
            "query": query,
        }

    parsed_query = _parse_search_request(query)
    needle = parsed_query["match_text"]
    if not needle:
        return {
            "ok": False,
            "kind": "search",
            "error": "missing_query",
            "workspace_path": root,
        }

    lowered_needle = needle.lower()
    matches = []
    files_with_matches = []
    file_match_counts = {}
    file_first_seen = {}
    file_definition_hits = {}
    truncated = False

    for full_path, relative_path in _iter_workspace_files(root):
        if _is_config_or_data_path(relative_path) and not parsed_query["include_config_data"]:
            continue
        try:
            size = os.path.getsize(full_path)
        except OSError:
            continue
        if size > WORKSPACE_SEARCH_MAX_FILE_BYTES or _is_probably_binary(full_path):
            continue

        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                for line_number, raw_line in enumerate(f, start=1):
                    if lowered_needle not in raw_line.lower():
                        continue
                    if relative_path not in files_with_matches:
                        files_with_matches.append(relative_path)
                        file_first_seen[relative_path] = len(file_first_seen)
                    file_match_counts[relative_path] = file_match_counts.get(relative_path, 0) + 1
                    file_definition_hits[relative_path] = file_definition_hits.get(relative_path, 0) + _definition_hit_score(
                        needle,
                        raw_line,
                    )
                    matches.append(
                        {
                            "relative_path": relative_path,
                            "line_number": line_number,
                            "line": raw_line.rstrip(),
                        }
                    )
                    if len(matches) >= WORKSPACE_SEARCH_MAX_MATCHES or len(files_with_matches) >= WORKSPACE_SEARCH_MAX_FILES:
                        truncated = True
                        break
        except OSError:
            continue

        if truncated:
            break

    ranked_files = sorted(
        files_with_matches,
        key=lambda path: (
            -file_definition_hits.get(path, 0),
            -_path_preference_score(path),
            -file_match_counts.get(path, 0),
            file_first_seen.get(path, 0),
            path,
        ),
    )
    file_rank = {path: index for index, path in enumerate(ranked_files)}
    ranked_matches = sorted(
        matches,
        key=lambda match: (
            file_rank.get(match["relative_path"], len(file_rank)),
            match["line_number"],
            match["relative_path"],
        ),
    )

    return {
        "ok": bool(ranked_matches),
        "kind": "search",
        "workspace_path": root,
        "query": needle,
        "raw_query": parsed_query["raw_query"],
        "matches": ranked_matches,
        "files_with_matches": ranked_files,
        "truncated": truncated,
        "included_config_data": parsed_query["include_config_data"],
        "error": None if ranked_matches else "no_matches",
    }


def describe_pending_action(action):
    if not isinstance(action, dict):
        return "unknown action"
    if action.get("kind") == "read":
        paths = action.get("paths") or []
        if len(paths) == 1:
            return f"read {paths[0]}"
        return "read " + ", ".join(paths)
    if action.get("kind") == "search":
        return f"search workspace for '{action.get('query', '')}'"
    return "unknown action"


def format_approval_request(action):
    description = describe_pending_action(action)
    reason = action.get("reason")
    lines = [
        "[approval required]",
        f"Requested action: {description}",
    ]
    if reason:
        lines.append(f"Why: {reason}")
    lines.append("Type `approve` to continue or `deny` to cancel.")
    return "\n".join(lines)


def format_workspace_read_result(result):
    if not result.get("ok"):
        lines = ["[approved workspace read]"]
        for file_result in result.get("files", []):
            lines.append(f"- {file_result.get('requested_path') or file_result.get('relative_path')}: {file_result.get('error')}")
        if len(lines) == 1:
            lines.append(f"Workspace read failed: {result.get('error', 'read_failed')}.")
        return "\n".join(lines)

    lines = ["[approved workspace read]"]
    for file_result in result.get("files", []):
        if not file_result.get("ok"):
            lines.append(f"- {file_result.get('requested_path') or file_result.get('relative_path')}: {file_result.get('error')}")
            continue
        lines.append("")
        lines.append(f"File: {file_result['relative_path']}")
        if file_result.get("truncated"):
            lines.append("Showing a bounded excerpt.")
        lines.append(file_result["content_with_line_numbers"])
    return "\n".join(lines)


def format_workspace_search_result(result):
    if not result.get("ok"):
        if result.get("error") == "no_matches":
            return f"[approved workspace search]\nNo matches found for '{result.get('query', '')}'."
        return f"Workspace search failed: {result.get('error', 'search_failed')}."

    lines = [
        "[approved workspace search]",
        f"Query: {result['query']}",
        f"Matches: {len(result['matches'])} in {len(result['files_with_matches'])} file(s)",
    ]
    for match in result["matches"]:
        lines.append(f"- {match['relative_path']}:{match['line_number']}: {match['line']}")
    if result.get("truncated"):
        lines.append("- Results truncated to keep the scope tight.")
    return "\n".join(lines)


def build_workspace_inspection_system_message(result):
    if not result or not result.get("ok"):
        return None

    if result.get("kind") == "read":
        lines = ["Approved workspace inspection results:"]
        for file_result in result.get("files", []):
            if not file_result.get("ok"):
                continue
            lines.append(f"File: {file_result['relative_path']}")
            if file_result.get("truncated"):
                lines.append("Note: excerpt truncated.")
            lines.append(file_result["content_with_line_numbers"])
            lines.append("")
        if len(lines) == 1:
            return None
        content = _clip_prompt_text("\n".join(lines).strip())
        return {
            "role": "system",
            "content": content,
        }

    if result.get("kind") == "search":
        lines = [
            "Approved workspace inspection results:",
            f"Search query: {result['query']}",
        ]
        for match in result.get("matches", []):
            lines.append(f"- {match['relative_path']}:{match['line_number']}: {match['line']}")
        if result.get("truncated"):
            lines.append("- Results were truncated.")
        content = _clip_prompt_text("\n".join(lines))
        return {
            "role": "system",
            "content": content,
        }

    return None


def _extract_search_query(message):
    for pattern in (BACKTICK_PATTERN, QUOTE_PATTERN):
        for match in pattern.finditer(message):
            query = next((group for group in match.groups() if group), None)
            if query and not PATH_PATTERN.search(query):
                return query.strip()

    for match in IDENTIFIER_PATTERN.finditer(message):
        candidate = match.group(0).strip()
        if candidate.lower() in {"find", "search", "grep", "look", "where", "which", "file"}:
            continue
        return candidate
    return None


def _basename_candidates(path):
    base = os.path.basename(path).lower()
    stem, _ = os.path.splitext(base)
    candidates = [base]
    if stem and stem != base:
        candidates.append(stem)
    return candidates


def _select_follow_up_search_file(message, approved_search):
    if not isinstance(approved_search, dict):
        return None

    files = [str(path or "").strip() for path in approved_search.get("files", []) if str(path or "").strip()]
    if not files:
        return None

    lowered = (message or "").lower()
    if not FOLLOW_UP_FILE_VERB_PATTERN.search(lowered):
        return None

    for path in files:
        for candidate in _basename_candidates(path):
            pattern = r"\b" + re.escape(candidate) + r"\b"
            if re.search(pattern, lowered, re.IGNORECASE):
                return path

    if re.search(r"\b(second|2nd)\s+(file|result|match|one)\b", lowered) and len(files) >= 2:
        return files[1]

    if re.search(r"\b(third|3rd)\s+(file|result|match|one)\b", lowered) and len(files) >= 3:
        return files[2]

    if re.search(r"\b(first|1st|top)\s+(file|result|match|one)\b", lowered):
        return files[0]

    if re.search(r"\b(that|this|same)\s+(file|result|match|one)\b", lowered):
        return files[0]

    if len(files) == 1 and re.search(r"\b(file|result|match|it)\b", lowered):
        return files[0]

    return None


def plan_workspace_action(message, workspace_root, approved_search=None):
    root, error = _normalize_workspace_root(workspace_root)
    if error:
        return None

    candidate_paths = []
    seen_paths = set()
    for match in PATH_PATTERN.finditer(message or ""):
        candidate = match.group(0).rstrip(".,:;)")
        resolved = resolve_workspace_item_path(root, candidate)
        if not resolved.get("ok") or resolved.get("is_dir"):
            continue
        relative_path = resolved["relative_path"]
        if relative_path in seen_paths:
            continue
        seen_paths.add(relative_path)
        candidate_paths.append(relative_path)
        if len(candidate_paths) >= 2:
            break

    if candidate_paths:
        return {
            "kind": "read",
            "origin": "chat",
            "workspace_path": root,
            "paths": candidate_paths,
            "reason": "your message referenced pinned workspace files",
        }

    if SEARCH_CUE_PATTERN.search(message or ""):
        query = _extract_search_query(message or "")
        if query:
            return {
                "kind": "search",
                "origin": "chat",
                "workspace_path": root,
                "query": query,
                "reason": "your message asked to search the pinned workspace",
            }

    follow_up_file = _select_follow_up_search_file(message, approved_search)
    if follow_up_file:
        return {
            "kind": "read",
            "origin": "chat",
            "workspace_path": root,
            "paths": [follow_up_file],
            "reason": "your follow-up referred to a file from the last approved workspace search",
        }

    return None
