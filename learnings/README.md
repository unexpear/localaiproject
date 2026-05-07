# learnings/

Per-skill recipe knowledge. One markdown file per skill. The chat
service auto-includes the matching file as a system message when a
session has that skill active.

## Why this folder exists

Skills (in `app/services/skill_service.py`) currently apply lane biases
— they push the router toward `coding` for `builder`, `reasoning` for
`reviewer`, and so on. That's a routing tweak, not knowledge.

This folder layers on actual recipe knowledge: the patterns and
conventions specific to a skill. The model gets these in its system
context only when the relevant skill is active, so we keep the prompt
budget tight.

The pattern is borrowed from rs-sdk's `learnings/` folder, which has
one file per game skill (banking, combat, fishing). Same idea here —
one file per workflow skill.

## How it gets included

(Planned — see also `docs/design/notes-buffer.md` for the related
notes injection pattern.)

In `app/services/chat_service.py`, alongside the existing skill
biases:

```python
def _build_runtime_messages(summary, recent_history, current_message,
                             session_context=None, ...):
    ...
    for skill_name in (context.get("active_skills") or []):
        recipe_text = _load_learnings_recipe(skill_name)  # NEW
        if recipe_text:
            messages.append({
                "role": "system",
                "content": (
                    f"Recipe knowledge for the '{skill_name}' skill "
                    "(loaded from learnings/{skill_name}.md):\n\n"
                    + recipe_text
                ),
            })
    ...
```

`_load_learnings_recipe(name)` reads `learnings/<name>.md` if it
exists, caches the text in-process, returns the body. Recipes that
don't exist are silently skipped.

Cap: load at most 8 KB per recipe so a runaway markdown file can't
dominate the prompt.

## Conventions

- One file per skill, named after the skill: `learnings/builder.md`
- First-level heading is the skill name
- Sections are short, **opinionated**, and **example-driven**
- Don't repeat AGENTS.md material; reference it
- "Don't" rules are as important as "do" rules — list both
- Each recipe should fit comfortably in ~6 KB of markdown so the 8 KB
  cap doesn't bite

## Adding a new recipe

1. Pick a skill name (lowercase, alphanumeric + hyphens)
2. Write `learnings/<name>.md` following the conventions above
3. Register the skill in `app/services/skill_service.py` so it can be
   activated with `skill add <name>`
4. (Optional) Add a lane bias to the same file
5. Add a short test asserting the recipe loads and is non-empty

## Current recipes

| File           | Active when `skill add <name>` |
|----------------|-------------------------------|
| `builder.md`   | `skill add builder`           |
| `reviewer.md`  | `skill add reviewer`          |
| `planner.md`   | `skill add planner`           |
