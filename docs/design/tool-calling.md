# Design: Tool Calling

**Status: Phase 1 + Phase 2 + Phase 3a + Phase 3b shipped · 2026-05-07**

The model can already propose file writes via ` ```python file=path ` ` `
fenced blocks (workspace-writes Phase 2). Tool calling generalises
that pattern: the model emits ` ```dispatcher ` blocks whose body
is any dispatcher command, and the user can review + run them.

This is **net-new design**, not a phase increment. None of the
surveyed projects ship tool calling in this exact shape — most
production agents use OpenAI's JSON-schema function-calling
protocol. We deliberately use the simpler fenced-block approach
because it composes with everything we already have:

* The same parser shape as workspace-writes.
* The dispatcher is already the universal entry point — every
  command goes through it.
* No JSON schema enforcement to implement; the dispatcher's
  existing argument parsing handles malformed commands.

## Why

Three concrete moments this enables:

1. *"The model just observed a pattern in my edits — let it
   propose updating my notes."* Today the proactive lane already
   does this via `{"command": "..."}` JSON. Tool calling makes
   the same surface available on every chat turn, not just the
   proactive timer.
2. *"The model should be able to chain dispatcher commands as
   part of answering."* "Let me check the docs first" → emit
   ` ```dispatcher\nrecall auth flow\n``` ` → user runs it →
   model's next turn gets the recall results back.
3. *"Audit which model response triggered which dispatcher
   action."* Today `dispatch_input` blurs user-typed and
   model-suggested actions; `tool run <n>` decorates the
   execution record so the audit trail shows provenance.

## Hard rules

- **No auto-execution in Phase 1.** The user explicitly invokes
  `tool run <n>` to dispatch a parsed call. This keeps the
  blast radius zero until we have a working approval flow
  (Phase 3 routes through the patch taxonomy).
- **Dispatcher is the universal target.** Tool calls go through
  `dispatch_input`, which means every existing safety net
  (workspace-write approval, mode gates, rule predicates, write
  cap) still applies. A model that emits
  ` ```dispatcher\nwrite secrets.py ... ``` ` still hits the
  Build-mode + approval gate.
- **Same fence shape as writes.** The fenced-block scaffolding
  (` ``` <info>\n<body>\n``` `) matches `parse_fenced_writes`.
  The two parsers operate on the same response text without
  conflicting — writes use `file=` info attr, tool calls use
  `dispatcher` info line.
- **1-based indexing in the dispatcher surface.** When the
  model emits 3 tool calls, the user runs them as
  `tool run 1`, `tool run 2`, `tool run 3` — matching the
  display order. Index 0 is unused to avoid off-by-one bugs.

## Surface

### `dispatcher` fenced blocks

The model emits:

````
Here's what I think you should do:

```dispatcher
note set working_memory User pattern: edits auth.py 5x in 20min
```

And then:

```dispatcher
mode plan
```
````

The parser pulls these out and the user sees them as a
numbered list:

```
> tool show
Tool calls (2):
  [1] note set working_memory User pattern: edits auth.py 5x in 20min
  [2] mode plan

Run with `tool run <n>` to execute.
```

### Dispatcher commands

| Command | Purpose |
|---------|---------|
| `tool show` (or `tool`) | Parse most recent assistant turn; print numbered list |
| `tool run <n>` | Execute the Nth tool call from the most recent assistant turn |
| `tool parse <text>` | Debug: scan arbitrary text for `dispatcher` blocks |

### Recursive dispatch

`tool run <n>` calls `dispatch_input(<command>, session_id=...)`
internally. The result dict carries:

* `origin: "tool_run"` — distinguishes from user-typed input
* `tool_call_index: <n>` — which call in the assistant's list
* `tool_call_command: <text>` — the literal command that ran

These fields let the audit trail and the UI surface "this
action was suggested by the model and accepted by the user"
without conflating it with direct user input.

## Implementation phases

### Phase 1 ✅ shipped (this doc)

- `app/services/tool_calls.py` — parser + dataclass + renderer.
- `parse_from_last_assistant(session_id)` — convenience that
  walks back through history.
- Dispatcher: `tool show / run / parse` subcommands.
- 28 tests in `tests/test_tool_calls.py`.

### Phase 2 ✅ shipped (2026-05-06)

- New `tool_calls_slot` in `prompt_assembly.py` (priority 8 —
  between mode=0 and skills=10). Returns `None` when
  `session.context["tool_calls_enabled"]` is False (the
  default); returns a system message with the
  `TOOL_CALLS_TEACHING_MESSAGE` content when True.
- New `is_enabled` / `set_enabled` helpers in
  `app/services/tool_calls.py`.
- New `tool_calls_enabled` field on default session context
  (default False), preserved through `_normalize_context`.
- New dispatcher subcommand `tool enable [on|off]` for the
  user-facing toggle. Reports current state when called with no
  arg; accepts truthy/falsy synonym sets matching the existing
  `proactive autofire` / `classifier autoroute` patterns.
- `chat_service.build_runtime_assembly` adds the slot to the
  assembly when the flag is set, between `mode` and `persona`.
- `SLOT_BUDGET_DEFAULTS["tool_calls"] = 2_000` — the teaching
  message is ~1 KB, leaving headroom for future expansion.
- 16 new tests in `tests/test_tool_calls.py`: flag default off,
  set/get round-trip, **storage round-trip via
  `_normalize_context`**, slot producer (off → None, on →
  PromptSlot with priority 8), priority sits between mode and
  skills, default budget pinned, **end-to-end via
  `build_runtime_assembly` asserts slot appears iff flag is
  set**, dispatcher status / on / off / truthy synonyms / falsy
  synonyms / unknown flag.

### Phase 3a ✅ shipped (2026-05-07)

- New `kind="tool_call"` recognised by
  `_normalize_pending_approval` in `app/memory/local_memory.py`.
  Required field: `command: str` (the dispatcher input that runs
  on approve). Optional `tier`, `original_message`, `reason`.
  No workspace_path required (tool calls can be workspace-less,
  e.g. `mode plan`, `status`).
- `_maybe_chat_write_proposal` in `chat_service.py` extended to
  also detect `​```dispatcher` blocks via
  `_maybe_tool_call_proposal` (gated on
  `session.context["tool_calls_enabled"]`). Returns
  `{kind: "tool_call", command: <first call's body>}`. **Writes
  win when both kinds are present** in one response — the tool
  call remains discoverable via `tool show` for manual run.
- Approval handler in `command_dispatcher.py` recognises
  `kind="tool_call"`: recursively calls `dispatch_input(command)`
  and decorates the result with `origin: "chat_tool_call"` +
  `tool_call_command: <text>` for audit-trail provenance.
- Banner rendering: `describe_pending_action` shows
  `tool call: <command (truncated)>`;
  `impact_statement_for` describes the recursive dispatch and
  reminds the user that per-command safety nets still apply
  (mode gate, workspace approval, write cap).
- Default tier: L2 (review-and-confirm). User reads the command
  before approving; the recursive dispatch then enforces
  per-command safety.
- 16 new tests: normalisation (5), chat-flow detection (5),
  approve/deny end-to-end (2), banner rendering (4).

### Phase 3b ✅ shipped (2026-05-07)

- `_normalize_pending_approval` bundle branch now accepts
  `tool_call` sub-actions in addition to `write` / `delete`.
  Workspace-less bundles allowed iff every sub-action is a
  tool call (`workspace_path` becomes optional in that case);
  mixed bundles (write + tool_call) still require a
  workspace_path for the write half.
- `bundle_from_action_dicts` (`patches.py`) accepts mixed
  sub-action kinds. Refuses unknown kinds; relaxes the
  "shared workspace" rule for tool-call-only bundles.
- `_maybe_tool_call_proposal` (chat-flow detector): N>1
  `dispatcher` blocks now emit a `kind="bundle"` whose
  `sub_actions` are individual `tool_call` entries. Single
  calls still emit a bare `kind="tool_call"` (Phase 3a
  behaviour for the simple case).
- `_apply_pending_bundle` extended: dispatches based on
  sub-action kind. write/delete go through the existing
  `_apply_pending_workspace_write` path with rollback;
  tool_call goes through `dispatch_input` recursively.
  - **Fail-stop semantics for tool calls.** If a tool_call
    sub-action fails (or any sub-action fails after a
    successful tool_call), already-dispatched tool calls
    are NOT undone — there's no rollback machinery for
    arbitrary dispatcher commands. The output makes this
    explicit: `WARNING: N tool call(s) already ran and
    cannot be auto-reverted.`
  - **Selective rollback.** Writes/deletes from the same
    bundle ARE still rolled back on any failure (their
    backups exist). The user gets `Rolled back R/W file
    changes` plus the tool-call warning.
  - Pre-flight: Build mode is required iff the bundle
    contains write/delete sub-actions; tool-call-only
    bundles work in Plan mode (matching the bare
    `kind="tool_call"` behaviour from Phase 3a).
  - Per-bundle write_cap check counts only write/delete
    sub-actions.
- `describe_bundle_summary` renders heterogeneous bundles
  with both file-path and tool-call sections.

14 new tests in `tests/test_tool_calls.py`:
- `ToolCallBundleNormalisationTests` (4): tool-call-only
  bundle no workspace required, mixed bundle requires
  workspace, unknown kind rejected, empty command in sub
  rejects whole bundle.
- `BundleComposerToolCallTests` (4): N tool calls compose,
  unknown kind rejected, summary renders tool-call section,
  mixed-bundle summary shows both.
- `ChatFlowMultiToolCallTests` (2): two calls emit bundle,
  single call still bare tool_call.
- `ApproveToolCallBundleTests` (4): **dispatches each in
  order** (asserts final state reflects last call),
  **tool-call-only bundle does NOT require Build mode**,
  **failure stops remaining** (asserts third call's effect
  doesn't happen), deny clears without dispatching.

### Phase 4 — tool catalog in the prompt (planned)

- Surface a curated list of available dispatcher commands in
  the system prompt (filtered to those that make sense for the
  model to emit — read-only commands like `recall`, `status`,
  `models`; mutation commands gated on Build mode).
- Per-skill tool subsets: a `coding` skill exposes `recall`,
  `retrieve`, write-related commands; a `vision` skill exposes
  image-reading commands.

### Phase 5 — JSON-schema interop (planned, low priority)

- Adapter that converts our fenced-block calls to OpenAI's
  function-calling JSON schema format and vice-versa, so cloud
  models that prefer the JSON form can interop. Low priority
  because most cloud models accept the simpler text format too.

## Honest gaps

- **No streaming-friendly parser.** The fenced-block regex
  needs the full response text. Phase 1 of streaming buffers
  the complete text before parsing; future phases could
  detect block boundaries incrementally.
- **No approval gate in Phase 1.** The user has to type
  `tool run <n>` explicitly, which IS the approval — but it's
  a CLI-style approval, not a banner-style one. Phase 3 routes
  through the patch taxonomy for parity with workspace writes.
- **No protection against pathological model output.** A
  malicious model could emit ` ```dispatcher\nrm -rf /\n``` `
  — but the dispatcher's existing safety nets (workspace
  containment, mode gates, write cap) catch the dangerous
  cases. The user still has to type `tool run <n>` to
  confirm.
- **Indices are scoped to the LAST assistant turn.** If the
  user runs another chat, the previous turn's tool calls
  become unaddressable. This is by design — the alternative
  (global index across turns) makes confusing UX.
- **No way to run all calls at once.** Phase 1 is one-at-a-time
  by design (user reviews each); Phase 3 will add a "run all"
  via the bundle-patch flow.

## Cross-references

- `workspace-writes.md` Phase 2 — the ` ```python file=path ` ` `
  pattern this mirrors.
- `patch-taxonomy.md` — the typed-patch system tool calls
  will route through in Phase 3.
- `prompt-assembly.md` — Phase 2 adds the `tool_calls_slot`
  to teach the model the format.
- `proactive_service.py` — the existing `{"command": "..."}`
  JSON tool-call emission used by the proactive lane;
  conceptually identical to Phase 1's parser but on a
  different channel.
