# Architecture

A single-user desktop control layer over local Ollama models, plus
extensions that let multiple agents (in-process or over the network)
target one localaiproject session.

## Goals (from `project_plan.txt`)

- **Local-first.** No telemetry, no cloud APIs by default. Models run on
  the user's hardware via Ollama.
- **Lane-based routing.** A small router picks one of four lanes
  (reasoning / coding / vision / unfiltered), the registry resolves the
  lane to a model, the runtime client streams the chat. The registry
  also classifies hardware fit per lane: a RAM tier (safe / tight /
  too_big) plus a GPU **placement** hint (gpu / partial_offload / cpu)
  derived from detected VRAM, so the doctor/models views can warn
  "fits VRAM, fast" vs "CPU offload, slower" rather than RAM-only.
- **Approval-gated workspace access.** Any read or search inside the
  pinned workspace is queued as a pending action and requires explicit
  `approve` (CLI) or click (GUI) before running. The router never
  silently touches the filesystem.
- **Compact memory.** Old turns get summarised into a structured
  facts/preferences/open-tasks bucket so context windows stay sane.
- **Honest UI.** Tkinter fallback ships out of the box; PySide6 is the
  default when present. Streaming pull progress is shown live; cancel
  always works.

These haven't changed. This document covers what's been added on top.

## High-level layers

```
                                       ┌──────────────────────────────┐
        external agents / CLIs         │  Claude Code, OpenCode,      │
        (over loopback HTTP)           │  scripts, future MCP clients │
                                       └──────────────┬───────────────┘
                                                      │ POST /dispatch · GET /events (SSE)
                                                      ▼
┌────────────┐    ┌────────────┐    ┌─────────────────────────────────┐
│ Tk window  │────│            │    │  Local HTTP gateway             │
│  (legacy)  │    │            │◄───┤  (app/services/gateway/         │
└────────────┘    │            │    │   http_server.py · stdlib only) │
                  │  EventBus  │    └─────────────┬───────────────────┘
┌────────────┐    │  (process- │                  │ publish
│ Qt main    │────│   local    │                  │
│  window    │    │   pub/sub) │◄─────────────────┘
└────────────┘    │            │
                  │            │            ┌────────────────────────────┐
┌────────────┐    │            │◄───────────┤  dispatcher                │
│ LinkedView │────│            │  publishes │  (app/services/            │
└────────────┘    └────────────┘  every     │   command_dispatcher.py)   │
                                  dispatch  └─────────────┬──────────────┘
                                            event         │
                                                          │ calls
                                                          ▼
                                            ┌──────────────────────────────┐
                                            │  goal verbs                  │
                                            │  (app/services/goals.py)     │
                                            │  dispatch_and_wait, …        │
                                            └─────────────┬────────────────┘
                                                          │
                                                          ▼
                                            ┌──────────────────────────────┐
                                            │  primitives                  │
                                            │  local_memory · model_*      │
                                            │  workspace_service · …       │
                                            └─────────────┬────────────────┘
                                                          │
                                                          ▼
                                            ┌──────────────────────────────┐
                                            │  storage                     │
                                            │  JsonStorage / DuckDbStorage │
                                            │  + ACID mutate(fn) lock      │
                                            └──────────────────────────────┘
```

## The two-tier API

The dispatcher and `local_memory` expose two distinct surfaces. The
distinction matters because agents treat them differently.

### Tier 1 — primitives

Return immediately after one side-effect. Examples (in `local_memory`):

```
add_task(planner, text, builder=None) -> task_dict
dispatch_to_all(planner, text)         -> [task_dict, ...]
next_task(builder)                     -> task_dict | None
mark_task(task_id, status=...)         -> task_dict
get_inbox(builder, status="queued")    -> [task_dict, ...]
get_session_context(session_id)        -> dict
append_message(role, content, ...)     -> message_dict
```

These are the building blocks. They never wait. They're pure CRUD plus
queue operations.

### Tier 2 — goal verbs

In `app/services/goals.py`. Loop until a real-world condition is reached.

```
dispatch_and_wait(planner, text, *, builder=None, timeout=None, cancel_event=None)
fan_out_and_wait(planner, text, *, timeout=None, cancel_event=None)
drain_inbox(builder, *, max_iterations=100, timeout=None, stop_on_pause=True)
wait_for_inbox_empty(builder, *, timeout=None, cancel_event=None)
wait_for_task(task_id, *, timeout=None, cancel_event=None)
wait_for_state(session_id, predicate, *, timeout=None, cancel_event=None)
```

Polling-based (default 100 ms tick). Every verb takes an optional
`cancel_event: threading.Event`-shape so callers can abort within one
poll interval.

CLI surface for tier 2:

```
do <task>                       fan out + wait until each builder finishes
do @<builder> <task>            single builder + wait
wait inbox <session> [<sec>]    block until builder's queue is drained
wait task <task-id> [<sec>]     block until terminal
```

Full reference: `api/goals.md`.

## Gateway (process-local bus + optional HTTP)

The gateway has two parts. They serve different scopes but share a single
event bus singleton.

### EventBus (in-process, always on)

`app/services/gateway/bus.py` — process-local pub/sub. Every
`dispatch_input(...)` call publishes a `KIND_DISPATCH` event after
returning. Every task lifecycle transition publishes a `KIND_TASK`
event. UI panes subscribe and refresh on relevant events.

```
default_bus().subscribe(session_id, callback) -> sub_id
default_bus().subscribe(EventBus.ANY, callback)  # wildcard
default_bus().unsubscribe(sub_id)
default_bus().publish(GatewayEvent(...))
```

Thread-safe: callbacks fire **outside** the bus lock so a slow
subscriber can't deadlock anyone. Subscriber exceptions are swallowed
(logged debug) so one flaky listener can't abort delivery.

The Qt main window and `LinkedView` both subscribe; they coalesce
bursts via `QTimer.singleShot(50, refresh)` so a flood of events
becomes one repaint.

### HTTP+SSE gateway (off by default)

`app/services/gateway/http_server.py` — stdlib-only server bound to
127.0.0.1. Bridges the in-process bus to network clients. Endpoints:

| Method | Path                      | Purpose                                     |
|--------|---------------------------|---------------------------------------------|
| GET    | `/health`                 | Liveness (no auth required)                 |
| POST   | `/dispatch`               | Body `{session_id, text}` → dispatcher      |
| GET    | `/sessions/<id>/state`    | Snapshot: context + history + inbox + links |
| GET    | `/events?session=<id>`    | Server-Sent Events stream                   |

Optional bearer-token auth via `LOCALAI_GATEWAY_TOKEN`. `/health`
stays public even with auth on.

Lifecycle: `gateway start` / `gateway stop` / `gateway status` from
the dispatcher.

Full reference: `api/gateway.md`.

## Storage

Two backends behind one `MemoryStorage` interface
(`app/memory/storage.py`):

- **`JsonStorage`** — single JSON file, atomic write via `os.replace`.
  Default. Works without extra deps.
- **`DuckDbStorage`** — single `.duckdb` file, ACID transactions, plus
  an `executions_index` side table that powers SQL-backed
  `search_executions`. Auto-migrates from the JSON file on first run.
  Opt in via `MEMORY_BACKEND=duckdb`.

Both implement the same `mutate(fn)` primitive: read + apply + write
all happen under one lock so concurrent threads (and concurrent
processes, in DuckDB's case) never lose each other's edits.

The choice is documented in `../project_plan.txt` §8 with the
DuckDB-vs-alternatives comparison.

## Coordination (planner ↔ builder)

`local_memory` carries a top-level `coordination` bucket:

```
coordination:
  links:  { planner_id: [builder_id_1, builder_id_2, ...] }
  tasks:  [ {id, planner_session, builder_session, task, status,
             result, created_at, started_at, completed_at, execution_id} ]
```

A planner can fan out a task to all linked builders or target one
specifically with `dispatch @<builder>`. Each task gets routed to one
builder's inbox. The builder consumes its inbox via `inbox next` /
`inbox auto`, and on completion the planner receives a `[task ... DONE]`
system note in its history.

Backward-compat: an older Phase-1 schema stored `links` as
`{planner: builder_string}`. `_ensure_coordination_bucket` transparently
promotes string values to single-element lists on read, so existing
on-disk data keeps working.

## OpenCode-inspired features

A set of capabilities ported from OpenCode's design. Already shipped:

- **Plan / Build mode** — session-context flag; chat service injects a
  mode-specific system prompt; future write tools will gate on
  `local_memory.get_mode(session_id)`
- **`@file` mention in chat** — `app/services/mentions_service.py`
  parses `@<path>` tokens; the chat service inlines the file content as
  a system message before model inference. Bypasses workspace approval
  (the `@` is the user's consent). Per-file 8 KB / per-message 24 KB
  caps; binary files skipped.
- **Custom slash commands** — `cmd add <name> <prompt>` saves a prompt
  alias; `/<name>` substitutes it. Reserved-name guard prevents
  shadowing built-ins.
- **`undo` / `redo`** — per-process snapshot deque hooked into
  `_mutate`; intra-session safety net for accidental edits.

## Roadmap (designs in `docs/design/`)

These items have an agreed design and a doc, but the implementation is
queued. Grouped by theme.

**Foundations** — small, independently shippable building blocks:

| Doc                                      | One-liner                                                       |
|------------------------------------------|-----------------------------------------------------------------|
| `design/error-vocabulary.md`             | Small fixed `reason` tag set, emitted consistently on errors     |
| `design/cli-mode.md`                     | `python -m app.cli ... --json` for non-interactive scripting     |
| `design/notes-buffer.md`                 | Free-form markdown buffer in session context                     |
| `design/sandbox.md`                      | Nix flake / Dockerfile so external agents can spin up an env     |
| `design/workspace-writes.md`             | Approval-gated file writes (prereq for cowork file deliverables) |

**Orchestration & chat** — higher-tier user surfaces:

| Doc                                      | One-liner                                                       |
|------------------------------------------|-----------------------------------------------------------------|
| `design/cowork-mode.md`                  | Plan → approve → execute → synthesize state machine             |
| `design/chat-providers.md`               | Opt-in multi-provider chat lanes (OpenAI / Anthropic / Google)  |

**Extension surfaces** — how third-party code reaches the dispatcher:

| Doc                                      | One-liner                                                       |
|------------------------------------------|-----------------------------------------------------------------|
| `design/extensibility.md`                | Umbrella covering skill / command / plugin / MCP / browser / rules |
| `design/mcp-server.md`                   | Wrap the dispatcher as an MCP server so any MCP client can use it  |
| `design/browser-automation.md`           | Playwright-driven browser skill with per-origin approval        |
| `design/agent-rules.md`                  | Composable YAML rule layer the dispatcher enforces              |
| `design/workflow-templates.md`           | Saved + parameterised cowork plans / command sequences          |

These design docs are the authoritative source for the open work.
Implementations should follow them; revisions go through PR like any
other design change. The most natural ordering is foundations first
(workspace-writes unblocks cowork file deliverables; error-vocabulary
unblocks consistent rule predicates), then orchestration, then
extension surfaces in priority order (MCP server has the highest
external reach).

## Test posture

The suite is in `tests/`. As of 2026-05-03 it has 427 tests and runs
under 20 s on a warm cache (most tests don't touch Qt). Run:

```
QT_QPA_PLATFORM=offscreen python -m unittest discover tests
```

The `QT_QPA_PLATFORM=offscreen` is mandatory if you don't have a real
display. Qt-backed tests run real `LocalAIQtWindow` / `LinkedView` /
`SettingsDialog` instances against headless rendering so we exercise
real subscription wiring, not mocks.

## Where to make changes

| Concern                                  | Module                                       |
|------------------------------------------|----------------------------------------------|
| New dispatcher command                   | `app/services/command_dispatcher.py`         |
| New durable verb                         | `app/services/goals.py`                      |
| New session-state field                  | `app/memory/local_memory.py` `_default_context` + `_normalize_context` |
| New event kind                           | `app/services/gateway/bus.py` constants      |
| New HTTP route                           | `app/services/gateway/http_server.py`        |
| New Qt widget                            | `app/ui/`                                    |
| Theme tweak                              | `app/ui/themes_qss/foundry.qss`              |
| Skill recipe                             | `learnings/<skill>.md`                       |
| Workflow rule for agents                 | `../AGENTS.md`                               |

If you're not sure, the dispatcher is the spine — most additions touch
`command_dispatcher.py` plus their dedicated module.
