# Design: Compact Prompt

**Status: Phase 1 shipped · 2026-05-07**

Same information, fewer tokens. The model already knows what
`## Persona` means; the verbose Plan-mode paragraph that enumerates
every refusal case is for paranoid debugging, not for the model.

This is **net-new design**, not a phase increment of
`prompt-assembly.md`. Layered prompt assembly stayed the same — what
changed is how each slot **renders** its content when the user
opts in.

## Why

Local Ollama models live or die by the prompt size. A 16k context
window minus 4k of system-message framing = 12k for actual
conversation. Every "Project persona (read from `PERSONA.md` at the
workspace root):\n\n" line is 50+ characters of wallpaper that
contributes nothing the model needs.

Three concrete things this enables:

1. *Longer conversations before compaction.* A typical compact-mode
   assembly is ~30% smaller than the verbose form on a populated
   workspace.
2. *More memory blocks.* Same total budget; less per-slot overhead;
   more room for actual content.
3. *Faster prompt eval on the model's first cache miss.* Tokens
   not sent are tokens not processed.

The trade-off: someone reading the prompt cold (e.g. via
`prompt show`) sees less context. We accept that — this is a
performance opt-in, not the default.

## Hard rules

- **Off by default.** Existing sessions render exactly as they
  always did. The user opts in via `prompt compact on`.
- **No information loss.** The body of every slot — persona text,
  project guidance, memory block values — is preserved verbatim.
  What we strip is the **framing** around it: "Project persona
  (read from `PERSONA.md`...)" prose, multi-sentence Plan/Build
  paragraphs, memory block descriptions.
- **No file fragmentation.** Compact mode is a **rendering**
  decision applied at assembly time. It does not create, manage,
  or persist any new files. Workspace stays exactly as the user
  left it.
- **Copy-paste survives.** File paths, code snippets, and
  citations render with their full text — no prefix factoring,
  no abbreviation. Anything the user might want to copy-and-run
  stays copy-and-runnable.
- **Slot priorities + budgets unchanged.** Compact mode only
  changes the **content** each slot emits. The trace's slot
  names, priorities, char budgets, total budget, and eviction
  order are identical to the verbose path.

## Surface

### `prompt compact [on|off]`

```
> prompt compact
compact_format: off.
Use: prompt compact on|off

> prompt compact on
compact_format set to on.

> prompt compact
compact_format: on.
Use: prompt compact on|off

> prompt compact off
compact_format set to off.
```

Bare `prompt compact` reports the current state. Adding `on`,
`true`, `1`, `yes`, `enable`, or `enabled` enables it; `off`,
`false`, `0`, `no`, `disable`, `disabled` disables it. Any other
value returns `invalid_argument`.

### Slot-level renderers

Each slot producer that supports compact rendering takes an
optional `compact: bool = False` kwarg. Default behaviour
(`compact=False`) is the verbose form — Phase 1 of compact-prompt
**does not change** what an existing call without the kwarg
produces.

| Slot              | Verbose render                                      | Compact render                |
|-------------------|-----------------------------------------------------|-------------------------------|
| `persona`         | `Project persona (read from PERSONA.md...): \n\n<body>` | `## Persona\n<body>`          |
| `project_context` | `Project guidance (read from AGENTS.md...): \n\n<body>` | `## Project\n<body>`          |
| `memory:<label>`  | `Memory block 'label': description\n\n<value>`      | `## label\n<value>`            |
| `mode` (plan)     | 350-char paragraph enumerating refusals             | `Mode: plan (read-only — no file edits or workspace writes).` |
| `mode` (build)    | 200-char paragraph + approval reminder              | `Mode: build (writes need user approval).` |

Slots not in this table (`control`, `tool_calls`, `skills`,
`workspace`, `inspection`, `mentions`, `retrieved_docs`,
`summary`) render identically in both modes — their content
already comes from a structured source, not editable framing.

### `compact_format` session field

Stored in `session.context["compact_format"]: bool`. The default
is `False`. `local_memory._default_context()` includes it on every
fresh session; `_normalize_context` coerces stored values via
`bool(...)`.

## Implementation phases

### Phase 1 ✅ shipped (this doc)

Files touched:

- `app/memory/local_memory.py` — `_default_context()` adds
  `"compact_format": False`; `_normalize_context` carries it
  through.
- `app/services/prompt_assembly.py` — `persona_slot`,
  `project_context_slot`, `memory_block_slots` each grow an
  optional `compact: bool = False` kwarg + dense renderer. Two
  module-level helpers: `is_compact_enabled(session_id)` and
  `set_compact(session_id, enabled)`.
- `app/services/chat_service.py` — `_mode_system_message` grows
  `compact: bool = False`. `build_runtime_assembly` reads
  `compact = bool(context.get("compact_format"))` once and threads
  it into `mode_slot`, `persona_slot`, `project_context_slot`,
  `memory_block_slots`.
- `app/services/command_dispatcher.py` — `_handle_prompt_command`
  grows the `compact` subcommand (no arg → status; `on|off` → set;
  unknown value → `invalid_argument`).
- `tests/test_compact_prompt.py` — 35 tests across 6 suites.

What this nails:

- Strips slot framing for persona / project_context / memory
  blocks (technique 1 from the design discussion: "format what
  you send efficiently").
- Collapses the Plan/Build mode paragraphs to one-liners
  (technique 3: "ditch redundant boilerplate").
- Honours the user's "no file fragmentation" constraint
  (technique 2 was scoped down to render-time only).
- Honours the user's "preserve copy-paste" constraint
  (technique 5 — prefix factoring — is **explicitly skipped**).
- Skips numeric tokenisation tricks (technique 6 — unclear
  value).

35 tests:

- `CompactFlagRoundTripTests` (6): default off, set returns new
  value, persists across reads, isolated per session,
  `_default_context` includes the field, `_normalize_context`
  coerces it.
- `PersonaSlotCompactTests` (5): verbose framing, compact strips
  framing, compact shorter, body preserved, blank input → None.
- `ProjectContextSlotCompactTests` (3): verbose framing, compact
  strips framing, compact shorter.
- `MemoryBlockSlotsCompactTests` (5): default header, compact
  short header, blank values skipped, priority/name preserved,
  compact shorter.
- `ModeSlotCompactTests` (3): default plan paragraph, compact plan
  one-liner, compact build one-liner.
- `CompactBuildRuntimeAssemblyTests` (7): end-to-end with a real
  workspace — compact assembly shorter than default, persona body
  preserved, project body preserved, memory block value
  preserved, framing strings stripped, default keeps framing,
  trace slot names unchanged.
- `PromptCompactDispatcherTests` (6): no-arg status, on persists,
  off persists, unknown value → invalid_argument, no-arg reports
  on after enabling, unknown subcommand mentions compact.

### Phase 2 — extend to remaining slots (planned)

Candidates that still render verbose framing:

- `skills` — the skill-system-message header could collapse from
  "The following skill recipes are active for this session..." to
  `## Skills`.
- `workspace` — the workspace-snapshot header.
- `summary` — the compaction summary's "Past-conversation summary
  (some context may be condensed):" prefix.

Each is its own contained change, gated on the same
`compact_format` flag.

### Phase 3 — token-aware truncation (planned)

Phase 1's per-slot `char_budget` truncates at character
boundaries. With a tokenizer hook (provider-specific), we could
truncate at token boundaries — same budget, more useful body.
Orthogonal to compact mode but composes naturally.

## Honest gaps

- **Character counts, not tokens.** Same gap `prompt-assembly.md`
  has — compact mode reduces characters, which usually reduces
  tokens, but the relationship isn't 1:1 (especially for unusual
  Unicode). We measure characters because every provider
  agrees on those.
- **No diff against legacy size.** We assert "compact < default"
  in tests but don't track an absolute byte savings. On a
  populated workspace with `PERSONA.md` + `AGENTS.md` + 2-3
  memory blocks, observed savings range 25-40% on the system
  messages.
- **Toggling mid-conversation.** Switching `compact_format` while
  a chat is active works — the next `build_runtime_assembly` call
  picks up the new value. But the model has already seen the
  verbose framing in earlier turns; it doesn't "unsee" the prose.
  In practice the model treats both forms identically.
- **No automatic adoption.** The flag is opt-in. We considered
  auto-enabling on small-context-window models (8k or less) but
  decided against silently changing what the model sees — the
  user owns this decision.

## Cross-references

- `prompt-assembly.md` — the layered slot system this builds on.
  Compact mode is a rendering choice; the assembly machinery
  (slot dataclass, eviction, traces, budgets) is unchanged.
- `notes-buffer.md` Phase 2 — memory blocks. Compact mode strips
  the description framing but keeps the value verbatim.
- `agent-rules.md` Phase 1 — the `forbid_provider` rule. Same
  per-session-flag pattern.
