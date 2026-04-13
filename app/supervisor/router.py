import re

LANE_PRIORITY = {
    "coding": 0,
    "vision": 1,
    "reasoning": 2,
}

FOLLOW_UP_STARTS = (
    "and ",
    "also ",
    "continue",
    "do that",
    "do the same",
    "expand",
    "explain that",
    "fix it",
    "keep going",
    "make it",
    "now ",
    "shorten",
    "what about",
)

FOLLOW_UP_TOKENS = {
    "again",
    "also",
    "another",
    "cleaner",
    "continue",
    "faster",
    "here",
    "it",
    "longer",
    "same",
    "shorter",
    "that",
    "them",
    "this",
    "too",
}

CODE_FILE_EXTENSIONS = (
    "py",
    "js",
    "ts",
    "tsx",
    "jsx",
    "java",
    "c",
    "cpp",
    "cs",
    "go",
    "rs",
    "rb",
    "php",
    "sql",
    "html",
    "css",
    "json",
    "yaml",
    "yml",
    "toml",
    "sh",
    "ps1",
)

IMAGE_FILE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "svg")

CODING_PHRASES = (
    "write code",
    "write a script",
    "debug this",
    "fix this bug",
    "fix this error",
    "refactor this",
    "review this code",
    "explain this code",
    "help me code",
    "help me program",
)

CODING_TERMS = (
    "api",
    "backend",
    "bug",
    "class",
    "code",
    "compile",
    "database",
    "debug",
    "deploy",
    "docker",
    "endpoint",
    "exception",
    "function",
    "git",
    "http",
    "json",
    "lint",
    "npm",
    "package",
    "pip",
    "python",
    "query",
    "refactor",
    "regex",
    "script",
    "sdk",
    "sql",
    "stack trace",
    "test",
    "traceback",
    "typescript",
)

VISION_TERMS = (
    "image",
    "picture",
    "photo",
    "screenshot",
    "diagram",
    "chart",
    "graph",
    "visual",
    "camera",
    "ocr",
    "scan",
    "see",
)

VISION_PHRASES = (
    "look at this image",
    "what is in this image",
    "describe this picture",
    "analyze this screenshot",
    "read this screenshot",
    "extract text from",
)

REASONING_TERMS = (
    "brainstorm",
    "compare",
    "consider",
    "decision",
    "evaluate",
    "explain",
    "idea",
    "plan",
    "pros and cons",
    "recommend",
    "summarize",
    "think through",
    "why",
)

CODING_REGEXES = (
    (re.compile(r"```"), 6, "contains a fenced code block"),
    (re.compile(r"\b(traceback|exception|syntaxerror|typeerror|nullreference)\b", re.IGNORECASE), 6, "looks like an error or stack trace"),
    (re.compile(r"\b(def|class|import|from|return|console\.log|select|insert|update|delete|function|const|let|var)\b", re.IGNORECASE), 4, "contains code-like syntax"),
    (re.compile(rf"\b[\w./\\-]+\.({'|'.join(CODE_FILE_EXTENSIONS)})\b", re.IGNORECASE), 4, "references a code or config file"),
    (re.compile(r"[{}();]{2,}|=>"), 2, "contains programming punctuation"),
)

VISION_REGEXES = (
    (re.compile(rf"\b[\w./\\-]+\.({'|'.join(IMAGE_FILE_EXTENSIONS)})\b", re.IGNORECASE), 6, "references an image file"),
    (re.compile(r"\b(screenshot|image|photo|picture|diagram|chart|graph)\b", re.IGNORECASE), 4, "mentions visual input"),
    (re.compile(r"\b(describe|analyze|inspect|read|extract)\b.*\b(image|photo|picture|screenshot|diagram|chart)\b", re.IGNORECASE), 5, "asks to inspect visual content"),
)

REASONING_REGEXES = (
    (re.compile(r"\b(why|how|should|compare|summarize|brainstorm|plan|explain)\b", re.IGNORECASE), 2, "asks for general reasoning or explanation"),
)


def _normalize(message):
    return (message or "").replace("\ufeff", "").replace("ï»¿", "").strip()


def _contains_any(text, phrases):
    return any(phrase in text for phrase in phrases)


def _count_term_hits(text, terms):
    hits = 0
    for term in terms:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, text, re.IGNORECASE):
            hits += 1
    return hits


def _add_signal(route_info, lane, score, reason):
    route_info["scores"][lane] += score
    route_info["signals"][lane].append({"score": score, "reason": reason})


def _score_regexes(route_info, message, regexes, lane):
    for pattern, score, reason in regexes:
        if pattern.search(message):
            _add_signal(route_info, lane, score, reason)


def _primary_signal(signals):
    if not signals:
        return "default"
    strongest = sorted(signals, key=lambda item: (-item["score"], item["reason"]))[0]
    return strongest["reason"]


def _rank_scores(scores):
    return sorted(scores.items(), key=lambda item: (-item[1], LANE_PRIORITY[item[0]]))


def _is_follow_up_prompt(normalized, lowered):
    words = lowered.split()
    if not words:
        return False
    if any(lowered.startswith(prefix) for prefix in FOLLOW_UP_STARTS):
        return True
    if len(words) <= 8 and any(token in FOLLOW_UP_TOKENS for token in words):
        return True
    if len(words) <= 12 and re.search(r"\b(it|this|that|them|same)\b", lowered):
        return True
    return False


def _context_lane(entry):
    metadata = entry.get("metadata") or {}
    lane = metadata.get("resolved_lane") or metadata.get("requested_lane")
    if lane in LANE_PRIORITY:
        return lane
    return None


def _apply_context(route_info, recent_history, normalized, lowered):
    ranking = _rank_scores(route_info["scores"])
    best_score = ranking[0][1]
    second_score = ranking[1][1]
    ambiguous_current_prompt = best_score - second_score <= 2
    follow_up_prompt = _is_follow_up_prompt(normalized, lowered)

    route_info["context_used"] = False
    route_info["context_reason"] = None

    if not recent_history or not (follow_up_prompt or ambiguous_current_prompt):
        return

    context_entries = recent_history[-4:]
    for distance, entry in enumerate(reversed(context_entries), start=1):
        lane = _context_lane(entry)
        if not lane:
            continue

        score = 3 if distance == 1 else 2 if distance <= 3 else 1
        if entry.get("role") == "user":
            score += 1
        if ambiguous_current_prompt and not follow_up_prompt:
            score = max(1, score - 1)

        _add_signal(route_info, lane, score, f"recent {entry.get('role', 'conversation')} context favored {lane}")
        route_info["context_used"] = True

    if route_info["context_used"]:
        reasons = []
        if follow_up_prompt:
            reasons.append("follow-up prompt")
        if ambiguous_current_prompt:
            reasons.append("ambiguous current prompt")
        route_info["context_reason"] = ", ".join(reasons)


def _apply_lane_biases(route_info, lane_biases):
    for lane, score in (lane_biases or {}).items():
        if lane not in LANE_PRIORITY or not score:
            continue
        _add_signal(route_info, lane, int(score), "active session skills favored this lane")


def route_message(message, recent_history=None, lane_biases=None):
    """
    Score the message and choose the best lane.

    Returns a dict containing lane, confidence, scores, and signals.
    """
    normalized = _normalize(message)
    lowered = normalized.lower()
    route_info = {
        "lane": "reasoning",
        "confidence": "low",
        "scores": {"coding": 0, "vision": 0, "reasoning": 1},
        "signals": {"coding": [], "vision": [], "reasoning": [{"score": 1, "reason": "default general-purpose lane"}]},
        "context_used": False,
        "context_reason": None,
    }

    if not normalized:
        route_info["primary_signal"] = "default general-purpose lane"
        return route_info

    _score_regexes(route_info, normalized, CODING_REGEXES, "coding")
    _score_regexes(route_info, normalized, VISION_REGEXES, "vision")
    _score_regexes(route_info, normalized, REASONING_REGEXES, "reasoning")

    if _contains_any(lowered, CODING_PHRASES):
        _add_signal(route_info, "coding", 5, "contains an explicit coding request")

    if _contains_any(lowered, VISION_PHRASES):
        _add_signal(route_info, "vision", 5, "contains an explicit visual-analysis request")

    coding_term_hits = _count_term_hits(normalized, CODING_TERMS)
    if coding_term_hits:
        _add_signal(route_info, "coding", min(5, coding_term_hits + 1), "contains programming-specific vocabulary")

    vision_term_hits = _count_term_hits(normalized, VISION_TERMS)
    if vision_term_hits:
        _add_signal(route_info, "vision", min(4, vision_term_hits), "contains vision-related vocabulary")

    reasoning_term_hits = _count_term_hits(normalized, REASONING_TERMS)
    if reasoning_term_hits:
        _add_signal(route_info, "reasoning", min(4, reasoning_term_hits), "contains general reasoning vocabulary")

    if len(normalized.split()) <= 3 and route_info["scores"]["coding"] == 0 and route_info["scores"]["vision"] == 0:
        _add_signal(route_info, "reasoning", 1, "short prompt without domain-specific cues")

    _apply_lane_biases(route_info, lane_biases)
    _apply_context(route_info, recent_history or [], normalized, lowered)

    ranking = _rank_scores(route_info["scores"])
    best_lane, best_score = ranking[0]
    second_score = ranking[1][1]
    margin = best_score - second_score

    if best_score >= 7 and margin >= 3:
        confidence = "high"
    elif best_score >= 4 and margin >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    route_info["lane"] = best_lane
    route_info["confidence"] = confidence
    route_info["primary_signal"] = _primary_signal(route_info["signals"][best_lane])
    return route_info


def route(message, recent_history=None, lane_biases=None):
    """Backward-compatible lane-only router."""
    return route_message(message, recent_history=recent_history, lane_biases=lane_biases)["lane"]
