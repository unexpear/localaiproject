# reviewer

Recipe for the `reviewer` skill — reading code, checking quality,
writing tests, catching regressions.

## Default lane

Routes to `reasoning` (llama3.1:8b). Falls back to `coding` if
reasoning isn't installed.

## Workflow

1. **Read the change, then read what it touches.** A diff in
   isolation lies. Pull the surrounding file with `@<path>` so you
   see the full context, not just the patch hunks.
2. **Map the change to its tests.** For every module touched, the
   tests live at `tests/test_<module>.py` (or in a closely-named
   sibling). If the change has no tests, that's the first thing to
   flag — even before reading the code.
3. **Run the tests yourself before commenting.**
   `QT_QPA_PLATFORM=offscreen python -m unittest discover tests`. A
   green suite earns the right to argue about style; a red suite
   means style is a distraction from the real problem.
4. **Be specific.** "This could be cleaner" is noise. "Line 47 races
   on `self._foo` because it's read in `_handle_x` without the lock"
   is a review.
5. **Sort findings by severity.** Use the same three buckets the
   huashu-design critique guide does:
   - ⚠️ **critical** — bug, security issue, regression, data loss
   - ⚡ **important** — design issue, missing test, surprising
     behaviour, leak
   - 💡 **optional** — naming, consistency, minor cleanup

## Five-axis review

Score 0–10 across all five before writing the summary.

1. **Correctness** — does it do what it claims?
2. **Test coverage** — are the new code paths exercised? what about
   the failure modes?
3. **Architectural fit** — does it match the existing structure
   (dispatcher / goals / storage / gateway / etc.)? Or is it a fork
   of one of those?
4. **Concurrency safety** — does it touch state? if yes, is the
   read-modify-write atomic?
5. **Agent-friendliness** — for dispatcher commands: does it set
   `event["reason"]` on errors? does it document the new command in
   `HELP_TEXT` and `_RESERVED_CMD_NAMES`?

If any axis scores below 6, fix it before merging.

## Common smells in this codebase

- **Local echo + bus refresh double-render.** If a Qt handler appends
  to the transcript AND the bus also fires for the same event, the
  user sees the message twice. Check that chat-action paths skip the
  local echo (see the refactored `_dispatch_text`).
- **Naked `load_memory()` + mutate + `save_memory()`.** That's the
  pre-Phase-2 pattern with the data-loss race. Call `local_memory._mutate(fn)`
  instead. The convenience wrappers like `set_workspace` already do.
- **Missing `try/except` around bus publish.** Every place that
  `default_bus().publish(...)` should be wrapped (or use the
  `_publish_task_event`-style helper) so a broken bus doesn't abort
  the caller.
- **Synchronous goal verbs on the Qt main thread.** `dispatch_and_wait`
  blocks. Calling it from a Qt slot freezes the UI. Spawn a worker.
- **Path traversal not normalised.** Anything that takes a
  user-supplied path inside the workspace should go through
  `resolve_workspace_item_path` so `..` escapes are blocked.
- **New top-level keys in storage without a default.** Storage
  payload shape is `{"sessions": {...}, "coordination": {...},
  "custom_commands": {...}}`. Adding a key needs a default in
  `_ensure_*` helpers and migration handling for old data.

## Dispatcher review checklist

Per new dispatcher command:

- [ ] Listed in `HELP_TEXT`
- [ ] Bare name in `_RESERVED_CMD_NAMES`
- [ ] Returns `{"action", "session_id", "output", ...}`
- [ ] Sets `event["reason"]` on error paths (per
      `docs/design/error-vocabulary.md`)
- [ ] Doesn't call `dispatch_input` recursively (would re-publish bus event)
- [ ] Has tests for: happy path, missing args, bad args, error paths
- [ ] Doesn't perform IO without an approval gate when applicable

## Test review checklist

- [ ] One concern per test (or clearly enumerated assertions for a
      contract)
- [ ] Uses `_isolated_memory(tmp_dir)` if it touches storage
- [ ] Mocks `handle_chat` if it would otherwise call Ollama
- [ ] Cleans up Qt widgets via `try/finally: win.deleteLater()` so the
      next test doesn't inherit state
- [ ] Doesn't depend on test execution order
- [ ] Doesn't sleep more than necessary; uses event-driven waits

## Don'ts

- **Don't** rewrite the patch in your review. Suggest the change;
  let the author write it
- **Don't** flag non-issues to look thorough. Empty review > noisy
  review
- **Don't** review style without context — the codebase has its own
  conventions; check them in `learnings/builder.md` first
- **Don't** approve a change you didn't actually run

## When you find a real bug

If the bug is in the change under review: comment with severity tag
+ specific fix. Don't merge.

If the bug is **pre-existing** (not introduced by this change):
- Is the change making it worse? If yes, block.
- If it's orthogonal, say so explicitly: "Pre-existing bug, not
  blocking this change. Filing separately."
- Use `mcp__ccd_session__spawn_task` (or its CLI equivalent) to file
  a follow-up so it doesn't get lost.

## Reading the audit log

Before reviewing a change to a session-scoped feature, look at recent
state:

```
> executions search <module name>     # what's been happening
> tasks <session>                     # what's queued / done
> doctor                              # full snapshot
```

A change that "fixes" something nobody is hitting is suspect. A
change that "fixes" something that fails on every other dispatch is
worth landing fast.
