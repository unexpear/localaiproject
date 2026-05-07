# Credits

External projects whose ideas shaped this codebase. We adopted
**patterns and concepts**, not code — but we want to acknowledge
where the thinking came from so future readers can trace lineage and
go to the source for deeper context.

Each project is independent and unaffiliated with this codebase.
Where there's a public repository, we link to it; please consult
that project's own LICENSE for any code distributed there. Pattern
adoption (an idea, not implementation) does not transfer license
obligations, but credit is the right thing to do.

---

## OpenCode

Open-source coding-agent CLI whose UX vocabulary directly shaped
ours.

**Adapted into this codebase:**

- **Plan / Build mode** framing — the read-only-vs-allowed-edits
  switch and the system message that tells the model which mode
  it's in. See `design/prompt-assembly.md` (mode slot) and the
  dispatcher's `mode plan` / `mode build` commands.
- **`@file` mention expansion** — typing `@app/foo.py` inlines that
  file's contents at the user-message level, scoped to the pinned
  workspace, with byte caps. See `app/services/mentions_service.py`.
- **Custom slash commands** — extending the dispatcher vocabulary
  via user-defined recipes. See `design/extensibility.md` and the
  `templates/` folder.
- **Undo / redo** for in-session mutations. See `local_memory.py`
  undo stack + the `undo` / `redo` dispatcher commands.

---

## Sandcastle

[github.com/mattpocock/sandcastle](https://github.com/mattpocock/sandcastle)

A "run an agent until it says it's done" toolkit. We borrowed
several primitives from its lifecycle.

**Adapted into this codebase:**

- **`iterate_until_complete` with a `<task-complete>` marker** — a
  structured way for an agent to signal completion. ✅ shipped in
  `api/goals.md`.
- **Branch strategy** (head / merge-to-head / branch) for cowork
  builders working in isolation. ✅ shipped in
  `design/cowork-mode.md` Phase 5.
- **Sub-task lifecycle hooks** — pre/post/error callbacks per
  delegated unit. ✅ shipped.
- **Backlog adapters** — pluggable input sources for queued work.
  ✅ shipped Phase 1 in `design/extensibility.md` §8.
- **Per-iteration git worktrees** for safe parallel attempts.
  Phase 4 of `design/sandbox.md` (planned).
- **Dispatcher-command expressions in templates** — Phase 2 of
  `design/workflow-templates.md` (planned).

---

## Letta

[github.com/letta-ai/letta](https://github.com/letta-ai/letta)
(formerly MemGPT)

Long-running stateful agents with explicit memory architecture.
Our memory model is a direct lift of theirs (re-implemented from
scratch, not vendored).

**Adapted into this codebase:**

- **Structured memory blocks** — multiple labelled, length-bounded,
  separately-editable memory units instead of one opaque blob.
  Each block has a `description`, `value`, `char_limit`, and
  `priority`. Phase 2 of `design/notes-buffer.md` ✅ shipped.
- **Sleep-time consolidation** as a latency-tolerant CoWork hook
  (compaction runs *between* turns, not in the user's hot path).
  Phase 7 of `design/cowork-mode.md` (planned).
- **Recall memory** as searchable session history with citations.
  `design/recall-memory.md` ✅ Phase 1 + 2a + 2b shipped.
- **Shared memory blocks** for planner ↔ builder coordination —
  one block both sides read and write. Phase 7 of
  `design/cowork-mode.md` (planned).

---

## Hermes Agent

[github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)

Open-weights agent toolkit whose architectural choices we found
particularly clean and lifted directly.

**Adapted into this codebase:**

- **Portable `SKILL.md` format** aligned with
  [agentskills.io](https://agentskills.io) — frontmatter + body
  recipes that any agent runtime can consume. Phase 5 of
  `design/workflow-templates.md` (planned).
- **Layered prompt assembly with explicit slot producers,
  per-slot budgets, and provenance trace** — every contribution to
  the system prompt is a typed `PromptSlot` with a name, priority,
  and char budget. `design/prompt-assembly.md` ✅ Phase 1 + 2 + 3
  + 4 shipped — this is one of the most-used systems in our code.
- **Pluggable execution backends** (in-process / git worktree /
  Docker) — same interface, different isolation levels. Phase 6
  of `design/cowork-mode.md` (planned).
- **Provider-runtime resolver consolidation** — one resolver that
  picks (provider, model) per lane instead of scattered lookups.
  Phase 6 of `design/chat-providers.md` (planned).

---

## Abacus AI Desktop

Commercial multi-surface AI workspace whose UX patterns shaped how
we think about surface integration.

**Adapted into this codebase:**

- **Multi-surface workflow integration** — the same agent reachable
  from CLI, terminal, and the desktop UI. See `vision/surfaces.md`.
- **Multi-provider chat lanes** — opt-in routing per lane to
  OpenAI / Anthropic / Google / local. Phase 1 of
  `design/chat-providers.md` ✅ shipped.
- **CoWork mode** — plan → approve → execute → synthesize as a
  state machine. Phase 1 of `design/cowork-mode.md` ✅ shipped.
- **Extensibility umbrella** — the framing that skills, commands,
  plugins, MCP, browser automation, and agent rules are all
  variations on "user-supplied behavior." `design/extensibility.md`.

---

## Conventions for adding to this list

- Adopt **ideas**, not code, unless we vendor with their license
  intact and a clear `THIRD_PARTY_NOTICES.md` entry.
- Each entry names the project, links to a public source if any,
  and lists the **specific** patterns we adapted with pointers to
  where they live in our docs. "Inspired by X" with no detail
  isn't a credit — it's a vibe.
- If a project's only contribution is a single quote / aphorism
  (no architecture, no code shape, no concept lift), credit it
  inline at the point of use rather than in this file.
