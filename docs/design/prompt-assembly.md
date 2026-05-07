# Design: Layered Prompt Assembly

**Status: Phase 1 + Phase 2 + Phase 3 + Phase 4 shipped · 2026-05-06**

- **Phase 1** (shipped): pure refactor — every contribution to
  `_build_runtime_messages` is now a typed `PromptSlot` registered
  with `PromptAssembly`. The legacy `_build_runtime_messages`
  signature is preserved (returns `list[dict]`); a sibling
  `build_runtime_assembly()` returns `(messages, trace)` and the
  per-slot trace is plumbed through `handle_chat` onto the result
  dict as `result["prompt_assembly"]`. Same outputs as the legacy
  code path; every contribution is now traceable.
- **Phase 2** (shipped): per-slot `char_budget` truncates oversize
  slots in place (e.g. a 20 KB workspace inspection capped at 16 KB);
  `total_budget` (default 24 KB, configurable via
  `session.context.prompt_total_budget`) evicts the lowest-priority
  slots when the assembly would otherwise overflow. Pinned slots
  (priority 0 — `control` and `mode`) are never evicted; if they
  alone exceed the budget, a synthetic `_diagnostics` trace entry
  flags `prompt_budget_exceeded`. Dispatcher commands `prompt show`,
  `prompt limits`, `prompt set-budget <slot> <n>`, `prompt
  clear-budgets`, `prompt set-total <n>` let users inspect + tune.
  A single-line `[prompt: name/N · name/EVICTED · OVER_BUDGET]`
  footer appears under chat output when any slot was truncated /
  evicted. 59 tests in `tests/test_prompt_assembly.py`.
- **Phase 3** (shipped): two new file-backed slot producers
  registered alongside the existing slots — Hermes-inspired:
  - `persona_slot` (priority 5, default budget 8 KB) reads
    `<workspace>/PERSONA.md` (case variants accepted) on every chat
    turn. Conveys voice / tone / behavioural constraints durable
    across this workspace's sessions.
  - `project_context_slot` (priority 7, default budget 12 KB) reads
    `<workspace>/AGENTS.md` (case variants accepted). Conveys what
    the codebase is, conventions, command vocabulary — distinct
    from the control layer's *own* `AGENTS.md` in its source tree.

  Both use a thin reader in `app/services/project_context_service.py`
  (UTF-8 bounded, null-byte binary guard, hard 64 KB cap with a
  `[file truncated]` marker; further truncation per slot
  `char_budget` happens inside the assembler). 14 new
  `tests/test_project_context_service.py` tests + 9 new tests in
  `test_prompt_assembly.py` (slot producers in isolation +
  end-to-end with a workspace fixture).
- **Phase 3.5 — retrieved_docs slot** ✅ shipped (2026-05-06):
  New ``retrieved_docs_slot`` priority 28 (between mentions=25 and
  memory:*=30+) auto-injects top-5 BM25 matches over the pinned
  workspace as a system message with ``[file:<path>]`` citations.
  Conservative gating: requires session_id + workspace_path +
  cached index (user opts in via ``retrieve build``) + query >= 8
  chars + at least one in-vocab term. See
  ``docs/design/recall-memory.md`` Phase 2b. ``build_runtime_assembly``
  takes a new ``session_id`` kwarg; all callers updated. 10
  additional tests.
- **Phase 4** (shipped):
  - **MCP tool** `get_prompt_assembly` — read-only inspection of
    the slot trace + budget config + over-budget warning for a
    session's NEXT chat turn (without dispatching). Optional
    `message` arg simulates how that user text would change the
    assembly (mostly via mention expansion when a workspace is
    pinned).
  - **Qt panel** `PromptAssemblyView` (`app/ui/prompt_assembly_view.py`)
    — non-modal dialog opened from the main window's "Prompt"
    quick-row button. Shows the slot table (name / priority /
    chars / budget / status), a total-budget progress bar that
    tints orange at >90% and red when over-budget, the
    `prompt_budget_exceeded` warning banner, and the same one-line
    footer the chat output renders. Auto-refreshes 50 ms after any
    `KIND_DISPATCH` event for the current session, so the trace
    stays current as the user runs commands.
  - 11 tests in `tests/test_prompt_assembly_view.py` (5 MCP +
    6 Qt widget tests).

A `PromptAssembly` class in `app/services/prompt_assembly.py` that
composes the model's system prompt from explicitly-typed slots —
replacing the previous ad-hoc string concatenation. Gives us a
single inspectable place to reason about prompt order, length
budgeting, and provenance.

Adapted from Hermes Agent's
([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
`prompt_builder` pattern, which assembles the system prompt from
layered files (`SOUL.md` for persona, `MEMORY.md` for state,
`AGENTS.md` for project context). The local-first interpretation:
keep all sources in our existing storage / memory primitives, but
expose composition + budgeting as a first-class abstraction.

## Why

Today `chat_service.handle_chat` builds the runtime messages by
splicing strings together in a fixed order:

1. `CONTROL_SYSTEM_MESSAGE` (the project-level preamble)
2. mode message (Plan vs Build)
3. skill biases
4. workspace info
5. workspace inspection results (if any)
6. mention expansion
7. notes (when non-empty)
8. compact summary
9. chat history
10. current user message

This works but:

- **No length budgeting.** A long `notes` blob can crowd out chat
  history; we have no per-slot limit. The 32 KB notes cap helps but
  doesn't solve the cross-slot-tradeoff problem.
- **No provenance.** When the model says something surprising, the
  user can't tell which slot pushed it that direction. (Was it a
  rule? A note? A skill bias?)
- **No way to disable a slot.** Debugging "why is the model
  ignoring my prompt" means commenting out chat_service code.
- **Adding the next slot** (memory blocks per
  [notes-buffer.md] Phase 2; persona file per Hermes) requires
  surgery in the middle of `_build_runtime_messages` instead of
  registering a new contributor.

## Surface

### `PromptAssembly` class

```python
@dataclass
class PromptSlot:
    name: str                  # "persona" | "memory" | "rules" | "skills" | ...
    content: str
    char_budget: int           # max chars this slot may emit
    priority: int              # eviction order when budget is exceeded
    truncated: bool = False    # set on assembly if content was clipped


class PromptAssembly:
    """One-shot construction of the runtime message list. Each slot is
    contributed by a registered SlotProducer; assembly applies budgets,
    orders by priority, and yields the final message list with a
    per-slot trace."""

    def add(self, slot: PromptSlot) -> None: ...
    def assemble(
        self, *, total_budget: int = 24_000,
    ) -> tuple[list[dict], list[PromptSlot]]: ...
```

Returns `(messages, trace)` — `messages` goes to the runtime client;
`trace` records what each slot contributed (and what got truncated).

### Slot producers (Phase 1)

One producer per existing source. Each is a function
`(session_context, message) -> Optional[PromptSlot]`. A producer
returning None contributes nothing.

```python
SLOT_PRODUCERS = [
    persona_slot,            # priority 0 — never evicted
    mode_slot,               # priority 0 — never evicted
    rules_slot,              # priority 1 — high; refuses must be in scope
    workspace_slot,          # priority 2
    inspection_slot,         # priority 2 — only when approved_workspace_result present
    skill_bias_slot,         # priority 3
    memory_blocks_slot,      # priority 4 — see notes-buffer.md Phase 2
    mentions_slot,           # priority 5
    summary_slot,            # priority 6 — first to evict on overflow
]
```

### Eviction policy

When the sum of slot contents exceeds `total_budget`, evict slots
from highest priority number to lowest until under budget. Within a
single slot, truncate to its `char_budget` and set `truncated=True`.

### Provenance

The `trace` list is attached to the chat result for every turn:

```python
{
    "ok": True,
    "response": "...",
    ...
    "prompt_assembly": [
        {"name": "persona", "chars": 200, "priority": 0, "truncated": false},
        {"name": "memory", "chars": 1200, "priority": 4, "truncated": false},
        {"name": "summary", "chars": 0, "priority": 6, "truncated": true,  # evicted
         "evicted_reason": "over_budget"},
    ],
}
```

The dispatcher's chat output formatter renders a single-line
footer when truncation/eviction happened:

```
[prompt: persona/200 · memory/1200 · summary/EVICTED]
```

Full trace is on the result dict for MCP / programmatic consumers.

### Inspector

New dispatcher command:

```
prompt show                      # show the slot graph for this session right now
prompt show --message "<text>"   # show what THIS message would produce
prompt limits                    # show per-slot char_budget defaults
prompt set-budget <slot> <n>     # tune a slot's char_budget
```

### MCP tool

`get_prompt_assembly(session_id, message?)` — returns the slot
trace, lets external agents inspect what prompt the model would see
without actually dispatching the turn.

## Implementation phases

### Phase 1 — extract slot producers, no behaviour change

- Refactor `_build_runtime_messages` into N slot producers + a
  `PromptAssembly.assemble()` call
- Same default budgets as today (32 KB notes, etc.)
- Same final message order
- `prompt_assembly` trace attached to result
- One regression test: byte-for-byte identical message output for
  a known input

### Phase 2 — eviction + budgeting

- Add `total_budget` cap (default 24 KB)
- Eviction by priority, with the trace recording evictions
- `prompt show` / `prompt limits` / `prompt set-budget` dispatcher
  commands
- Footer in chat output when truncation/eviction happened

### Phase 3 — new sources

- `persona_slot` from `PERSONA.md` at workspace root (Hermes-style)
- `memory_blocks_slot` (depends on `notes-buffer.md` Phase 2)
- `project_context_slot` from auto-detected `AGENTS.md`

### Phase 4 — MCP + UI

- `get_prompt_assembly` MCP tool
- Qt UI panel: slot bar chart + truncation indicators next to the
  current input

## Tests

- `tests/test_prompt_assembly.py` — slot producers in isolation; full
  assembly with various budgets + eviction; trace shape
- Regression: Phase 1 produces byte-identical messages to today

## Honest gaps

- **No model-aware budgeting.** `total_budget` is a character count,
  not a token count. Rough but adequate for v1; revisit when we have
  a tokenizer dep already in play.
- **Producer order is fixed.** No dynamic reordering based on
  context. Rules-based reordering (e.g. "if message mentions a file,
  promote inspection_slot") could come later but adds complexity for
  marginal win.

## Cross-references

- `chat-providers.md` — provider resolver consumes the assembled
  message list; no coupling to slot internals
- `notes-buffer.md` Phase 2 — memory blocks are slot producers
- `agent-rules.md` — rules contribute to `rules_slot`
- `error-vocabulary.md` — adds `prompt_budget_exceeded` (warning, not
  refusal) when total content far exceeds budget even after eviction
