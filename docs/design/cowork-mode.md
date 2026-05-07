# Design: CoWork Mode

**Status: Phase 1 + 3 + 4 + 5 shipped · 2026-05-04**

- Phase 1: manual cowork (plan → approve → execute → synthesize)
- Phase 3: parallelism (ThreadPoolExecutor with `max_workers` default 4,
  hard cap 8; per-builder locks serialise sub-tasks targeting the same
  builder); chat-flow file delivery via workspace-writes Phase 2
- Phase 4: Qt UI live progress widget — `CoWorkView` dialog renders
  the active run + sub-task table with status icons + synthesis preview
  + past-runs list, refreshing on every `KIND_COWORK` bus event
- Phase 5: sandcastle-inspired branch strategy (`head` / `merge-to-head`
  / `branch`) + sub-task lifecycle hooks (`before` / `after`)

101 tests across `tests/test_cowork.py` (52, including parallelism +
hooks + branch strategy), `tests/test_cowork_view.py` (11), and
`tests/test_cowork_git.py` (19, all subprocess-mocked).

**Adapted from "CoWork Mode".**

A higher-tier orchestration mode that takes a user goal, decomposes it
into a multi-step plan, runs the plan against linked builders with
file-deliverable outputs, and presents a polished result. Composes the
shipped planner/builder + `goals.py` durable verbs into a one-command
experience.

## What it adds on top of what's shipped

We already have the building blocks:

| Existing capability                                        | Module                                          |
|------------------------------------------------------------|-------------------------------------------------|
| Planner ↔ builder coordination                             | `local_memory` `coordination` bucket            |
| `dispatch` / fan-out                                       | `local_memory.add_task`, `dispatch_to_all`      |
| Durable verbs (`do`, `wait inbox`, `wait task`)            | `app/services/goals.py`                         |
| Inbox auto-pump (`inbox auto`)                             | `_handle_inbox_command` + `_run_one_inbox_task` |
| Task lifecycle bus events                                  | `_publish_task_event`                           |
| Approval-gated workspace reads                             | `app/services/workspace_service.py`             |

CoWork mode adds:

1. **Plan step** — a "plan" lane (probably reasoning) decomposes the
   user goal into 2–10 sub-tasks with assigned builder + acceptance
   criterion per sub-task.
2. **Approval of the plan** before any execution. The user sees the
   plan, can edit, accept, or reject.
3. **Execute step** — fan out the approved plan; each builder runs its
   sub-task; partial-failure recovery.
4. **Deliverables** — sub-tasks can produce **file outputs** (not just
   chat responses), written to the workspace through the planned
   approval-gated write surface (`workspace-writes.md`).
5. **Synthesize step** — once all sub-tasks are terminal, a synthesis
   step assembles the final deliverable: a summary + a manifest of
   files written + open follow-ups.

## End-to-end flow

```
user> cowork "set up a python lib skeleton: package, README, pyproject, one passing test"
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│ PLAN (1 turn against the planner lane)                      │
│   sub_1 = "create package layout in src/" -> coder           │
│   sub_2 = "draft README.md" -> writer                        │
│   sub_3 = "write pyproject.toml" -> coder                    │
│   sub_4 = "add tests/test_smoke.py with one passing test" -> coder │
│   sub_5 = "verify pytest passes" -> coder (acceptance: tests pass) │
└─────────────────────────────────────────────────────────────┘
         │
         ▼ (user approves the plan, edits if needed)
┌─────────────────────────────────────────────────────────────┐
│ EXECUTE                                                      │
│   parallel where possible; serial where dependencies exist   │
│   each sub-task is a regular dispatched task                 │
│   file-write proposals queued through workspace approval     │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│ SYNTHESIZE (1 turn against the planner lane)                │
│   summary of what was done                                   │
│   manifest of files written / changed                        │
│   list of failed sub-tasks + reasons                         │
│   suggested follow-ups                                       │
└─────────────────────────────────────────────────────────────┘
         │
         ▼
output appears in transcript + execution log + on disk
```

## Surface

### Dispatcher commands

```
cowork "<goal>"                         # full plan → approve → execute → synthesize
cowork plan "<goal>"                    # plan only; show plan; pause for approval
cowork run <plan-id>                    # execute a previously-planned plan
cowork status                           # current cowork run, if any
cowork cancel                           # abort the current cowork run
```

### Storage

Add `cowork_runs` to the storage payload:

```python
"cowork_runs": [
    {
        "id": "cw_a1b2c3d4",
        "session_id": "default",
        "goal": "set up a python lib skeleton",
        "plan": [
            {"id": "sub_1", "task": "...", "builder": "coder",
             "depends_on": [], "acceptance": "..."},
            ...
        ],
        "status": "planning|approved|executing|synthesizing|done|failed|cancelled",
        "created_at": "...",
        "approved_at": "...",
        "completed_at": "...",
        "task_ids": ["task_xxx", ...],   # links into coordination.tasks
        "deliverables": [
            {"path": "src/foo/__init__.py", "kind": "create",
             "task_id": "task_xxx", "approved_at": "..."},
            ...
        ],
        "synthesis": "...",  # final summary text
    }
]
```

### Plan format

The planner emits structured JSON wrapped in a fenced block. We parse
it; if parsing fails, we ask the planner to retry. Schema:

```json
{
  "goal": "<echoed back>",
  "branch_strategy": "head | merge-to-head | branch",
  "sub_tasks": [
    {
      "id": "sub_1",
      "task": "<one imperative line>",
      "builder": "<builder session id>",
      "depends_on": ["sub_X", ...],
      "before": "<optional dispatcher command run before the sub-task>",
      "after":  "<optional dispatcher command run after the sub-task>",
      "acceptance": "<how the builder knows it's done>"
    }
  ]
}
```

Hard caps: 10 sub-tasks max per plan; one plan per session at a time
(can't cowork a new goal while one is running).

### Branch strategy (planned, inspired by sandcastle)

`branch_strategy` is optional; default is `head` (today's behaviour —
all writes land on the working tree). Two more options for review-
pipeline workflows:

| Strategy           | Behaviour                                                                    |
|--------------------|------------------------------------------------------------------------------|
| `head` (default)   | Sub-task writes apply directly to the workspace working tree, no git involvement |
| `merge-to-head`    | Cowork creates a temp branch; sub-task writes commit there; auto-merges to HEAD if every sub-task succeeds; on failure leaves the branch for the user |
| `branch`           | Plan declares a target branch name; commits land there; user merges by hand  |

Each branch strategy composes with the workspace-writes approval gate
unchanged — every individual write still prompts. The strategy only
controls *where* approved writes land. Useful when:
- Multiple cowork runs need to share a repo without stepping on each other
- The user wants a review checkpoint before merging a polished change
- Cowork itself needs atomicity ("either all 5 sub-tasks land or none")

Implementation depends on `workspace-writes.md` Phase 1.5 (the git-aware
write hook). Defer until that ships.

### Sub-task lifecycle hooks (planned, inspired by sandcastle)

`before` and `after` are optional dispatcher commands that run around
each sub-task. Use cases:
- `before: "do @reviewer audit src/foo for testability"` — gate
  expensive work on a quick precheck
- `after: "do @test-runner pytest -q"` — verification ritual after
  every code change
- `before: "compact"` — trim context before a long sub-task

Hooks honour the same approval gates and reason vocabulary as any other
dispatcher command. A failing `before` hook aborts the sub-task with
`reason: precondition_failed` (added to error-vocabulary at
implementation time).

### Plan approval

After planning, the dispatcher returns the plan with a `pending_action`
of kind `cowork_plan` analogous to today's workspace approval pattern.
The user runs `approve` to start execution or `deny` to cancel.

### Execution

For each sub-task in dependency-respecting order:
- Dispatch as a normal task on the assigned builder
- Wait for terminal status via `goals.wait_for_task`
- If the sub-task produces file deliverables, route them through the
  workspace-writes approval flow (see `workspace-writes.md`)
- Mark the sub-task done / failed; update the cowork run status

Parallelism is allowed: independent sub-tasks (no `depends_on`
relation) can dispatch in parallel using a small thread pool.

### Synthesis

After all sub-tasks are terminal:
- Send a synthesis prompt to the planner lane: "Here's the goal, the
  plan, what each sub-task produced, what failed. Write a polished
  summary."
- The synthesis text gets:
  - Appended to the planner's chat history
  - Stored in the cowork run as `synthesis`
  - Written as `cowork_<id>.md` to the workspace (if workspace pinned
    + writes approved)

## Bus events

New `KIND_COWORK` (or reuse `KIND_DISPATCH`?) for cowork lifecycle:
`planned`, `approved`, `subtask_done`, `subtask_failed`, `completed`,
`cancelled`. Subscribers (LinkedView, future progress widget) render
a cowork progress bar.

## UI surface

A new **CoWork view** in the Qt UI:

```
┌─────────────────────────────────────────────────────────────┐
│ COWORK · cw_a1b2c3d4 · "set up python lib skeleton"          │
├─────────────────────────────────────────────────────────────┤
│ Plan (5 sub-tasks)                                           │
│   ✓ sub_1  coder        create package layout in src/       │
│   ✓ sub_2  writer       draft README.md                     │
│   ▶ sub_3  coder        write pyproject.toml                │
│   ⏸ sub_4  coder        add tests/test_smoke.py (waits 3)   │
│   ⏸ sub_5  coder        verify pytest passes (waits 4)      │
│                                                              │
│ Deliverables                                                 │
│   ✓ src/foo/__init__.py       (created)                      │
│   ✓ src/foo/__main__.py       (created)                      │
│   ✓ README.md                  (created)                      │
│   ⏳ pyproject.toml            (queued)                       │
│                                                              │
│ [Cancel]                                       3/5 done · 60%│
└─────────────────────────────────────────────────────────────┘
```

Same QSS / theme as the rest. Opens via a new "CoWork" quick-action
button next to "Linked".

## Hard rules

- **Plan must be approved.** No execution without explicit approval.
  Inherits the security model from workspace approval.
- **One cowork run per session.** Concurrent cowork runs on the same
  session are rejected — sequence them.
- **Failure isolation.** A failed sub-task doesn't abort the whole
  run unless the plan declares a hard dependency. Synthesis honestly
  reports failures.
- **Cancellation works.** `cowork cancel` aborts in-flight sub-tasks
  via their cancel events; storage is left in a coherent state.
- **No silent network access.** If any sub-task uses a cloud provider
  (per `chat-providers.md`), the user has explicitly bound that lane;
  cowork doesn't add cloud routing on its own.

## Composition with existing pieces

CoWork doesn't introduce new primitives — it composes:

| Step                | Composes                                                                     |
|---------------------|------------------------------------------------------------------------------|
| Plan                | One chat dispatch on the planner lane with structured-output prompting       |
| Approve             | `set_pending_approval` → `approve` (workspace-approval pattern)              |
| Dispatch sub-tasks  | `local_memory.add_task` (per builder)                                        |
| Wait for completion | `goals.wait_for_task` per sub-task; possibly parallel via thread pool        |
| Write deliverables  | Workspace-write approval flow (see `workspace-writes.md`)                    |
| Synthesize          | One chat dispatch on the planner lane with full sub-task results in context  |

Means: most of the work is **prompt design** (good plan format, good
synthesis prompt) plus a small **state machine** that walks the run.

## Implementation phases

### Phase 1 — manual cowork ✅ shipped
- `cowork "<goal>"` and `cowork plan "<goal>"` produce a JSON plan
  (planner is asked once, retried once on parse failure) and queue an
  `approval_required` pending action of `kind: "cowork_plan"`
- `approve` / `deny` flip the run to `approved` / `cancelled` via the
  existing pending-approval gate
- `cowork run <id>` walks the plan in topological order, dispatches
  each sub-task as a regular builder task via `goals.add_task` +
  `goals.dispatch_and_wait_for_existing`, awaits terminal status
- File deliverables work through the shipped workspace-writes flow —
  any sub-task that proposes a write hits the same `approve` gate
- Synthesis runs after every sub-task is terminal (one final chat
  dispatch on the planner; failure produces an honest report)
- `cowork status` lists past + active runs; `cowork cancel` aborts
- Bus events: `KIND_COWORK` with `event` discriminator (`planned` /
  `approved` / `subtask_done` / `subtask_failed` / `completed` /
  `cancelled`)

### Phase 2 — file deliverables
- Depends on `workspace-writes.md` shipping ✅ (now satisfied)
- Sub-tasks can declare files they'll create / modify
- Synthesis includes a file manifest

### Phase 3 — parallelism ✅ shipped
- `execute_run` uses a `concurrent.futures.ThreadPoolExecutor` with
  `max_workers` cap (default `DEFAULT_MAX_WORKERS=4`, hard ceiling
  `MAX_WORKERS_CAP=8`)
- Per-builder `threading.Lock` (created lazily in `builder_locks`) so
  two sub-tasks targeting the same builder serialise — one inbox, one
  in-flight task at a time
- Driver loop submits ready sub-tasks on each iteration (a sub-task is
  "ready" once every entry in its `depends_on` is in the
  `terminal_subs` set), waits FIRST_COMPLETED with a 0.25s timeout so
  cancellation is observed quickly, repeats until everything's terminal
- Failure isolation preserved: a failed/skipped sub-task lands in a
  shared `failed_or_skipped` set; downstream sub-tasks see it under
  `state_lock` and auto-skip with `dependency_failed` rather than
  spawning their builder dispatch
- Cancellation: `cancel_event.set()` empties the pending queue (no new
  sub-tasks submit) and is also passed through to
  `goals.dispatch_and_wait_for_existing` so in-flight workers abort
  promptly; final state is `cancelled` with no synthesis
- Worker exceptions are caught inside `_execute_one` and converted to
  a "failed" sub-task result so a single misbehaving sub-task can't
  derail the run
- Dispatcher flag: `cowork run <id> --workers N` overrides the default

### Phase 4 — UI ✅ shipped
- `app/ui/cowork_view.py` — `CoWorkView` QDialog with:
  - Session combo (top) + status banner showing `<status> · N/M done`
  - Sub-task table: glyph (✓ done · ▶ in_progress · ✗ failed · — skipped
    · · pending), id, builder, task text + acceptance tooltip
  - Synthesis text pane below the table
  - Past-runs list (newest first); clicking promotes to the displayed
    run. Default falls back to the most recent past run when no active
    run exists, so opening the dialog always shows *something* useful
  - Cancel button enabled only when the displayed run IS the active
    run AND its status is in `{planning, approved, executing,
    synthesizing}`; click dispatches `cowork cancel`
  - Refresh button + 50ms-debounced auto-refresh on every
    `KIND_COWORK` bus event scoped to the current session
- "CoWork" button in the Qt main window's quick-action row, opens the
  dialog focused on the current session
- 11 tests in `tests/test_cowork_view.py` (offscreen Qt; layout +
  bus integration + close-unsubscribes)

### Phase 5 — branch strategy + lifecycle hooks (sandcastle-inspired) ✅ shipped

What landed (`app/services/cowork.py`,
`app/services/cowork_git.py`,
`app/memory/local_memory.py`,
`tests/test_cowork.py` + `tests/test_cowork_git.py`):

**Sub-task lifecycle hooks** — each sub-task may declare optional
`before` and `after` dispatcher commands:

- `before` hook runs through the dispatcher before the main task. A
  non-empty `reason` on the resulting event aborts the sub-task with
  `precondition_failed`; downstream dependents auto-skip with the
  existing `dependency_failed` semantics.
- `after` hook runs after the main task succeeds. A failing `after`
  hook attaches an `after_hook_warning` to the sub-task result but
  does NOT flip the status — the work was already done; the user
  can promote a strict gate to a separate sub-task if needed.

**Branch strategy** — plan declares `branch_strategy` (default
`head`):

- `head` — no git involvement; identical to pre-Phase-5 behavior.
- `branch` — plan also declares `branch_name`; cowork creates +
  checks out the branch off HEAD, runs sub-tasks, leaves the branch
  checked out at the end.
- `merge-to-head` — branch name auto-generated as
  `lai/<session>/<run-id>`; on a successful run cowork checks out
  the original branch and `git merge --no-ff` the run branch back;
  on any sub-task failure leaves the run branch checked out for
  inspection.

`app/services/cowork_git.py` is the single subprocess seam: tests
patch its `_run_git` to inject canned `GitResult` values, so no real
`git` process spawns in CI.

Branch-strategy plumbing is bus-driven — `_setup_branch_strategy`
subscribes to `KIND_WORKSPACE_WRITE` for the session's writes and
auto-commits each one. No coupling to the workspace-writes internals
beyond the bus. `git_state` (`{strategy, original_branch,
target_branch, commits, merge_status}`) is persisted on the run for
audit trail.

Refuses to start a non-`head` strategy when:
- No workspace pinned (`branch strategy needs git`)
- Workspace isn't a git repo
- Workspace has uncommitted changes (would conflict with the
  per-write commits)
- Detached HEAD (no original branch to merge back to)

### Phase 5.5 — typed cancellation tokens ✅ shipped (2026-05-06)

Adopted from Local AI Workbench's
``shared/cancel.py``. New ``app/services/cancel.py`` module ships:

* ``CancelToken(label)`` — wraps a ``threading.Event`` with two
  additions: a ``cancel(reason=...)`` call that captures *why*
  (first cancel wins; subsequent calls noop) and a
  ``raise_if_cancelled()`` that throws ``OperationCancelled``
  carrying the reason + label.
* ``cancellable_sleep(token, seconds)`` — drop-in replacement for
  ``time.sleep`` in cancellable loops; bails out early on cancel.
* ``from_event(event, label=...)`` — wraps an existing
  ``threading.Event`` for incremental migration; both views stay in
  sync.

Wired into ``cowork.py``: every ``execute_run`` registers a
``CancelToken`` alongside the legacy event registry (parallel
``_cancel_tokens`` dict guarded by the same lock). ``cancel_active_run``
now accepts ``reason="..."`` and threads it through the token, so
downstream observers can pattern-match on *why* the run was
cancelled (``user-requested`` / ``dependency_failed`` /
``timeout`` / etc.) rather than re-deriving it from context. The
underlying event is the same one the existing sub-task / synthesis
checks read, so no call sites had to change.

22 tests in ``tests/test_cancel.py``: token state machine,
idempotent re-cancel, ``raise_if_cancelled``, ``wait_with_timeout``,
``cancellable_sleep`` (sync + async cancel + None token),
``from_event`` round-trip, cowork integration (token captures
reason; ``get_cancel_token`` exposes it).

Future: thread tokens into ``goals.py`` durable verbs (do / iterate
/ wait) so they share the same reason-capture surface — currently
those use bare ``threading.Event``.

### Phase 5.6 — typed pipeline reports ✅ shipped (2026-05-06)

Adopted from Local AI Workbench's
``shared/schemas.py`` pattern. New ``app/services/reports.py``
module ships a ``StageReport`` dataclass + a converter that
distills a stored cowork run dict into a typed summary:

```python
@dataclass
class StageReport:
    name: str               # "cowork run cw_xxx — <goal>"
    status: str             # done / failed / cancelled / ...
    started_at: str         # ISO timestamp (run.approved_at)
    finished_at: str        # ISO timestamp (run.completed_at)
    duration_seconds: float
    total: int              # len(plan)
    done: int               # subtask_results entries with status="done"
    failed: int
    skipped: int
    paths: List[str]        # dedup'd from git_state.commits[].path
    errors: List[Dict]      # one entry per non-done sub-task
    summary: str            # synthesis or count-fallback
    extra: Dict[str, Any]   # branch_strategy + git_state + deliverables_count
```

Helpers: ``one_line_summary()`` for footer / log lines,
``format_report(report)`` for multi-line text, ``to_dict()`` /
``from_dict()`` for JSON / MCP serialization.

Dispatcher surface: ``cowork report [<run_id>]`` — explicit id
looks up that run, no-id falls back to the active run, then to
the most recent run for the session. Output is the multi-line
text rendering of the StageReport.

25 tests in ``tests/test_reports.py``: dataclass round-trip,
mixed sub-task outcomes (done / failed / skipped), cancelled run
with no synthesis (count-fallback summary), git_state path dedup,
malformed input tolerance, dispatcher round-trip.

Future: ``template_report_from_run`` (workflow templates) and
``recall_report_from_results`` (search hits) can reuse the same
``StageReport`` shape without changes.

### Phase 6 — pluggable execution backends (planned, Hermes-inspired)

Hermes Agent ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
exposes an `ExecutionBackend` abstraction so the same agent loop can
drive a local shell, Docker, SSH, Daytona, Modal, or a sandbox without
touching the planner. Adapting that pattern lets cowork's `branch`
strategy auto-spin a git worktree (we already create them under
`.claude/worktrees/`) — or even a Docker scratch dir for risky
sub-tasks — without changing planner output or sub-task semantics.

```python
class ExecutionBackend(Protocol):
    def prepare(self, run: dict) -> dict: ...
    def run(self, command: str, *, approve_callback) -> dict: ...
    def cleanup(self) -> None: ...
```

Phase 6 implementations:

- `InProcessBackend` — current behaviour (default). Zero new deps.
- `WorktreeBackend` — auto-creates a `git worktree` rooted at the
  branch declared by `branch_strategy`; tears it down on `cleanup`
  (or leaves it for the user when `merge_status != merged`). Zero
  deps beyond git (already required by Phase 5).
- `DockerBackend` — optional extra (`pip install
  localaiproject[docker]`). Spins a fresh container with the
  workspace bind-mounted; sub-task writes happen inside the
  container. The approval gate stays in front of every write
  regardless of backend.

Bus events get a `backend` field. The CoWork view can show "running
in: worktree" / "running in: container" so the user knows where
their writes will land.

### Phase 7 — sleep-time consolidation hook (planned, Letta-inspired)

A built-in CoWork `after`-hook (Phase 5 lifecycle hooks already
shipped) called `consolidate_memory` runs once a multi-step goal
completes:

1. Reads the run's transcript + current memory blocks (see
   `notes-buffer.md` Phase 2)
2. Dispatches a small reasoning-lane prompt asking for proposed
   delta updates to the memory blocks (NOT rewrites)
3. Surfaces the proposed deltas as a diff in the CoWork live-progress
   widget for **explicit user approval** before commit
4. On approve, applies the deltas via the standard memory-block
   write path (which itself is approval-gated through the workspace
   gate; chained approvals collapse to one accept-all in the UI)

Crucially this is opt-in per template — no background daemon, no
surprise context drift. The latency-tolerance comes from making it
a post-run hook rather than an interactive turn.

Bus event `KIND_COWORK` gains a `consolidation` event subtype with
the proposed deltas as payload.

## Tests

- `tests/test_cowork_plan_parser.py` — plan JSON shape validation,
  malformed plan triggers retry
- `tests/test_cowork_run.py` — fake builders + canned chat responses;
  verify the state machine walks plan → approve → execute → synthesize
- `tests/test_cowork_failure_isolation.py` — one sub-task fails; the
  rest still run; synthesis flags the failure
- `tests/test_cowork_cancel.py` — cancel mid-run; in-flight tasks stop;
  storage is coherent

## Anti-patterns

- **Auto-approve plans.** No. The plan IS the contract; the user must
  see it.
- **Run cowork without a planner lane available.** If the planner lane
  isn't installed, fail fast with a clear `reason: model_unavailable`.
- **Long plans.** 10-task cap is deliberate. Bigger goals should be
  split into multiple cowork runs by the user.
- **Hidden retries.** A failed sub-task is reported; the user decides
  whether to retry. We don't auto-retry on the model's behalf.

## Open questions

- Should the plan step itself be cowork-able (recursive)? Probably
  not for v1 — keeps the state machine sane.
- Do we let the user **edit the plan** before approval? Yes, eventually
  — plan as JSON in the transcript; user edits; `approve` re-parses.
  Phase 4.
- How does cowork interact with **Plan vs Build mode**? Cowork
  requires the session to be in `mode build` (because execution will
  write files). If you're in `mode plan`, `cowork plan` works but
  `cowork run` refuses with `mode_required: build`.
