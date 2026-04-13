import re

SKILL_CATALOG = {
    "builder": {
        "description": "Implementation-first help for code and product changes.",
        "instruction": (
            "Treat concrete requests as build work. Prefer implementation-ready answers, call out likely tests, "
            "and keep suggestions grounded in the supplied workspace and conversation."
        ),
        "lane_bias": {"coding": 2},
    },
    "reviewer": {
        "description": "Senior-engineer review mode focused on bugs, regressions, and missing tests.",
        "instruction": (
            "Use a review stance. Lead with bugs, regressions, risky assumptions, and missing tests before minor polish."
        ),
        "lane_bias": {"coding": 2},
    },
    "planner": {
        "description": "Turns rough ideas into milestones, sequencing, and next moves.",
        "instruction": (
            "Turn rough requests into scope, milestones, dependencies, and the next practical move."
        ),
        "lane_bias": {"reasoning": 2},
    },
    "concise": {
        "description": "Keeps answers short, direct, and high-signal.",
        "instruction": "Keep answers compact, direct, and easy to scan. Skip long preambles.",
        "lane_bias": {},
    },
    "safe": {
        "description": "Adds extra caution around risky, destructive, or high-impact actions.",
        "instruction": (
            "Stay conservative around destructive or high-impact actions. Prefer reversible steps and explicit caution."
        ),
        "lane_bias": {},
    },
}

SKILL_ALIASES = {
    "build": "builder",
    "builder": "builder",
    "code-review": "reviewer",
    "plan": "planner",
    "review": "reviewer",
    "reviewer": "reviewer",
    "short": "concise",
    "concise": "concise",
    "safe": "safe",
    "safety": "safe",
}


def normalize_skill_name(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if not slug:
        return None
    return SKILL_ALIASES.get(slug, slug)


def get_skill(name):
    normalized = normalize_skill_name(name)
    if not normalized:
        return None
    skill = SKILL_CATALOG.get(normalized)
    if not skill:
        return None
    return {"name": normalized, **skill}


def normalize_active_skills(skills):
    normalized = []
    seen = set()
    for skill_name in skills or []:
        resolved = normalize_skill_name(skill_name)
        if not resolved or resolved in seen or resolved not in SKILL_CATALOG:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return normalized


def list_skills():
    return [{"name": name, **info} for name, info in sorted(SKILL_CATALOG.items())]


def get_lane_biases(active_skills):
    lane_biases = {"coding": 0, "vision": 0, "reasoning": 0}
    for skill_name in normalize_active_skills(active_skills):
        skill = SKILL_CATALOG[skill_name]
        for lane, score in skill.get("lane_bias", {}).items():
            lane_biases[lane] = lane_biases.get(lane, 0) + int(score)
    return {lane: score for lane, score in lane_biases.items() if score > 0}


def format_skill_overview(active_skills):
    active = normalize_active_skills(active_skills)
    lines = []
    lines.append(f"Active skills: {', '.join(active) if active else 'none'}")
    lines.append("")
    lines.append("Available skills")
    for skill in list_skills():
        lines.append(f"- {skill['name']}: {skill['description']}")
    lines.append("")
    lines.append("Use: skill add <name>, skill remove <name>, skill clear")
    return "\n".join(lines)


def build_skill_system_message(active_skills):
    active = normalize_active_skills(active_skills)
    if not active:
        return None

    lines = ["Active operating skills for this session:"]
    for skill_name in active:
        skill = SKILL_CATALOG[skill_name]
        lines.append(f"- {skill_name}: {skill['instruction']}")

    return {
        "role": "system",
        "content": "\n".join(lines),
    }
