# Design: Custom Instructions

**Status: Phase 1 + minimal Phase 2 + Phase 2.5 + Phase 3a shipped · 2026-05-07**

ChatGPT-style "custom instructions" — except **typed, conditional,
addressable, and per-session.** A first-class CRUD-able list of named
behavioural rules the model sees on every turn (filtered by the rule's
own `applies_to` predicate).

This is **net-new design**, not a phase increment of any existing
doc. Builds on `prompt-assembly.md` (the layered slot system),
`compact-prompt.md` (the dense-renderer flag), and Letta-inspired
memory blocks (`notes-buffer.md` Phase 2) — but is its own concept.

## Why

ChatGPT/Claude's "custom instructions" is one global text box. It's
useful but blunt:

- Always-on, even when irrelevant to the task.
- Free-form prose; the model has to figure out which line is a
  constraint vs. a tone preference vs. background.
- One blob — toggle one rule means edit the whole field.
- Per-user only; can't say "for this codebase, never edit
  migrations" without re-typing the full instructions every workspace.

We already have **three places** to influence the prompt:
`PERSONA.md`, `AGENTS.md`, and memory blocks. They're scattered,
format-incompatible, and only memory blocks have first-class CRUD.

Custom instructions consolidate the **behavioural** subset of all
three into one addressable list:

```
> instructions add constraint "Never edit files under migrations/"
Added: i_a8f3 [constraint] Never edit files under migrations/
> instructions add style "Reply in plain prose, no bullet lists"
Added: i_b1c2 [style] Reply in plain prose, no bullet lists
> instructions list
ID      KIND        ENABLED  CONDITION              LABEL/BODY
i_a8f3  constraint  ✓        —                      Never edit files under migrations/
i_b1c2  style       ✓        —                      Reply in plain prose, no bullet lists
> instructions disable i_b1c2  # mute for this turn
> instructions add constraint --mode build "Always run tests after a write"
```

## Hard rules

- **Off by default.** A fresh session has zero instructions. The
  feature is purely additive — existing sessions render exactly as
  they always did.
- **Per-session storage.** Instructions live in
  `session.context["instructions"]` — a list of dicts. Different
  workspaces can have different rules. (Phase 5 adds shared
  libraries.)
- **`applies_to` is a filter, not a guarantee.** When the
  predicate doesn't match, the instruction is **omitted from the
  prompt** — the model never sees it. We don't paste "ignore this if
  X" into the prompt and hope the model obeys.
- **No body shaming.** The bodies render verbatim. We don't reword,
  shorten, or add framing prose around them. The user's words are
  what the model sees (modulo a tight `### Kind: <label>\n<body>`
  header).
- **Stable ids.** Each entry gets a slug-style id (`i_<hex>` by
  default; user-supplied ids accepted via `--id`). Ids survive
  edits — `instructions edit i_a8f3 --body "..."` keeps the id.
- **Bounded length.** Per-instruction `char_limit` defaults to
  2,048; total instructions slot defaults to 8,192 chars. Same
  budget machinery as every other slot.

## Data model

```python
{
  "id": "i_a8f3",                   # stable; user-supplied or auto
  "label": "no-migration-edits",    # optional display name
  "kind": "constraint",             # one of CUSTOM_INSTRUCTION_KINDS
  "body": "Never edit files under migrations/",
  "enabled": True,
  "applies_to": {                   # all keys optional; AND semantics
    "mode": "build",                # "plan" | "build" | None
    "workspace": "C:\\repos\\X",    # exact match against pinned ws
    "tags": ["work"],               # any-of session tags
    "lanes": ["code"],              # any-of resolved router lane
  },
  "priority": 50,                   # within-slot render order; lower first
  "char_limit": 2048,               # 0 = unbounded for this entry
}
```

### Kinds

```python
CUSTOM_INSTRUCTION_KINDS = (
    "constraint",   # hard rules: "never X"
    "style",        # output formatting / tone
    "persona",      # behavioural / role framing
    "knowledge",    # background facts about the user / project
    "override",     # explicit override of a default behaviour
)
```

The model sees `### Constraint: <label>` etc. in the prompt — the
header is the only "framing" we add. Kind ordering in the prompt:
`override → constraint → persona → knowledge → style`. Overrides
land first because they reshape every later rule's interpretation;
style lands last because it's the lowest-stakes layer.

### Why these five kinds

- `constraint` and `style` are the obvious split (don't / do).
- `persona` mirrors the existing PERSONA.md concept but is
  addressable.
- `knowledge` covers "the user is on Windows", "the project uses
  Postgres" — facts the model should know but aren't behavioural.
- `override` is a meta-kind for users who explicitly want to
  countermand the default control message ("ignore the 'don't
  claim to have inspected unseen files' rule for THIS workspace
  because we trust the cache").

We deliberately stopped at five. More kinds = more decisions for
the user.

## Surface

### `instructions list`

```
> instructions list
ID      KIND        ENABLED  CONDITION                LABEL/BODY
i_a8f3  constraint  ✓        —                        Never edit files under migrations/
i_b1c2  style       ✗        —                        Reply in plain prose, no bullet lists
i_c2d3  knowledge   ✓        ws=C:\repos\frontend     React 19 + Vite; no class components
i_d4e5  constraint  ✓        mode=build               Always run tests after a write
```

### `instructions add <kind> <body...>`

Positional kind + body. Optional flags:

- `--label <slug>` — display name; defaults to first 40 chars of body
- `--id <slug>` — explicit id; defaults to `i_<hex>`
- `--mode plan|build` — apply only in this mode
- `--workspace <path>` — apply only when this workspace is pinned
- `--tags <a,b,c>` — apply when ANY listed tag is on the session
- `--lanes <a,b,c>` — apply when resolved router lane is in this set
- `--priority <n>` — within-slot render order (default 50)
- `--char-limit <n>` — per-instruction body cap (default 2048)

```
> instructions add constraint --mode build --label tests "Always run tests after a write"
Added: i_e7f8 [constraint] tests
```

### `instructions enable <id>` / `instructions disable <id>`

Toggle without losing the body. The disabled entry stays in `list`
output with `✗`.

### `instructions remove <id>`

Drop the entry entirely.

### `instructions show <id>`

Full view — body, all `applies_to` keys, char_limit, priority.

### `instructions clear`

Drop all entries (asks for confirmation in interactive surfaces;
blunt in dispatcher).

## Slot integration

New `custom_instructions_slot(...)` in `prompt_assembly.py`. Takes
the full instruction list plus the runtime context (mode,
workspace_path, tags, lane, compact). Filters + sorts + renders one
slot:

```
### Override: <label>
<body>

### Constraint: <label>
<body>

### Persona: <label>
<body>

...
```

Slot priority: **3** — between `mode` (0) and `persona` (5). Custom
instructions are an explicit override of *what we send*; they
should arrive before the file-derived persona/AGENTS context.

Default char budget: 8,192 (added to `SLOT_BUDGET_DEFAULTS`). Per-
session override via existing `prompt set-budget custom_instructions
<n>`.

`compact_format=True` (Phase 1 of compact-prompt.md): drops the
`### Kind:` headers in favour of `K:<label>` two-letter prefixes
(`C:`, `S:`, `P:`, `K:`, `O:`). Each rule renders as one block
separated by blank lines. The body still renders verbatim.

## Filter semantics

For each instruction, we check the four `applies_to` keys against
the runtime context. **AND across keys, ANY across list-valued
keys.**

| `applies_to` key | Match when |
|------------------|-----------|
| `mode`            | `mode == context_mode` |
| `workspace`       | `workspace == context_workspace_path` (exact) |
| `tags`            | `set(tags) & set(context_tags)` is non-empty |
| `lanes`           | `resolved_lane in lanes` |
| (omitted)         | always matches |

Examples:

- `applies_to: {}` (or omitted) → applies everywhere
- `applies_to: {"mode": "build"}` → only Build mode
- `applies_to: {"workspace": "C:\\X", "tags": ["work"]}` → only when
  pinned to `C:\X` AND any of the session tags includes `work`
- `applies_to: {"lanes": ["code", "research"]}` → fires when the
  router resolved this turn into `code` OR `research`

The slot producer is pure — it doesn't read `local_memory` itself.
The caller (build_runtime_assembly) gathers all four runtime values
and passes them in. This keeps testing simple.

## Implementation phases

### Phase 1 ✅ shipped (this doc)

Files touched:

- `app/memory/local_memory.py`:
  - `_default_context()` adds `"instructions": []`.
  - `_normalize_context` carries it through with
    `_normalize_instructions(...)`.
  - `_normalize_instruction(entry)` validates one entry (kind,
    body, applies_to keys, char_limit clamp 64–MAX, priority clamp
    0–100, id auto-mint).
  - Module-level helpers: `get_instructions(session_id)`,
    `add_instruction(...)`, `update_instruction(...)`,
    `remove_instruction(id)`, `set_instruction_enabled(id, bool)`,
    `clear_instructions()`.
- `app/services/prompt_assembly.py`:
  - `custom_instructions_slot(instructions, *, mode, workspace_path,
    tags, lane, compact)` slot producer.
  - `_filter_applicable_instructions(instructions, *, mode, ...)`
    helper.
  - `SLOT_BUDGET_DEFAULTS["custom_instructions"] = 8_192`.
- `app/services/chat_service.py`:
  - `build_runtime_assembly` reads `context.get("instructions")`
    and threads mode/workspace/tags/lane through to the slot.
  - Resolved router lane (from `route_info["lane"]`) is plumbed
    into the slot too.

### Phase 2 — Dispatcher commands ✅ shipped (this doc, minimal subset)

Shipped subcommands:

- `instructions list`
- `instructions add <kind> <body...>` with `--label / --id /
  --mode / --workspace / --tags / --lanes / --priority`
- `instructions show <id>`
- `instructions enable <id>` / `instructions disable <id>`
- `instructions remove <id>`
- `instructions clear`

Argument parser is hand-rolled (no `argparse`) — keeps the
dispatcher's pattern consistent with the rest of the file.

### Phase 2.5 ✅ shipped (this doc)

Three pieces that were missing from the minimal Phase 2 CRUD:

**`instructions edit <id> [flags]`** — edit-in-place. Preserves
the id; only the fields you pass via flags change. Recognised
flags:

- `--body NEW BODY` — consumes everything up to the next flag
- `--label X` / `--kind K` / `--priority N` / `--char-limit N`
- `--mode plan|build` / `--no-mode` — set or clear that one
  `applies_to` key
- `--workspace PATH` / `--no-workspace`
- `--tags a,b,c` / `--no-tags`
- `--lanes a,b,c` / `--no-lanes`

Critical: `applies_to` keys are **deep-merged** against the
existing entry. `--mode build` on an entry that already has
`tags: [work]` keeps the tags intact. `--no-mode` clears only
that key. This is what users expect from "edit" (vs the
`update_instruction` helper's whole-dict-replace semantics).

**`instructions hush <id> [--turns N]`** — temporarily mute an
entry for N effective chat turns without flipping `enabled`.
Default `--turns 1`; `--turns 0` clears any existing hush.
Stored as `hushed_remaining: int` on the instruction itself.

The filter (`_filter_applicable_instructions`) treats
`hushed_remaining > 0` as if `enabled` were False — the entry
stays in the list with its `enabled` flag intact, but the model
doesn't see it. `handle_chat` calls
`tick_hushed_instructions(session_id)` after the assistant
responds (success OR failure — a failed turn still saw the
filter, so the hush did its job), decrementing every positive
counter by 1. Once it hits 0, the entry re-arms on the next
turn.

Why turn-based and not timestamp-based: chat turns are the
natural unit — the user wants "skip the next 3 turns," not
"skip for 3 minutes." Wall-clock muting is a different feature
we're not pursuing.

**`instructions import-persona`** — bulk import from
PERSONA.md. Walks `# Heading` and `## Heading` sections; the
heading text becomes the entry's `label`, the body content
becomes the `body`, and a heuristic maps the heading to one of
the five kinds:

| Heading word(s) | Mapped kind |
|-----------------|-------------|
| `tone` / `style` / `voice` / `format` / `formatting` | `style` |
| `constraint(s)` / `rules` / `don't` / `do not` | `constraint` |
| `background` / `about` / `knowledge` / `facts` / `context` | `knowledge` |
| `persona` / `role` / `identity` | `persona` |
| `override(s)` | `override` |
| (anything else) | `persona` (default — PERSONA.md is itself a persona document) |

The mapping looks first at the full heading (lower-cased,
colon-stripped) then falls through to per-word matches. Empty-
body sections are dropped. The result is one new instruction
per non-empty section, with the original PERSONA.md left in
place (use it for the file-derived `persona` slot or delete it
manually once the import is in). Refuses with `no_workspace`
when no workspace is pinned, and `unknown_target` when
PERSONA.md is missing or has no headings.

Phase 2.5 test count: 32 new tests in
`tests/test_custom_instructions.py` (88 total in the file):

- `HushTests` (9): default 0, hush sets remaining, hush 0
  clears, missing id returns None, filter treats hushed as
  disabled, tick decrements only positive values, tick no-op
  when none hushed, tick clamps at 0, **end-to-end N-turn
  hush returns after exactly N ticks**.
- `EditDispatcherTests` (8): body, kind, priority, applies_to
  merge, no-mode clear, unknown id, no-changes invalid,
  multi-word body terminates at next flag.
- `HushDispatcherTests` (6): default 1 turn, explicit turns,
  zero clears, unknown id, negative rejected, non-int
  rejected.
- `ImportPersonaTests` (6): kind-from-heading mapping,
  fallback, parse extracts sections, skips empty bodies,
  no-workspace error, end-to-end importer creates one entry
  per section, missing-PERSONA returns unknown_target.
- `HushIntegrationTests` (2): hushed entry excluded from
  `build_runtime_assembly`, returns after the right number
  of ticks.

### Phase 3a ✅ shipped (this doc)

`prompt show` now renders a per-instruction explanation section
after the slot trace, so users can see at a glance why a rule
did or didn't fire this turn:

```
Prompt assembly (next turn):
  control                 prio=0    chars=376
  mode                    prio=0    chars=287
  custom_instructions     prio=3    chars=72/8192
  ...

Custom instructions (1 applied, 2 filtered, 1 hushed, 0 disabled):
  i_a8f3      [constraint] no-mig                   APPLIED
  i_b1c2      [style     ] tone                     FILTERED  (applies_to.mode='build' but session mode='plan')
  i_c2d3      [knowledge ] stack                    FILTERED  (applies_to.workspace=C:\\repos\\X not pinned)
  i_d4e5      [constraint] tests                    HUSHED    (hushed for 2 more turns)
```

Implementation: a new pure helper
``prompt_assembly.explain_instructions(instructions, *, mode,
workspace_path, tags, lane)`` returns one dict per entry with
``status`` ∈ ``{"applied", "disabled", "hushed", "filtered"}``
plus a short ``reason`` (empty for ``applied``).

Precedence when multiple disqualifications could apply:
``disabled`` > ``hushed`` > ``filtered``. So a disabled rule
that *would also* be mode-filtered shows as ``DISABLED``, not
``FILTERED``.

The dispatcher's ``result["result"]`` shape evolved from a flat
trace list to ``{"trace": [...], "instructions": [...]}``.
Existing tests only consume ``result["output"]`` (verified via
grep before changing); no callers needed updates.

Lane filtering note: ``prompt show`` runs without actually
routing the message, so the resolved lane is always None and
lane-filtered entries always appear as FILTERED with reason
``resolved lane=(unknown)``. To see lane filtering in action,
trigger an actual chat turn and inspect the next ``prompt
show`` (after the router has run, the entry's status reflects
the real lane). A future Phase 3b could route a synthetic
message for preview purposes.

21 new tests in `tests/test_custom_instructions.py` (109
total): 14 `ExplainInstructionsTests` + 7
`PromptShowInstructionsSectionTests`.

### Phase 3b — Qt panel (planned)

- List of cards with toggle, edit, condition badge.
- Status indicator per card (applied / hushed / filtered with
  the same reason text).
- Drag-to-reorder for priority.

### Phase 4 — Promotion + bulk ops (planned)

- `instructions import-persona` parses PERSONA.md headers into
  typed sections (`# Tone` → `style`, `# Constraints` →
  `constraint`, etc.).
- `instructions import-rules` reads `agent-rules.md` rule
  definitions whose `then` is purely behavioural (no
  `forbid_provider`, no dispatcher predicate) and converts them to
  instructions.
- `instructions snapshot <name>` / `instructions restore <name>`
  for sharing across workspaces.

### Phase 5 — Linked libraries (planned)

The "DLL pattern" sketched out in conversation (declined for now —
literal DLLs are a no-go; the pattern is interesting). Shared,
versioned libraries of pre-built instruction sections. Sessions
`instructions link <name>` to attach them; the slot producer
resolves linked sections at assemble time. See
`compact-prompt.md` discussion for the full sketch.

## Test plan (Phase 1 + 2 minimal)

`tests/test_custom_instructions.py`:

- **Storage round-trip** (5+): default empty list, add returns id,
  update preserves id, remove returns bool, set_enabled flips.
- **Normalisation** (6+): unknown kind rejected, blank body
  rejected, too-long body truncated to char_limit, malformed
  applies_to dropped, out-of-range priority clamped, duplicate
  ids deduped on add.
- **Filter semantics** (8+): empty applies_to matches all,
  mode-only filter, workspace-only filter, tags any-of, lanes
  any-of, AND across keys, disabled entry skipped, all-applicable
  → all rendered.
- **Slot rendering** (4+): one entry verbose, one entry compact,
  multi-entry kind ordering, char_budget truncation.
- **End-to-end via build_runtime_assembly** (4+): instructions
  reach the assembled prompt, mode-filter at runtime drops the
  wrong-mode entry, lane-filter respects router result, slot
  absent when list is empty.
- **Dispatcher commands** (8+): list empty, add minimal, add with
  flags, show, disable, enable, remove, clear, unknown subcommand.

Target: 35-45 tests, all in isolation.

## Honest gaps

- **No edit-in-place subcommand yet.** Phase 2.5 adds
  `instructions edit`. Today the user does `remove` + `add` to
  change a body, which is honest but ugly.
- **No conflict detection.** Two instructions can directly
  contradict each other (`always use bullets` + `never use
  bullets`). We render both; the model resolves. We could detect
  semantic conflicts via embedding similarity but that's a Phase
  6+ topic.
- **applies_to is exact-match for workspace.** Phase 4 might add
  glob/regex support (`workspace: C:\\repos\\*`). For now it's
  literal string equality.
- **No per-instruction last-applied telemetry.** The trace shows
  the slot's chars; it doesn't say which entries fired this turn.
  Phase 3 adds that to `prompt show`.
- **The `lane` filter is reactive, not proactive.** We pass the
  *resolved* lane to the slot; we don't bias routing based on
  instructions. (You can still bias-by-skill via Phase 1 of
  skills.)

## Cross-references

- `prompt-assembly.md` — the layered slot system this builds on.
- `compact-prompt.md` Phase 1 — the dense-renderer flag this
  honours via the `compact` kwarg.
- `notes-buffer.md` Phase 2 — memory blocks (Letta-inspired). Same
  storage pattern (labelled, length-bounded, separately editable);
  different concept (memory is what we know, instructions are
  how to behave).
- `agent-rules.md` — the YAML rule layer. Phase 4 can promote pure
  behavioural rules into instructions automatically.
