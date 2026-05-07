# Design: Workspace Writes

**Status: Phase 1 + 1.5 + 2 shipped · 2026-05-04**

- Phase 1: `write` / `delete` / `diff` commands, approval flow,
  backups, 20-write soft cap, bus events.
- Phase 1.5: undo integration that restores overwritten/deleted files
  (or removes fresh creates).
- Phase 2: chat-flow detection — when a chat response contains a
  ```` ```<lang> file=<path> ```` fenced block, the chat handler
  proposes a workspace write through the same approval gate. After
  the user approves, the in-progress builder task is auto-marked
  done so cowork sub-tasks can deliver files via chat.

42 tests in `tests/test_workspace_writes.py`. Git-aware writes (the
sandcastle-inspired branch strategy hook) remain planned.

Prerequisite for `cowork-mode.md` to deliver real files. Extends the
existing read/search approval gate to **create / modify / delete**
operations against the pinned workspace.

## Why

Today the workspace gate handles reads:

```
> read app/services/router.py
[approval required] read app/services/router.py
> approve
… (file content rendered into the model's context)
```

We have no equivalent for writes. So a builder that generates code
can show it in chat, but can't actually write it to disk. CoWork mode
needs this; so does any "deliver a file" workflow.

## Hard rules

- **Same trust model as reads.** Every write proposes; the user
  approves before bytes hit disk.
- **Path-traversal blocked.** Reuse `resolve_workspace_item_path`;
  same rules: must be inside the pinned workspace, no `..` escapes.
- **No silent overwrites.** Every write proposal carries old + new
  content (or "create"); the user sees a diff before approving.
- **Atomic per file.** Each file write is one approval. Batched
  writes (cowork case) get one approval per file unless the user opts
  into "approve all in this batch".
- **Undo path.** A write records a backup of the prior content (if
  any) so `undo` reverses it for the duration of the session.

## Surface

### New action kinds

Pending action gets two new `kind` values:

- `kind: "write"` — create or overwrite a file
- `kind: "delete"` — delete a file
- (Existing: `kind: "read"`, `kind: "search"`)

Action shape:

```python
{
  "kind": "write",
  "origin": "chat",              # or "command" for explicit `write` cmd
  "workspace_path": "C:/proj",
  "path": "src/foo/__init__.py", # relative to workspace
  "content": "...",              # full new content
  "create": True,                # vs modify existing
  "previous_content_hash": None, # or sha256 of what we expect to overwrite
  "requested_at": "...",
  "reason": "task task_xxx (cowork sub-task)",
}
```

### Dispatcher commands

```
write <path>                     # interactive: agent supplies content via chat
write <path> <<EOF               # heredoc-style explicit write
…
EOF
delete <path>                    # explicit delete
approve                          # acts on the pending action (any kind)
deny                             # cancels the pending action
diff <path>                      # show pending vs current (for write actions)
```

### Chat-flow integration

The chat service may emit a write proposal when:

1. The user explicitly asked for a file change ("update `router.py` to
   thread the cancel_event")
2. The mode is `build` (Plan mode disables write proposals entirely)
3. The model produced a fenced code block tagged with a path:

   ```` ```python file=app/foo.py ```` ` `
   `<full file content>`
   `` ``` ``

The chat handler parses the fenced block, builds a `kind: "write"`
pending action, returns `approval_required=True`. Same UX as a `read`
approval today.

### Diff display

Before the user approves, they see:

```
> approve
[approval pending] write app/services/router.py (modify, 47 lines changed)

  --- current  (sha 9f8e7d6c…)
  +++ proposed
  @@ -10,3 +10,5 @@
  -def route_message(text):
  +def route_message(text, cancel_event=None):
  +    if cancel_event is not None and cancel_event.is_set():
  +        return None
       ...

Type `approve` to write, `deny` to cancel.
```

Diff is computed via stdlib `difflib.unified_diff`. No third-party
deps.

### Bus events

`KIND_TASK` already covers task lifecycle. Add two granular kinds:

- `KIND_WORKSPACE_WRITE` — published when a write is approved + applied.
  Payload: `{path, action: "create"|"modify"|"delete", bytes}`.
- `KIND_WORKSPACE_DENY` — published when a write proposal is denied.

Subscribers (Qt UI, file-watcher, future audit log) react.

## Storage and undo

### Backup record

Before each approved write, store a backup in the session under
`session.context.write_backups`:

```python
"write_backups": [
    {
        "id": "wb_a1b2c3",
        "path": "src/foo.py",
        "previous_content": "...",   # or null for create
        "previous_existed": True,
        "applied_at": "...",
        "execution_id": "exe_xxx",
    },
    ...
]
```

Cap at last 50 backups per session. Older ones get dropped (with a
log line). 50 × ~10 KB average = 500 KB, well within the storage
budget.

### Undo behaviour

`undo` already rolls back the last memory mutation via the snapshot
deque. Extend it: when the snapshot includes a `write_backups` entry
that wasn't there before, also restore the file contents (or delete
the file if it was a fresh create).

Cross-session caveat: undo is per-process and intra-session; if
another process modified the file between the write and the undo,
the undo skips with a `reason: write_backup_stale` event so the user
knows their file isn't what they expect.

## Rate-limiting / safety net

A "soft" cap on writes per session:

- **Default**: 20 file writes per session before the user must
  explicitly bump the limit
- **Bump**: `workspace writes-bump 100` raises the cap for the current
  session
- **Reset on session switch**

Catches "model went off the rails and tries to rewrite 200 files"
class of bugs. The cap is per-session because cowork runs are typically
≤10 deliverables; a runaway is anomalous.

## Workspace `mode build` requirement

Plan mode (`mode plan`) refuses writes outright:

```
> approve
Cannot apply write: session is in Plan mode (read-only context).
Use `mode build` to enable file writes.
reason: mode_required: build
```

Build mode is required for any write proposal to even be created.

## Permissions on disk

Default file permissions: 0644 on POSIX, default ACL on Windows. We
don't try to be clever about ownership; whoever owns the workspace
owns the new files.

Symlinks are followed for path resolution but **never created**. If
a write proposes a symlink, refuse with `reason: symlink_unsupported`.

## Implementation phases

### Phase 1 — explicit `write` command ✅ shipped
- `write <path> <<EOF\n<content>\nEOF` heredoc syntax (and an inline
  single-line form `write <path> <content>`)
- `delete <path>` symmetrically
- `diff` shows the pending proposal vs current file content
- `workspace writes-bump <n>` raises the per-session soft cap
- Path traversal blocked, symlinks refused, plan mode refuses,
  20-write cap (`DEFAULT_WRITE_CAP`), stale-content detection via
  SHA-256 hash captured at propose-time
- Write backups recorded in `session.context.write_backups` (cap 50,
  rotation), tracking previous content + sha256 for would-be undo
- `KIND_WORKSPACE_WRITE` and `KIND_WORKSPACE_DENY` bus events
- 20 tests in `tests/test_workspace_writes.py`

### Phase 1.5 — undo integration ✅ shipped

What landed (`app/services/command_dispatcher.py`,
`app/memory/local_memory.py`,
`tests/test_workspace_writes.py`):

- `_undo_last_workspace_write(session_id)` — the actual rollback. For a
  recorded backup it:
  - **`write` + `previous_existed=False` (create):** deletes the file.
    If it's already gone (someone else nuked it first), reports
    "(already gone)" and still pops the backup — idempotent.
  - **`write` + `previous_existed=True` (modify):** restores
    `previous_content` via `apply_workspace_write`. If the file was
    since deleted, the write recreates it (which is what "undo this
    modify" should mean).
  - **`delete` + `previous_existed=True`:** restores the file via
    `apply_workspace_write`.
- `_handle_undo_command(session_id)` — the dispatcher entry point.
  Tries the workspace-write rollback first; falls through to today's
  `local_memory.undo()` if no `write_backups` entries exist. Memory
  undo behaviour is unchanged.
- `write undo` / `/write undo` — alias of `undo` for clarity when the
  user is mid-write-flow. Same handler.
- `local_memory.decrement_write_count(session_id)` — keeps the soft
  cap honest after a rollback so the user can keep working.
- `KIND_WORKSPACE_WRITE` event with `event: "undone"` and
  `action: "undo_create" | "undo_modify" | "undo_delete"` so
  subscribers (Qt UI, audit log) can render the rollback.
- Refusal modes: if the workspace pin was cleared, returns
  `reason: no_workspace`; the backup is preserved so re-pinning lets
  the user retry.

**Honest gap (Phase 1.6 candidate):** we record `previous_sha256`
(what the file was BEFORE we wrote) but not what we actually wrote.
That means we can't currently detect the "another process modified
the file between our write and the undo" case — we'll restore the
previous content over their change. Closing this needs an
`applied_sha256` field on the backup and a stale check at
undo-time that returns `reason: write_backup_stale` and skips the
rollback. Tracked as a future round; the current implementation is a
fallback safety net, not a transactional guarantee.

### Phase 1.6+ — git-aware writes (deferred)

**Git-aware write hook (sandcastle-inspired):** add an optional
"after-write commit" hook so a session that has `git_commit_writes:
true` set in its context will, after each approved write applies, run
`git add <path> && git commit -m "..."` against either the working
tree (`head`) or a session-scoped branch. Three modes mirror
sandcastle's branch-strategy dial:

- **`head`** — no git involvement (today's behaviour)
- **`merge-to-head`** — commits land on a temp branch `lai/<session>/<run-id>`; merged on demand
- **`branch`** — commits land on an explicitly named branch

This is the foundation that lets `cowork-mode.md` Phase 5 implement
atomic cowork runs ("either all sub-task writes land or the branch is
discarded"). Undo when git-aware: prefer `git revert <commit>` over
the in-memory backup (cleaner audit trail; the backup becomes a
fallback for non-git workspaces).

**Per-iteration git worktree (also sandcastle-inspired):** when
`branch_strategy` is `merge-to-head` or `branch`, the session can
create a fresh `git worktree` rooted at the named branch and pin its
workspace to that worktree for the duration of the run. Lets multiple
parallel cowork runs share a repo without stepping on each other.
We're already running inside Claude Code worktrees today; this just
exposes the same capability to localaiproject sessions.

### Phase 2 — chat-flow detection ✅ shipped
What landed (`app/services/workspace_service.py` parser,
`app/services/chat_service.py` proposal hook,
`app/services/command_dispatcher.py` post-approve task completion,
`tests/test_workspace_writes.py` 15 new tests):

- `parse_fenced_writes(text)` scans for ```` ```<lang> file=<path> ````
  blocks; returns the list of `{path, content, info}` dicts in document
  order. Supports quoted paths (with spaces), `\\` → `/` normalisation,
  and a single leading `./` strip — but NEVER strips `..` characters
  (the resolver catches traversal attempts).
- `chat_service._maybe_chat_write_proposal()` runs after model
  inference; gated on **mode = build** + a pinned workspace + no
  existing pending approval; picks the first fenced block whose path
  passes `resolve_workspace_write_path` and returns it as a
  `kind: "write"` pending action with `origin: "chat"`,
  `previous_content_hash` for stale detection, and
  `original_message` so the post-approval flow can reference it.
- `_apply_pending_workspace_write` extended: after applying a
  `origin: "chat"` write, the dispatcher finds the in-progress
  builder task carrying the approval-pending marker and flips it
  to `done` (publishing the standard `KIND_TASK done` event). This
  is the missing piece that lets cowork sub-tasks deliver files
  via chat — the sub-task naturally completes once the user
  approves the file.
- Path-traversal blocked, plan-mode blocked, no-workspace blocked,
  pre-existing pending approvals not clobbered.

What's still deferred to a later round:
- **Multi-file batched approval** — Phase 2 handles the FIRST
  fenced block per response; subsequent blocks are ignored. The
  user can dispatch another chat turn (or the cowork sub-task can
  produce one block at a time, which it naturally does).
- **`workspace_action_message` plumbing into the dispatcher's chat
  return path** — the field is set on the chat result but the
  dispatcher event doesn't surface it as a separate banner yet.

### Phase 3 — diff display
- Built-in unified diff renderer in transcript
- "Approve all" for batched writes (cowork case)

### Phase 3.5 — workspace isolation audit ✅ shipped (2026-05-06)

Adopted from Local AI Workbench's
``storage/isolation.py`` pattern. A small ``app/services/workspace_isolation.py``
module scans the session's stored state for paths that escape the
pinned workspace (cross-workspace leakage). Three call sites today:

- **Dispatcher subcommand:** ``workspace audit`` — scans
  ``approved_search.files``, ``pending_approval.path``,
  ``write_backups[].path``, and the recent execution-replay
  ``approved_workspace_result`` paths. Returns a per-source severity
  report. Errors emit ``reason: outside_workspace``.
- **CoWork branch-strategy pre-flight:** ``cowork.py``'s
  ``_setup_branch_strategy`` calls the audit before creating a
  branch. If the audit finds errors, the run refuses to start with
  a `precondition_failed`-style ctx error (so stale state from a
  prior workspace can't scribble into a sibling tree under a
  branch-strategy run).
- **Future:** workspace pin / repoint hook (planned), proactive
  trigger pre-check (planned).

23 tests in ``tests/test_workspace_isolation.py``.

### Phase 4 — typed patch envelope (Phase 1 shipped, see `patch-taxonomy.md`)
- ✅ **Phase 1 shipped** (2026-05-06): chat-flow detection emits a
  ``kind="bundle"`` pending-approval when N>1 fenced blocks are
  valid; ``approve`` applies them atomically with rollback on
  any sub-action failure. Single-block path unchanged.
- Phase 2+ (planned): tier-based friction (L1/L2/L3) + per-kind
  shell-generated impact statements for L3.

### Phase 5 — UI
- Settings dialog: per-session "approved writers" list (auto-approve
  for trusted patterns)
- Write log panel showing recent file deltas

## Tests

- `tests/test_workspace_writes.py` — write proposal flow: propose,
  diff, approve, file appears, undo restores
- Path traversal is rejected
- Plan mode rejects writes
- Cap enforcement: 21st write fails with `reason: write_cap_reached`
- Backup rotation at >50 entries
- Bus events fire on apply + deny
- Stale backup detection (file modified between write and undo)

## Anti-patterns

- **Auto-approve.** No. Every write is a deliberate user act.
  Auto-approval undermines the trust model.
- **Approve-all without showing the list.** The user must see what's
  being applied; "Approve all" presents the full set first.
- **Recursive writes.** A handler that writes file A, which triggers
  a chat that proposes writing file B — refused. One write proposal
  per dispatch.
- **Writes outside the workspace.** Bound to the pinned workspace.
  Writing to `/etc/` requires re-pinning the workspace there
  explicitly, which is itself an explicit user act.

## Honest gaps

- **No cross-process write coordination.** Two localaiproject processes
  writing to the same file at the same time will race. JSON storage
  doesn't help here (it doesn't track files); DuckDB doesn't either.
  Single-user single-process is the assumption.
- **No version control awareness.** We don't `git add` after a write.
  The user (or their workflow) handles git. Future round could add an
  optional `auto_git_add` hook.
- **No conflict detection beyond previous-content-hash.** If the file
  changed between propose-time and approve-time, the hash mismatch
  catches it; the user is told and can re-propose.
- **No diff for binary files.** Binary writes are flagged + offered
  byte-count + sha256 comparison instead.

## Open questions

- Should writes inside the workspace under `.git/` be **always
  refused** as a footgun guard? Probably yes.
- Auto-approve list as a config? `workspace allow src/foo/`. Useful
  for tight loops (cowork builds) but undermines the trust model.
  Defer; manual approve until proven necessary.
- Soft cap bypass: should `cowork run <id>` get a higher cap because
  the plan was already approved? Probably yes — when the plan
  approval includes the file manifest, the per-write approval is
  redundant. Flag in cowork-mode.md.

## Cross-references

- `cowork-mode.md` — depends on this for file deliverables; cowork's
  Phase 5 branch-strategy + lifecycle hooks build on Phase 1.5 here
- `error-vocabulary.md` — adds `write_cap_reached`, `mode_required:
  build`, `write_backup_stale`, `symlink_unsupported`
- `sandbox.md` — per-iteration git worktrees compose with the sandbox
  story; both isolate state for concurrent runs
- `notes-buffer.md` — complementary; notes are session-scoped scratch,
  not workspace files
