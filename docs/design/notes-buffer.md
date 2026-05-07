# Design: Per-Session Notes Buffer

**Status: Phase 1 + 2 shipped · 2026-05-04**

- **Phase 1** (shipped): single free-form notes buffer, dispatcher
  `notes` command, chat-flow injection.
- **Phase 2** (shipped): structured memory blocks (Letta-inspired) —
  `notes` becomes the working_memory block; new `note` command
  manages multiple labelled, length-bounded, separately-editable
  blocks (persona / current_goal / user_preferences / working_memory
  + user-defined). Per-block char limits + priority-ordered
  per-block chat-flow injection. Backwards-compat: legacy `notes`
  API operates on the working_memory block; legacy `notes` storage
  field auto-migrates on first read.

47 tests in `tests/test_notes_buffer.py` (18 Phase 1 + 29 Phase 2).
The `note edit → $EDITOR` shell-out and Phase 3 (sleep-time
consolidation hook) remain planned.

A free-form markdown scratchpad that lives in `session.context.notes`.
The agent can read it, append to it, and replace it whenever it wants
its own working memory between turns.

## Why

We have:
- **`session.history`** — chat turns; append-only; the canonical record
- **`session.summary`** — compact memory: facts, preferences, open_tasks;
  structured fields, designed for the model's system prompt
- **`session.context`** — workspace_path, active_skills, mode, etc.;
  one value per key

We don't have:
- A free-form scratchpad. Somewhere the agent can dump its own
  intermediate notes ("plan: do X then Y; questions: Z; risks: …")
  and read them back next turn.

`learnings/` is on-disk recipe knowledge for an entire skill.
`session.summary.facts` is too rigid (one-line items).
`history` is record, not workspace.

The notes buffer fills the gap. rs-sdk does this with per-bot lab logs
in markdown files.

## Surface

### Storage

Add one field to `_default_context()` in `local_memory.py`:

```python
def _default_context():
    return {
        "workspace_path": None,
        "active_skills": [],
        "pending_approval": None,
        "approved_search": None,
        "mode": DEFAULT_MODE,
        "notes": "",        # NEW — markdown string, possibly empty
    }
```

`_normalize_context` validates: must be a string; max length 32 KB.
Anything longer gets truncated with a `\n\n…[truncated]\n` marker.

### Local API

```python
local_memory.get_notes(session_id="default") -> str
local_memory.set_notes(session_id="default", notes: str) -> str
local_memory.append_notes(session_id="default", text: str, *, prefix_timestamp=False) -> str
```

`append_notes` writes `text` to the end with a leading `\n` separator.
With `prefix_timestamp=True` it adds an ISO timestamp header (a
markdown subheading), useful for journals.

All three go through `_mutate(fn)` so they're atomic with other state
changes.

### Dispatcher commands

```
notes                     # show the current notes
notes show                # alias
notes append <text>       # add a line at the end
notes clear               # wipe
notes edit                # open in $EDITOR (planned, lower priority)
```

### Chat-flow integration

When chat runs, the system message stack already includes:
- `CONTROL_SYSTEM_MESSAGE`
- mode message
- skill biases
- workspace info
- approved search snapshot
- compact summary
- mention expansion

Add notes as another optional system message **between** the workspace
info and the compact summary:

```python
if context.get("notes"):
    messages.append({
        "role": "system",
        "content": (
            "Your private notes for this session (you wrote these; "
            "you can update them with `notes append …`):\n\n"
            + context["notes"]
        ),
    })
```

### LinkedView integration

Add a small "NOTES" pane below the planner transcript (collapsible),
showing the buffer with an Append button. Defer to a future round; the
CLI is enough for v1.

## Bus / events

Notes mutations don't need their own event kind — they're rare and
uninteresting to subscribers. They go through `_mutate`, so the
existing `KIND_DISPATCH` event fires when a `notes append` command
runs (because it's a dispatcher command). Subscribers that care about
notes specifically can read `local_memory.get_notes(session_id)` after
any dispatch event.

## Limits

- 32 KB hard cap. Past that, we truncate with a marker. 32 KB is roughly
  6,000 words — plenty for working notes; not so much that it dominates
  the system prompt.
- One notes buffer per session. Multi-buffer ("draft" / "scratch" /
  "decisions") would be over-engineering for v1.
- No history of notes versions. `undo` covers intra-session rollback;
  if someone wants long-term version history they can paste notes into
  a real file.

## Migration

Existing on-disk data has no `notes` field. `_normalize_context` defaults
to `""`. No migration script needed.

## Tests

`tests/test_notes.py`:
- `get_notes` on a fresh session returns `""`
- `set_notes` round-trips
- `append_notes` adds to the end with `\n` separator
- `append_notes(prefix_timestamp=True)` adds an ISO date header
- 32 KB cap truncates with the marker
- Dispatcher: `notes`, `notes append <text>`, `notes clear`
- `_build_runtime_messages` includes the notes system message when
  `context["notes"]` is non-empty

## Phase 2 — structured memory blocks ✅ shipped (Letta-inspired)

The shipped surface is a single free-form blob. Letta's
([letta-ai/letta](https://github.com/letta-ai/letta), formerly
MemGPT) memory architecture treats memory as multiple **labelled,
length-bounded, separately-editable blocks** the model (and the
dispatcher) can address by name. Adapting that pattern keeps our
context small + focused while letting different concerns coexist
without competing over one buffer.

### Storage

Replace the single `notes` string with a `memory_blocks` dict:

```python
"memory_blocks": {
    "persona": {
        "description": "How the assistant should present itself",
        "value": "Concise; quote code; cite sources",
        "char_limit": 1000,
    },
    "current_goal": {
        "description": "What we're working on right now",
        "value": "Ship cowork phase 6 (execution backends)",
        "char_limit": 2000,
    },
    "user_preferences": {
        "description": "Stable preferences the user has stated",
        "value": "Always show diffs before applying. Prefer TOML to YAML.",
        "char_limit": 4000,
    },
    "working_memory": {
        "description": "Per-turn scratch — flushed at compact time",
        "value": "Open question: should sleep-time agents block?",
        "char_limit": 2000,
    },
}
```

The existing `notes` field stays as a backwards-compat alias for the
`working_memory` block (or migrates wholesale at upgrade time).

### Dispatcher commands (additions)

```
note set <label> <text>       # replace a block's value
note edit <label>             # open in $EDITOR (Phase 3)
note show [<label>]           # show one block (or all)
note describe <label> <desc>  # update the block description
note budget <label> <bytes>   # raise/lower the char_limit
```

### Chat-flow integration

Each non-empty block is injected as its own system message ordered by
a fixed precedence (`persona` → `user_preferences` → `current_goal` →
`working_memory`). The block's `description` becomes the leading
sentence of the message, so the model sees both the *purpose* and the
*content*.

### Hard rules

- **Edits stay user-initiated.** The model can *propose* edits as
  fenced ` ```memory label=current_goal ` blocks (mirroring
  `workspace-writes.md` Phase 2 chat-flow detection); the user
  approves through the existing gate. No silent self-editing.
- **Per-block char limits** so a runaway block can't dominate the
  prompt.
- **Backwards-compat aliasing**: existing `notes` API still works,
  routed to the `working_memory` block.

### Phase 3 — sleep-time consolidation

Letta's "sleep-time agents" run during idle periods to consolidate
memory blocks. Our local-first interpretation: a built-in CoWork
`after`-hook (Phase 5 lifecycle hooks already shipped) called
`consolidate_memory` that runs once a multi-step goal completes,
asks the planner lane to propose memory-block deltas, and **shows
them as a diff for user approval** before committing. No background
daemon, no surprise context drift.

## Privacy / safety

- The notes are part of `session.context`, so they live in the same
  storage as everything else (JSON or DuckDB).
- They're sent to the model on every chat turn (when non-empty). Don't
  put secrets there. `AGENTS.md` should call this out.

## Open questions

- Should we surface a `notes` resource over MCP so a remote agent can
  subscribe to changes? Probably yes; defer to MCP server work.
- Should `notes edit` shell out to `$EDITOR` from the CLI? Useful but
  out of scope for v1; ship the API + dispatcher first.
- Should the chat flow include notes in the **router** prompt too, not
  just the inference prompt? Probably not — keep router context tight.
