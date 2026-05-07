# Surfaces · Where localaiproject lives

**Adapted from "Seamless Workflow Integration".**

Same dispatcher, same storage, many entry points. localaiproject is a
control layer; the goal is that you reach it from whichever surface
already lives in your workflow rather than being forced to switch
context.

## What's shipped (you can use today)

| Surface                          | Module                                          | Best for                                                                              |
|----------------------------------|-------------------------------------------------|---------------------------------------------------------------------------------------|
| Interactive CLI                  | `app/ui/main_window.py::_run_cli`               | Headless / SSH / no-display environments. The fallback when no GUI framework is found. |
| Tk window                        | `app/ui/main_window.py::main`                   | Legacy / minimal-deps fallback. Auto-selected when PySide6 isn't installed.           |
| PySide6 main window              | `app/ui/qt_main_window.py`                       | Default. Full transcript, executions, flow graph, install progress, cancel button.   |
| LinkedView (planner ↔ builder)   | `app/ui/linked_view.py`                         | Multi-session work. Three-pane: planner, task queue, builder tabs.                    |
| ModelsView                       | `app/ui/models_view.py`                         | Lane / model fit at a glance. Per-row install with live progress.                     |
| Settings dialog (theme editor)   | `app/ui/settings_dialog.py`                     | Theme presets, kind picker, drag-and-drop QSS import.                                 |
| Local HTTP+SSE gateway           | `app/services/gateway/http_server.py`           | External agents / scripts driving one session over loopback HTTP.                     |
| EventBus (process-local)         | `app/services/gateway/bus.py`                   | In-process subscribers (UI panes, future MCP server) reacting to dispatches.          |

All of these route through one `dispatch_input(text, session_id)` call.
There's no parallel implementation per surface — change a feature once,
every surface picks it up.

## What's planned (designed, queued)

| Surface                          | Design doc                                      | Brief                                                                                |
|----------------------------------|-------------------------------------------------|--------------------------------------------------------------------------------------|
| Non-interactive CLI              | `../design/cli-mode.md`                         | `python -m app.cli --session <id> --command "<text>" --json` for scripting / CI.     |
| MCP server                       | `../design/mcp-server.md`                       | Wrap the dispatcher as MCP tools. Any MCP client (Claude Code, OpenCode, …) drives.  |
| Code-editor integration          | (this doc · §"Code editor integration")          | LSP-extension-shaped: VS Code / JetBrains / Neovim plugin that talks to the gateway. |
| Sandbox / Docker / Nix           | `../design/sandbox.md`                          | One-command spin-up for any environment.                                             |

## How to choose a surface

```
                        ┌──────────────────────────┐
                        │  What are you doing?     │
                        └──────────────┬───────────┘
                                       │
              ┌────────────────────────┼─────────────────────────┐
              │                        │                         │
        Interactive               Scripting /                Embedded /
        chat work                 CI / one-shot              another agent
              │                        │                         │
              ▼                        ▼                         ▼
       Qt main window           CLI mode                   HTTP gateway
       (or Tk fallback)         (planned)                  or MCP server
                                                           (planned)
              │                        │                         │
              │ multi-session?         │                         │
              ▼                        ▼                         ▼
       LinkedView              `--from-stdin`               EventBus
       (planner+builder)       for piped input              subscribers
                                                            (LinkedView,
                                                             future MCP,
                                                             future plugin)
```

## Code editor integration (planned)

The big missing surface. Pattern that fits cleanly with what we have:

### Option A — LSP-shaped editor plugin (loose coupling)

The plugin runs in the editor; localaiproject runs separately with the
gateway started. The plugin talks to the HTTP gateway over loopback.
What the plugin gets:

- **`POST /dispatch` from a keybind** — e.g. select code, hit `Cmd-K`,
  the plugin sends `dispatch @<builder> refactor this:\n<code>` and
  drops the response into the buffer.
- **`/sessions/<id>/state`** — sidebar panel showing current pinned
  workspace, active skills, executions list.
- **`GET /events`** — live indicator when a builder produces a result;
  syslog-style notification.
- **`POST /dispatch` with `text=approve`** — approve workspace reads
  from inside the editor, never leaving context.

This is a thin TypeScript / Lua / Vimscript shim over `fetch`/`curl`.
We don't need to ship it as part of localaiproject; once the gateway
is documented (`../api/gateway.md`), anyone can write the plugin in an
afternoon.

### Option B — MCP-shaped editor plugin (tight coupling)

Once the MCP server (`../design/mcp-server.md`) ships, editors that
already support MCP (Claude Code, possibly Cursor / Zed / VS Code via
extension) get localaiproject as a tool source for free. No editor
plugin to maintain.

### Recommended: ship MCP first; LSP-shim is fallback

The MCP path covers more editors with less code. Build it; if a target
editor doesn't yet speak MCP, fall back to a thin gateway-HTTP shim
for that editor specifically.

## How they share state

All surfaces share **one storage backend** per process. The bus + gateway
extend that to multiple processes:

```
Process A (Qt main window)  ─┐
Process B (CLI one-shot)    ─┼─► same DuckDB / JSON file on disk
Process C (gateway)         ─┘   ↑
                                 │ atomic via storage.mutate(fn) lock
External agent over HTTP    ────►│
External agent over MCP     ────►│
```

`storage.mutate(fn)` serialises concurrent writers correctly across
threads in one process. Across processes, JsonStorage relies on
last-writer-wins (acceptable for the single-user case); DuckDB's
own locking handles real cross-process safety. See
`../architecture.md` §Storage.

## CoWork-mode equivalent

Abacus's "CoWork" maps to our **planner/builder + goal verbs** combo:

| Abacus CoWork capability              | Our equivalent                                                                |
|---------------------------------------|-------------------------------------------------------------------------------|
| Delegate complex multi-step tasks     | `link <builder>`; `dispatch <task>`; `do <task>`                              |
| Plans / executes / delivers           | `goals.fan_out_and_wait` for batch; `goals.dispatch_and_wait` for single      |
| Polished results to local files       | **Planned** — see `../design/workspace-writes.md`                              |
| Multi-step orchestration              | **Planned (extension)** — `../design/cowork-mode.md`                          |

We're materially close; the gap is the file-deliverable side. See
the linked design docs.

## Honest gaps

- **No editor extension is shipped today.** You can still drive the
  gateway from your editor with `curl` or a small ad-hoc script.
- **No mobile / iOS surface.** Out of scope for a desktop control
  layer.
- **No web surface.** The HTTP gateway exposes JSON; you'd write the
  UI yourself. This is intentional — the desktop UI is the canonical
  one.
- **Cross-process state in JSON backend is best-effort.** Use the
  DuckDB backend (`MEMORY_BACKEND=duckdb`) when you genuinely have
  multiple processes.

## Reference

- Gateway protocol: `../api/gateway.md`
- Goal verbs: `../api/goals.md`
- Architecture: `../architecture.md`
- MCP design: `../design/mcp-server.md`
- CLI mode design: `../design/cli-mode.md`
