# AGENTS.md

Guide for AI coding agents working **inside** localaiproject (modifying its
code) and **with** localaiproject (driving it as a tool over the gateway).

This file is the first thing an agent should read. It's intentionally
short: rules for sizing your batches, what to query before acting, where
to find specific knowledge, and which mode you're in.

If you're a human, you can read this too — but the project's overall
direction lives in `project_plan.txt` and the architecture lives in
`docs/architecture.md`.

---

## 1. Hard rules (don't skip)

1. **Query state first.** Read before you write. The cheapest five
   commands you can run before any meaningful action:
   ```
   doctor          # full control-plane snapshot
   models          # which lanes are installed + hardware fit
   executions      # what's recently happened on this session
   tasks           # what's been dispatched / what's still queued
   link status     # who is this session connected to
   ```
   None of these mutate state. If you can't form a clear picture of the
   current state with these, you're not ready to dispatch.

2. **Respect Plan vs Build mode.** Check `mode` before you propose
   edits.
   - `mode plan` — analyze, search, explain, design. Do **not** propose
     concrete file edits. Don't run installs.
   - `mode build` — propose edits, queue workspace actions, run installs.
   Both modes still gate workspace reads/writes through `approve` /
   `deny`. Plan mode is your safer default; switch to Build mode only
   when you're about to make change.

3. **Workspace reads need approval.** Even in Build mode, every `read`
   and `search` against the pinned workspace requires the user (or
   another agent) to say `approve`. Don't fake the read; don't claim to
   have inspected a file you didn't.

4. **One source of truth for state.** All state lives in
   `local_memory` (backed by JSON or DuckDB depending on `MEMORY_BACKEND`).
   Never invent a side store for "your" agent's working memory — use the
   session context's `notes` buffer (see §6) or the compact summary.

5. **Test after every functional change.** The test suite runs in ~10s
   on a warm cache. Run `python -m unittest discover tests` and don't
   ship green if you broke a test you didn't intend to touch.

---

## 2. Batch sizing

If you're driving localaiproject as a remote agent (over the gateway or
from another chat session), choose your batch size deliberately.

| Batch size                | When to use                                                          |
|---------------------------|----------------------------------------------------------------------|
| Single dispatch (`do X`)  | Anything you haven't done before on this codebase. Validates the loop. |
| 2–5 dispatches            | A focused refactor with clearly-bounded scope.                        |
| `dispatch all` fan-out    | Three or fewer linked builders, all working on the same task variant. |
| `inbox auto`              | A queue of independent tasks you've already vetted individually.      |
| 10+ batched               | Only after you've successfully run the same shape at smaller batch.   |

> Aphorism, lifted from rs-sdk: *"A failed 5-minute run wastes more time
> than five 30-second diagnostic runs."*

Use the durable goal verbs (`do`, `wait inbox`, `wait task`) when you
need to **wait** for a task to actually finish before deciding what's
next. Use the primitives (`dispatch`, `inbox next`) when you want
fire-and-check-later semantics.

See `docs/api/goals.md` for the durable verb reference.

---

## 3. Where to look for skill recipes

Each session can activate one or more skills. The skills currently
shipped are:

| Skill        | When it applies                              | Recipe file                  |
|--------------|----------------------------------------------|------------------------------|
| `builder`    | Implementing new features, writing code      | `learnings/builder.md`       |
| `reviewer`   | Reading code, checking quality, writing tests| `learnings/reviewer.md`      |
| `planner`    | Breaking work down, sequencing, dispatching  | `learnings/planner.md`       |

When a session has skill X active, the chat service automatically
inlines `learnings/X.md` into the model's system context (planned —
once shipped you don't need to do anything; until then, read the file
yourself before you act).

Add a new recipe by writing `learnings/<skillname>.md` and registering
the skill in `app/services/skill_service.py`.

### Workspace convention files (auto-loaded into the prompt)

When a workspace is pinned, two markdown files at its root are read
on every chat turn and injected as their own slots in the layered
prompt assembly (no approval required — same level of trust as
having pinned the workspace itself; same cap-and-truncate behaviour
as the rest of the prompt slots). Conventions are Hermes-inspired:

| File at workspace root | Slot              | What it's for                                                |
|------------------------|-------------------|--------------------------------------------------------------|
| `PERSONA.md`           | `persona`         | Voice, tone, behavioural constraints durable for this project|
| `AGENTS.md`            | `project_context` | What the codebase is, conventions, project-specific commands |

Distinct from the control layer's *own* `AGENTS.md` (this file): the
workspace's `AGENTS.md` lives at the user's project root and tells
the model about *their* project. Both are bounded (64 KB hard cap,
truncate per slot `char_budget`); inspect what gets loaded with
`prompt show` and tune budgets with `prompt set-budget <slot> <n>`.
See `docs/design/prompt-assembly.md`. For tighter prompts (same
information, fewer tokens) flip on `prompt compact on` — see
`docs/design/compact-prompt.md`.

For typed/conditional/addressable behavioural rules (better than
ChatGPT-style custom instructions: per-session, AND/ANY filters
on mode/workspace/tags/lanes, toggle individual rules without
deleting their body) use `instructions add <kind> <body>` — see
`docs/design/custom-instructions.md`.

---

## 4. Multi-agent coordination

Two patterns are first-class:

**Planner → Builder(s)** — one session dispatches tasks to one or more
builder sessions and waits for results. Each builder runs its inbox.
Use:
```
link <builder>          # add a builder to your link list
dispatch <task>         # fan out to all linked builders
do @<builder> <task>    # single builder + wait until done
```

**External agent → localaiproject session** — another process (Claude
Code, an OpenCode agent, your own script) drives one session over the
HTTP gateway. Use:
```
gateway start                              # in localaiproject
curl -X POST 127.0.0.1:7780/dispatch ...  # from anywhere on loopback
```

See `docs/api/gateway.md` for the full HTTP + SSE reference.

---

## 5. Session-scoped vs app-scoped state

| Where it lives          | Examples                                                          |
|-------------------------|-------------------------------------------------------------------|
| `session.context`       | workspace_path, active_skills, mode, pending_approval, notes      |
| `session.history`       | chat turns + system [task ... DONE] notes                         |
| `session.executions`    | one record per dispatch (router decision, model, result, replay)  |
| `session.summary`       | compact memory: facts, preferences, open_tasks                    |
| App-level (top of store)| `coordination.links`, `coordination.tasks`, `custom_commands`     |
| Process-only (in-memory)| undo/redo snapshot stacks, EventBus subscribers                   |

If you're storing something cross-session, you probably want
`coordination.*` or `custom_commands`. If you want a free-form
scratch-pad inside one session, use `notes` (planned — see
`docs/design/notes-buffer.md`).

---

## 6. Standard error vocabulary

When a dispatch fails or refuses to act, the event will carry a
`reason` tag from this small fixed set so you can pattern-match without
parsing English. All tags below are **shipped** — the dispatcher
emits them today (verified by `tests/test_error_vocabulary.py`).

| `reason`              | What it means                                                              |
|-----------------------|----------------------------------------------------------------------------|
| `no_workspace`        | The session has no pinned workspace; `workspace <path>` first              |
| `path_not_found`      | A path was referenced that doesn't exist inside the workspace              |
| `outside_workspace`   | A path was referenced that escapes the pinned workspace root               |
| `approval_required`   | A workspace `read`/`search` is queued; `approve` or `deny` to continue     |
| `no_link`             | A `dispatch` was issued but the planner has no linked builders             |
| `unknown_builder`     | `dispatch @x …` referenced a builder that isn't linked                     |
| `inbox_empty`         | An `inbox next` / `inbox auto` ran when the inbox was already empty        |
| `unknown_target`      | `install` / `uninstall` / `skill` / `retry` got a name that isn't known    |
| `runtime_offline`     | Ollama is not reachable                                                    |
| `model_unavailable`   | The lane resolved to a model that isn't installed and can't be pulled now  |
| `version_too_old`     | A model needs a newer Ollama version than is installed                     |
| `hardware_too_small`  | The model is too big for available RAM                                     |
| `invalid_argument`    | Command parsed but arguments didn't validate (usage error, bad name, …)    |
| `timeout`             | `wait inbox` / `wait task` exceeded its deadline                           |
| `already_running`     | `gateway start` called when a prior instance is in flight                  |
| `internal_error`      | An unexpected exception bubbled up; check executions log                   |
| `cancelled`           | A long-running operation was aborted via the cancel event                  |

Planned tags (designed but emitted only by feature surfaces still in
flight — see the linked design docs):

| `reason`                  | Surface                  | Quick meaning                                       |
|---------------------------|--------------------------|-----------------------------------------------------|
| `mode_required`           | workspace-writes / cowork| Action needs `mode build` (or another named mode)   |
| `write_cap_reached`       | workspace-writes         | Per-session soft cap on file writes hit             |
| `symlink_unsupported`     | workspace-writes         | A write proposed creating a symlink                 |
| `provider_unauthenticated`| chat-providers           | Lane bound to a provider with no usable credentials |
| `skill_unavailable`       | extensibility            | A required skill isn't installed in this session    |
| `origin_not_approved`     | browser-automation       | Browser action on a non-whitelisted origin          |
| `rule_loop`               | agent-rules              | A rule re-fired itself past the depth cap           |
| `template_param_missing`  | workflow-templates       | Template run without a required parameter           |

Full spec: `docs/design/error-vocabulary.md`. The canonical Python
constants live in `app/services/error_vocabulary.py`; new code should
import from there rather than typing string literals.

---

## 7. Useful commands by intent

```
# Inspect
help                           list commands
doctor                         full state snapshot
models                         lane / model fit
executions                     recent dispatches
executions search <q>          filter by lane / status / since
tasks                          all coordination tasks
link status                    who's linked to whom
gateway status                 is the HTTP gateway up

# Modify intent
mode plan                      analyse only
mode build                     allow edits
workspace <path>               pin a workspace
workspace audit                scan stored state for cross-workspace path leakage
workspace clear                unpin the workspace (clears approvals + searches)
workspace writes-bump <n>      raise the per-session write soft cap
read <path> / search <q>       queue an approval-gated workspace action
write <path> <<EOF...EOF       queue an approval-gated file write (Build mode only)
delete <path>                  queue an approval-gated file delete (Build mode only)
diff                           show diff for the pending write/delete proposal
approve / deny                 act on the pending approval (L1/L2)
approve <phrase>               act on a high-impact (L3) approval — phrase shown in banner

# Coordinate
link <builder>                 add a builder
dispatch <task>                fan out (queue, return immediately)
do <task>                      fan out + wait until done (durable)
do @<builder> <task>           single builder + wait
iterate <task> [--max <n>]     loop dispatch until builder emits <task-complete>
inbox / inbox next / inbox auto  process queued tasks
wait inbox <session> [<sec>]   block until drained
wait task <id> [<sec>]         block until terminal

# Brainstorm + orchestrate
notes [show]                   display the working_memory block (legacy alias)
notes append <text>            jot a note for the next chat turn (auto-included)
notes set <text> | notes clear  replace / wipe working_memory
note list                      list all memory blocks with usage
note show <label>              inspect one block (description + value)
note set <label> <text>        set a block's value (truncates to char_limit)
note describe <label> <desc>   update the block description
note budget <label> <bytes>    raise / lower a block's char_limit
note clear <label> | note remove <label>  clear value / drop user-defined block
cowork "<goal>"                plan + approve + execute + synthesize a multi-step goal
cowork plan "<goal>"           plan only; pause for approval before execution
cowork run <id> [--workers N]  execute an approved plan (default 4 workers, hard cap 8)
cowork status | cowork cancel  inspect / abort the active run
template list                  show registered templates (templates/*.toml|json)
template show <id>             inspect one template's params + steps
template run <id> [k=v ...]    run a template with substituted params
template reload                re-parse the templates/ directory

# Constraints
rules [list]                   show registered rules + which are disabled
rules show <id>                inspect one rule
rules disable|enable <id>      toggle a rule for the current process
rules reload                   re-parse rules/ from disk
tags [show]                    show tags on the current session
tags add <tag>                 attach a tag (used by `session_tag` rule predicate)
tags remove <tag> | tags clear  detach / wipe tags

# Cloud lanes (opt-in, per session)
provider list                  show registered providers + credential status
provider show                  show this session's lane→provider bindings
provider bind <lane> <id>:<m>  e.g. `provider bind reasoning anthropic:claude-sonnet-4-5`
provider unbind <lane>         remove a binding (lane falls back to ollama)

# Expose dispatcher as MCP tools (for Claude Code / Cursor / OpenCode)
mcp [info]                     show MCP tool/resource counts
mcp tools                      list every tool the MCP server exposes
mcp config                     emit a copy-paste .mcp.json snippet for clients
mcp serve                      run the stdio MCP server in this process

# Read-only browser skill (Playwright; Phase 1)
browser open <url>             load a URL (first hit per origin needs approve)
browser navigate <url>         navigate within an approved origin
browser screenshot [<sel>]     capture a PNG to _browser_cache/
browser get-text [<sel>]       innerText of page (or selector)
browser get-html [<sel>]       outerHTML of page (or selector)
browser close                  tear down the active context

# Backlog adapters (pull external work into the inbox)
backlog list                   show registered sources (json_file is built-in)
backlog pull <id> [k=v ...]    pull items; e.g. `backlog pull json_file path=tasks.json builder=coder`

# Recall memory (search chat history across sessions)
recall <query>                 substring search; default 10 hits
recall <query> --since 24h     bound by recency (24h / 7d / 1w / ISO)
recall <query> --session <id>  bound to one session
recall <query> --limit N       cap hit count (max 200)

# Prompt assembly inspection + budgets (Phase 2)
prompt show                    render the slot trace for the next chat turn
prompt limits                  show per-slot defaults + session overrides
prompt set-budget <slot> <n>   override one slot's char_budget (n=0 clears)
prompt set-total <n>           override total-budget cap (n=0 reverts)
prompt clear-budgets           drop all per-slot overrides
prompt compact [on|off]        opt-in dense slot rendering (compact-prompt.md)

# Custom instructions (custom-instructions.md Phase 1 + 2 + 2.5)
instructions list                       show typed/conditional rules + status
instructions add <kind> <body...>       new entry; flags: --label/--id/--mode/--workspace/--tags/--lanes/--priority
instructions edit <id> [flags]          edit in place; --body/--kind/--priority/--mode/--no-mode/--tags/--no-tags/...
instructions show <id>                  full body + condition + budgets
instructions enable <id>                re-arm a disabled rule
instructions disable <id>               mute without losing the body
instructions hush <id> [--turns N]      temporary mute; auto re-arms after N turns (default 1)
instructions remove <id>                drop entirely
instructions clear                      drop all entries
instructions import-persona             bulk-create one entry per PERSONA.md heading

# Proactive trigger (patch-taxonomy.md Phase 3 — `honest null` pattern)
proactive status               heuristic state + would_fire_now
proactive tick                 manually invoke; null is a normal outcome
proactive reset                clear all proactive state
proactive prompt               show what the model would see (debug)
proactive autofire [on|off]    toggle the dispatch-driven auto-scheduler

# Learned lane classifier (agent-rules.md Phase 5a)
classifier status              show whether trained + per-label sample counts
classifier train               (re)train from this session's executions
classifier predict <text>      preview the learned lane (no live routing yet)
classifier reset               clear the trained model
classifier autoroute [on|off]  blend classifier votes into the live router

# BM25 retrieval over workspace docs (recall-memory.md Phase 2a)
retrieve <query>               BM25 search; lazy-builds index on first call
retrieve build                 force rebuild of the index
retrieve status                show whether index is cached + doc/vocab counts
retrieve clear                 drop the cached index

# Tool calling (tool-calling.md Phase 1)
tool show                      list `​```dispatcher` blocks from the most recent assistant turn
tool run <n>                   recursively dispatch the Nth call (1-based)
tool parse <text>              debug — scan arbitrary text for tool calls
tool enable [on|off]           opt the model into the teaching slot (Phase 2)

# Recover
write undo                     restore the last approved file write/delete
                               (alias of `undo` when a write_backup exists)
undo                           revert last memory mutation (intra-session)
redo                           re-apply
```

---

## 8. Files you should know about

| Path                                | Purpose                                                |
|-------------------------------------|--------------------------------------------------------|
| `project_plan.txt`                  | The strategic plan the project is following            |
| `docs/README.md`                    | Index for everything in `docs/`                        |
| `docs/architecture.md`              | System overview + roadmap                              |
| `docs/vision/surfaces.md`           | Shipped + planned surfaces; how to choose              |
| `docs/api/gateway.md`               | HTTP gateway + EventBus reference                      |
| `docs/api/goals.md`                 | Durable goal verbs (`dispatch_and_wait`, etc.)         |
| `docs/design/extensibility.md`      | Umbrella for skill / command / plugin / MCP / browser  |
| `docs/design/cowork-mode.md`        | Plan → approve → execute → synthesize state machine    |
| `docs/design/workspace-writes.md`   | Approval-gated file writes (cowork prereq)             |
| `docs/design/agent-rules.md`        | Composable YAML rule layer the dispatcher enforces     |
| `docs/design/*.md`                  | Other designs for items not yet shipped                |
| `learnings/*.md`                    | Per-skill recipe knowledge                             |
| `app/memory/local_memory.py`        | Storage-backed session state API                       |
| `app/services/command_dispatcher.py`| The dispatcher routes every command                    |
| `app/services/goals.py`             | High-level durable verbs                               |
| `app/services/gateway/*.py`         | EventBus + HTTP gateway                                |
| `app/services/coordination_*.py`    | (Conceptual location for) planner / builder logic      |
| `app/services/prompt_assembly.py`   | Layered prompt slot producers + per-slot trace         |
| `app/services/project_context_service.py` | PERSONA.md + project AGENTS.md readers           |
| `app/services/workspace_isolation.py` | Cross-workspace path-leakage audit                   |
| `app/services/patches.py`           | Typed Patch envelope + bundle aggregation             |
| `app/services/impact_statements.py` | Tier resolver + shell-generated impact + L3 phrase    |
| `app/services/proactive_service.py` | Proactive trigger: heuristic + null-honest parser     |
| `app/services/cancel.py`            | CancelToken cooperative cancellation primitive        |
| `app/services/reports.py`           | StageReport dataclass + cowork run → typed report     |
| `app/services/lane_classifier.py`   | Pure-Python Naive Bayes lane classifier               |
| `app/services/retrieval_index.py`   | BM25 index over workspace docs                        |
| `app/services/chat_providers.py`    | Provider registry; batch + streaming dispatch         |
| `app/models/runtime_client.py`      | Ollama HTTP client (batch + streaming)                |
| `app/services/vision_service.py`    | `@image:<path>` attachments for vision-capable models |
| `app/services/tool_calls.py`        | ` ```dispatcher` fenced-block parser + tool run dispatcher |
| `app/ui/prompt_assembly_view.py`    | Qt panel — live slot inspector with budget bar         |
| `app/ui/image_drop_input.py`        | Qt drag-drop input that auto-inserts `@image:` tokens  |

---

## 9. When in doubt

- Run `doctor`, look at the full snapshot.
- If you're about to do something destructive: are you in Build mode?
  Have you got an undo path?
- If you're about to ship code: did you run the tests?
- If you're about to dispatch a multi-task batch: have you done one
  smaller dispatch first to validate the shape?
- If you genuinely don't know what to do next: ask. Don't guess.
