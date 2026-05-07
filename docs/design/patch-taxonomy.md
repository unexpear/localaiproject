# Design: Patch Taxonomy

**Status: Phase 1 + Phase 2 + Phase 3 shipped · 2026-05-06**

- **Phase 1** (shipped): typed ``Patch`` envelope + kind constants
  + ``bundle`` aggregation. Chat-flow detector emits a
  ``kind="bundle"`` pending-approval when N>1 fenced ``file=path``
  blocks are valid; ``approve`` applies them in order with atomic
  rollback on any sub-action failure. 29 tests in
  ``tests/test_patches.py``.
- **Phase 2** (shipped): tier-based friction (L1 / L2 / L3) +
  shell-generated impact statements. New
  ``app/services/impact_statements.py`` module exposes:
  - ``tier_for(action)`` — L1 (read/search), L2 (write/delete/
    bundle/cowork_plan/browser_navigate, default), L3 (explicit
    override, e.g. for ``system_prompt_update``).
  - ``impact_statement_for(action)`` — static, shell-generated
    one-line impact text per kind. The model never describes its
    own patches' consequences; the shell does.
  - ``confirmation_phrase_for(action)`` — for L3 patches only.
    Deterministic 6-character phrase derived from a SHA-256 of the
    patch content; uses Crockford-style base32 minus confusables
    so the phrase is unambiguous to read off the banner.
  - ``confirmation_matches(action, supplied)`` — case-insensitive,
    whitespace-trimmed comparison.

  The approval banner now renders:
  - **L1:** compact (tier badge + description, no Impact line).
  - **L2:** tier badge + Impact line + plain ``approve`` accepted
    (today's default, preserved).
  - **L3:** tier badge + Impact line + the deterministic phrase the
    user must type after ``approve``. Plain ``approve`` against an
    L3 patch is refused with ``reason: approval_required`` and
    leaves the pending action intact for retry.

  Dispatcher routing extended to accept ``approve <phrase>``;
  ``approve`` alone still works for L1/L2. 35 tests in
  ``tests/test_impact_statements.py``.
- **Phase 3** (Phase 3a shipped): proactive ``honest null``
  triggers. New ``app/services/proactive_service.py`` module:
  - **Heuristic**: ``should_fire(state, now)`` refuses unless
    >= 2 user actions in trailing 5 min, >= 90 s since last check,
    >= 5 min since last proposal, and null_streak < 3. Pure
    function; testable without time mocking.
  - **Prompt**: ``PROACTIVE_SYSTEM_PROMPT`` explicitly tells the
    model "the user did NOT summon you; null is the NORMAL
    outcome; only propose when you have specific non-trivial
    value." Adopted verbatim from `personal app`.
  - **Parser**: ``parse_proactive_response`` accepts
    ``{"patch": null, "reason": "..."}`` or
    ``{"patch": {"command": "..."}, "reason": "..."}``. Tolerates
    fenced JSON / surrounding prose; rejects malformed.
  - **Lifecycle**: 3 consecutive null responses idle the trigger
    until the next user-initiated action resets the streak (every
    ``dispatch_input`` calls ``note_user_action`` which clears it).
  - **Storage**: state persisted in ``session.context.proactive``
    so behaviour survives restart.
  - **Dispatcher surface**: ``proactive status / tick / reset /
    prompt``. ``tick`` accepts an injectable ``model_responder``
    (default routes through chat-providers on reasoning lane;
    tests inject a fake to avoid live LLM calls).

  **Phase 3b shipped (2026-05-06)**: dispatch-driven auto-scheduler.
  Every user-initiated ``dispatch_input`` (excluding the meta-state
  ops in ``_PROACTIVE_HOOK_EXCLUDES``) now also calls
  ``proactive_service.maybe_tick_async``. That helper:
  - Returns False immediately when the module-level
    ``AUTOFIRE_ENABLED`` flag is False (the default — tests opt
    in by flipping it).
  - Returns False when ``should_fire(state)`` refuses (heuristic
    unmet).
  - Acquires a non-blocking per-session lock for coalescing: if a
    previous tick is still inside the model call, the second
    autofire is dropped (prevents parallel ticks for the same
    session).
  - Spawns a daemon thread that runs ``tick`` (which calls the
    model + parses the response). The user's ``dispatch_input``
    returns in microseconds — never waits for the model call.

  Toggle at runtime via the ``proactive autofire on/off``
  dispatcher subcommand. 12 additional tests in
  ``tests/test_proactive_service.py`` (default-off behaviour,
  heuristic still gates when on, **dispatch returns in <0.15s
  even with a 0.3s slow responder**, **coalescing prevents
  parallel ticks**, set_autofire return value, dispatcher
  subcommand round-trips).

  46 tests total in ``tests/test_proactive_service.py`` (Phase 3a
  + 3b combined).
- **Phase 4** (planned): MCP ``propose_patch`` tool + Qt UI tier
  badge in the approval banner.

A closed vocabulary of *typed* mutations the model can propose,
each carrying its own schema, validator, sandbox/preview, friction
tier (L1/L2/L3), and undo semantics. Adopted from the user's
`personal app` project (`C:\Users\micha\Desktop\personal app`),
which uses this pattern to keep a self-modifying browser agent
honest: every change the model proposes is one of seven well-known
shapes; each shape has its own approval flow.

The keystone idea: instead of a single "approval-required" gate
for arbitrary writes, every model-proposed mutation is *typed*.
Different types pay different friction (single-tap, diff-and-confirm,
typed-confirmation), invoke different validators, and roll back
through the same matching unmake operation.

## Why

Today our approval surface is a heterogenous mix:

- `pending_approval` (workspace read / search / write / delete)
- `chat-flow ` ` ```python file=path ` ` ` proposals
- Memory-block edits via `note set <label> <text>` (no approval)
- `prompt set-budget <slot> <n>` (no approval)
- Skill toggles (no approval)
- Session-context mutations (mostly no approval)
- Custom commands (no approval)
- Workspace pin / unpin (no approval)

**Problem 1 — friction is uniform.** Either we approval-gate
everything (which makes routine changes painful) or we don't (which
lets dangerous changes slip through). Today we mostly don't, with
one exception (workspace writes). A typed taxonomy lets us calibrate
friction per type.

**Problem 2 — no atomic multi-part approval.** A user request like
"rename foo→bar everywhere and update the docs" produces N independent
write approvals. If the user denies #3, #1 and #2 are already on
disk. There's no rollback group. `personal app` solves this with a
`bundle` patch type — a single approval surface for several
sub-patches that commit/rollback together.

**Problem 3 — undo is per-mutation, not per-intent.** Our
`local_memory.undo()` reverts the last single mutation. If the
model emitted three changes for one user intent, the user has to
hit undo three times.

**Problem 4 — the model's surface is implicit.** "What can the
model edit?" is answered today by reading `chat_service.py` and
the dispatcher's reserved-name set. A typed taxonomy makes the
answer explicit and auditable.

## Surface

### Patch types (Phase 1 vocabulary)

Mapping our existing concepts onto the seven types from `personal
app`. Some types we already do (with different names) — the
taxonomy gives them a uniform skin so callers can pattern-match.

| Type                   | What it changes                              | Maps to (today)                        | Tier    |
|------------------------|----------------------------------------------|----------------------------------------|---------|
| `workspace_write`      | One file written / created / deleted         | `pending_approval kind="write"`        | L2      |
| `workspace_bundle`     | N coordinated workspace_writes               | NEW — chat-flow can emit several       | L2      |
| `memory_block_update`  | One memory block's `value`                   | `note set <label> <text>`              | L1      |
| `config_update`        | One session-context field (mode, skills, …)  | `mode`, `skills`, `prompt set-budget`  | L1      |
| `prompt_update`        | The session's persona / project_context      | NEW — model proposes PERSONA.md edits  | L2      |
| `system_prompt_update` | The control layer's CONTROL_SYSTEM_MESSAGE   | NEW — opt-in only                      | L3      |
| `bundle`               | An atomic group of any of the above          | NEW                                    | inherit |

Each entry carries its own schema (so the proposal is
JSON-validatable) and its own validator (path containment, length
caps, etc.).

### Patch envelope

A typed dataclass that wraps every proposed mutation. The shape
mirrors how `pending_approval` looks today, generalised:

```python
@dataclass
class Patch:
    kind: str                    # one of the 7 types above
    payload: Dict[str, Any]      # type-specific shape
    rationale: str               # 1-line "why"
    tier: str                    # "L1" | "L2" | "L3"
    origin: str                  # "chat" | "dispatcher" | "mcp" | "proactive"
    proposed_at: str             # ISO timestamp
    sub_patches: Optional[List["Patch"]] = None  # populated for `bundle`
```

### Friction tiers (mirrors `personal app`'s L1/L2/L3)

- **L1 — single-tap.** Easily reversible, low blast radius. Apply
  on confirm. Default for `memory_block_update`, `config_update`,
  many `note` / `prompt` commands.
- **L2 — diff-and-confirm.** Show the user a unified diff (or a
  workspace-action description) and a 2-step confirm. Default for
  `workspace_write`, `workspace_bundle`, `prompt_update`. Mirrors
  today's `pending_approval` flow.
- **L3 — typed confirmation.** Show a *shell-generated* impact
  statement (NOT model-generated — see below) and require the user
  to *type* a confirmation phrase before applying. Default for
  `system_prompt_update`. Reserved for changes that affect the
  whole control layer's behaviour.

**Critical pattern — shell, not model, generates L3 impact text.**
The model can soft-pedal consequences ("This is a small change to
how I respond"). The shell knows what each patch *kind* actually
does and can produce a static, honest description from a fixed
template per type. We don't trust the model to describe its own
patches when the patch is dangerous enough for L3.

### Validators (per type)

Per-type validators run *before* the user sees the proposal — a
patch that fails validation is rejected silently with a
canonical reason tag rather than presented for approval.

| Type                   | Validators                                                          |
|------------------------|---------------------------------------------------------------------|
| `workspace_write`      | path containment (no traversal, no symlink), size cap, write_cap    |
| `workspace_bundle`     | every sub-patch validates; sub-patches all target same workspace    |
| `memory_block_update`  | label is known, char_limit not exceeded                             |
| `config_update`        | field in allow-list (mode/skills/prompt_budgets/...)                |
| `prompt_update`        | target file in workspace, content < 64 KB, UTF-8                    |
| `system_prompt_update` | entire CONTROL_SYSTEM_MESSAGE is valid markdown, < 16 KB            |
| `bundle`               | every sub-patch validates; bundle has < 10 sub-patches              |

Validators returning errors emit one of: `path_not_found`,
`outside_workspace`, `binary_file`, `is_directory`,
`symlink_unsupported`, `write_cap_reached`, `template_param_missing`,
or new tags `bundle_too_large`, `unknown_patch_kind`,
`validator_refused`.

### Undo semantics

Each patch type registers an `unmake` operation. For the existing
single-mutation undo this is no change. For `bundle`, undo unwinds
*all* sub-patches in reverse order — atomic rollback.

For `workspace_write` the existing `write_backups` mechanism is the
unmake; nothing changes.

For `system_prompt_update` we keep a one-deep before/after history
on disk so even a process restart can revert.

## Implementation phases

### Phase 1 — Patch envelope + 2 types

- Add the `Patch` dataclass + `validate_patch(patch)` registry in a
  new `app/services/patches.py`.
- Reframe `workspace_write` (today's chat-flow proposal) as a
  `Patch(kind="workspace_write", ...)`. Existing approval flow is
  preserved; we only change the wrapper shape.
- Add `bundle` as a new top-level type. The chat-flow detector
  parses multiple ` ```python file=path ` ` ` blocks; if there are
  more than one, emit one `Patch(kind="bundle", sub_patches=[...])`
  instead of N independent approvals.
- Tests: per-type schema round-trip, bundle atomicity, undo grouping.

### Phase 2 — Tiered approval

- Add `tier` field to existing `pending_approval`; default L2 for
  workspace_write (today's behaviour). Show diff-and-confirm.
- Add L1 fast-path for `memory_block_update` + `config_update`.
- Add L3 typed-confirmation for `system_prompt_update`.
- Shell-generated impact templates per kind in
  `app/services/patches/impact_statements.py`.
- Dispatcher commands: `approve --tier <L1|L2|L3>` for explicit
  override (advanced users); default tier comes from the patch.

### Phase 3 — Proactive triggers ("honest null")

- Adopt the `personal app` "honest null" pattern: the proactive lane
  prompt explicitly says "the user did not summon you; respond
  with `{patch: null, reason: <one line>}` if you have no genuine
  improvement to propose."
- Trigger heuristic: 90 s poll interval, only fires when ≥2
  user actions in the trailing 5 min, ≥5 min cooldown after the
  last proposal.
- Proactive-emitted patches default to L2+ (diff-and-confirm
  minimum) — the model can never auto-apply.
- Rate-limit: if the model returns 3 consecutive `null` responses,
  the trigger goes idle until the next user-initiated action.

### Phase 4 — MCP + UI

- MCP tool `propose_patch(kind, payload, rationale)` — lets external
  agents queue a patch through the same approval gate.
- Qt UI — extends the existing `pending_approval` banner to render
  the patch's tier badge + (for L3) the shell-generated impact
  statement.

## Cross-references

- `workspace-writes.md` — Phase 1 (`workspace_write`) is already
  shipped under that doc; Phase 2 adds `workspace_bundle`; Phase 3
  the L1/L2/L3 retrofit. Both designs evolve together.
- `agent-rules.md` — Phase 3's proactive triggers slot into the
  rule layer (e.g. `forbid: proactive_propose unless mode=plan`).
- `prompt-assembly.md` — `system_prompt_update` mutates the
  `control` slot's content; `prompt_update` mutates `persona`.
- `notes-buffer.md` — `memory_block_update` is the typed wrapper
  around the existing `note set` flow.
- `error-vocabulary.md` — adds `unknown_patch_kind`,
  `validator_refused`, `bundle_too_large`.

## Honest gaps

- **No model-aware tier override.** A user might want every
  workspace_write to be L1 ("trust me, I know what I'm doing"). We
  don't yet have a config to demote tiers per session — adding
  this is straightforward but waited until Phase 2 lands.
- **No auto-tier inference.** The patch's tier is set by the
  emitter (chat_service for chat-flow patches, dispatcher for
  command-flow). A future phase could derive tier from the patch's
  blast radius (size of diff, number of sub-patches), but for now
  it's a fixed mapping per kind.
- **`prompt_update` overlaps with `workspace_write`.** When the user's
  PERSONA.md is at the workspace root, an edit to it is *both* a
  filesystem write and a prompt mutation. We resolve this at
  emit time: if the target path is one of `PERSONA.md` /
  `AGENTS.md` (the project-context conventions from
  `prompt-assembly.md` Phase 3), emit `prompt_update` instead of
  `workspace_write`. The validator checks pass either way.

## What's already shipped that this generalises

- `workspace_write` patches (today: `pending_approval kind="write"`
  + chat-flow proposals + write_backups undo).
- `memory_block_update` semantics (today: `note set <label>`).
- `config_update` semantics (today: `mode`, `skills`, `prompt
  set-budget`, `prompt set-total`).

What's genuinely new under this taxonomy:

- `bundle` — atomic multi-write approval + rollback.
- `prompt_update` — typed PERSONA.md / project_context.md edits.
- `system_prompt_update` — typed CONTROL_SYSTEM_MESSAGE edits with
  L3 friction.
- Tiered friction via `Patch.tier` instead of single approval gate.
- Proactive `null` patches (Phase 3).
