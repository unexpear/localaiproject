# Design: Extensibility & Integrations

**Status: umbrella · 2026-05-03**

**Adapted from "Extensibility & Integrations".**

How the codebase is meant to be extended, plus the catalog of
integration designs (each with its own deeper doc).

## Eight extension surfaces

```
              ┌──────────────────────────────────────────────────────┐
              │  YOUR EXTENSION                                      │
              └─┬──────┬────────┬───────┬───────┬───────┬───────┬────┘
                │      │        │       │       │       │       │
                ▼      ▼        ▼       ▼       ▼       ▼       ▼
              Skill  Custom  Plugin   MCP    Browser  Rules  Templates  ← user-authored
              recipe command package server  skill   YAML   YAML        ← + Backlog
                │      │        │       │       │       │       │   adapters
                ▼      ▼        ▼       ▼       ▼       ▼       ▼
              ┌──────────────────────────────────────────────────────┐
              │              dispatcher (one entry point)            │
              └──────────────────────────────────────────────────────┘
```

Each surface has a clear use case. Pick the lightest that solves your
problem.

| You want to…                                       | Use                       | Effort       |
|----------------------------------------------------|---------------------------|--------------|
| Give a skill some recipe knowledge                 | Skill recipe              | Markdown only|
| Save a frequently-used prompt as `/<name>`         | Custom command            | One CLI call |
| Add a new dispatcher command + handler             | Plugin (planned)          | One Python module |
| Expose localaiproject as an MCP tool source        | MCP server (planned)      | Already-designed |
| Drive a browser as part of a task                  | Browser automation skill (planned) | Playwright + skill recipe |
| Codify behavioural constraints declaratively       | Agent rules (planned)     | YAML rule + loader |
| Save a parameterised cowork plan / command list    | Workflow templates (planned) | YAML template |
| Pull tasks from GitHub Issues / Linear / etc.      | Backlog adapter (planned) | One Python module + config |

## 1. Skill recipes (shipped)

Per-skill markdown files in `learnings/`. The chat service
auto-includes the matching file when a session has the skill active
(auto-include is planned — recipes themselves are written; see
`learnings/README.md`).

Reach for this when: you want to teach the model conventions for a
specific role (`builder`, `reviewer`, `planner`) without changing any
code.

## 2. Custom slash commands (shipped)

`cmd add <name> <prompt>` saves a prompt alias. `/<name>` substitutes
it. Persists in storage. Reserved-name guard prevents shadowing
built-ins.

Reach for this when: you find yourself typing the same prompt three
times. Promote it to a custom command.

## 3. Plugin model (planned)

A way to add new dispatcher commands without forking the codebase.
Sketch:

### Plugin entry point

```python
# my_plugin/plugin.py
from app.services.command_dispatcher import register_command, ReservedName

@register_command("greet", reserved=True)
def greet(text, session_id, args):
    return {
        "action": "greet",
        "session_id": session_id,
        "output": f"Hello, {args.strip() or 'world'}!"
    }
```

### Discovery

Plugins are discovered via:
- `entry_points` in installed packages (Python's standard plugin
  mechanism), group `localaiproject.dispatcher_commands`
- A `plugins/` folder in the project (auto-imported on startup; symlinks
  allowed)
- An explicit `--plugin <module>` CLI flag

### Lifecycle

Plugins are import-and-register; no `setup()`/`teardown()` hooks for v1.
A plugin that wants long-lived state uses the existing storage
(top-level key namespaced by plugin name).

### Tests

Plugins ship their own tests; the framework provides a
`@plugin_test` decorator that gives a clean `_isolated_memory(tmp)`
plus dispatcher in a fixture.

### Status

Spec sketch only. Not implemented. Pulls forward when someone has a
real third-party plugin in mind.

## 4. MCP server (planned · `mcp-server.md`)

Wrap the dispatcher as a Model Context Protocol server so any MCP
client (Claude Code, OpenCode, Cursor with MCP extension) drives
localaiproject as a tool source.

Highest-leverage extension by reach. Ship the stdio transport first
(matches Claude Code's default), HTTP transport second.

Full design: [`mcp-server.md`](./mcp-server.md)

## 5. Browser automation (planned · `browser-automation.md`)

A first-class skill that gives a builder Playwright capabilities so it
can navigate sites, fill forms, scrape data, and screenshot results.

Full design: [`browser-automation.md`](./browser-automation.md)

## 6. Agent rules (planned · `agent-rules.md`)

A composable, declarative rule layer beneath `AGENTS.md`, mode flags,
and skill biases. Lets you say "in this session, the builder must
always run tests after a write" or "no model output longer than 2000
tokens unless approved" — as YAML rules the dispatcher enforces.

Full design: [`agent-rules.md`](./agent-rules.md)

## 7. Workflow templates (planned · `workflow-templates.md`)

Saved cowork plans + custom-command sequences you can reuse: "set up a
new Python lib", "do my morning standup", "release a new version".
Power-user automation that builds on the cowork + custom-command
primitives.

Full design: [`workflow-templates.md`](./workflow-templates.md)

## 8. Backlog adapters (Phase 1 shipped, sandcastle-inspired)

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — `BacklogSource` Protocol + `register_backlog_source` decorator
+ built-in `JsonFileSource` + `pull_into_inbox` + `backlog list / pull`
dispatcher commands — is shipped (`tests/test_backlog_service.py`, 22
tests). Phase 2 (GitHub Issues source, periodic `backlog auto`,
per-session source-config storage) is planned.

A pluggable interface for pulling tasks into a session's inbox from
external systems — GitHub Issues, Linear cycles, Jira tickets, a
local JSON file, or anything else with a list-of-things-to-do shape.

### Why

Today, `inbox auto` drains the in-process queue: tasks land via
`dispatch` from another linked session. That's perfect for in-session
coordination but useless when the work originates outside
localaiproject (a label on a GitHub issue, a Linear card moving into
"Ready", etc.).

Sandcastle's "backlog manager" abstraction (it pulls from GitHub
Issues by default, supports Beads, and accepts custom implementations)
maps cleanly onto our existing inbox semantics. The dispatcher already
has all the runtime behaviour; the missing piece is the *source*.

### Sketch

```python
# my_backlog/github_issues.py
from app.services.backlog import register_backlog_source

@register_backlog_source("github_issues")
def github_issues_source(config):
    """Yield tasks from GitHub issues matching the configured filter.

    Each yielded item is a dict {id, body, builder_hint, source_url}.
    The dispatcher converts these into local_memory.add_task calls on
    the configured builder session.
    """
    repo = config["repo"]
    label = config.get("label", "agent-ready")
    for issue in gh.list_issues(repo, labels=[label], state="open"):
        yield {
            "id": f"gh-{issue.number}",
            "body": f"{issue.title}\n\n{issue.body}",
            "builder_hint": "coder",
            "source_url": issue.html_url,
        }
```

### Dispatcher commands

```
backlog list                       # configured sources + last-pull stats
backlog pull <source>              # pull pending items into the linked builder's inbox
backlog auto <source> [<sec>]      # poll the source every N seconds and pull
backlog source add <id> <yaml>     # register a source via inline config
backlog source remove <id>         # drop a source
```

### Composition

- **MCP server**: a backlog adapter is just another MCP tool source —
  the same MCP-driven flow that drives a session can also seed it.
- **CoWork**: a cowork plan can declare a backlog source as its
  goal-input — pull the next 5 GitHub issues, plan a multi-step run
  per issue.
- **Agent rules**: a rule can refuse pulls from an unauthorized source
  (`session_tag: production` + `backlog source: external` →
  `production_local_only`).

### Status

Spec only. Held for after the MCP server ships (since that's the most
natural piping for credentials + auth flows).

These would each be substantial projects. Calling them out so the
roadmap stays honest:

| Idea                                  | Why not now                                                                          |
|---------------------------------------|--------------------------------------------------------------------------------------|
| Real-time meeting transcription       | Needs audio capture + STT engine + cross-platform mic permissions; large surface     |
| Always-on global hotkey               | OS-specific; requires elevated permissions on Windows; not aligned with desktop-app focus |
| Cloud sync of session state           | Project is local-first; cloud sync would change the trust model                      |
| Mobile companion                      | Out of scope for a desktop control layer                                             |
| Voice control                         | Needs STT; if the user wants this they layer it on top of the gateway                |

If any of these land, they'll be third-party plugins (see §3) rather
than core.

## Integration with existing layers

Every extension surface routes through the **dispatcher** — there is
no second entry point. That means:

- A plugin's command goes through the same `dispatch_input` wrapper
  that publishes bus events and respects the error vocabulary.
- An MCP tool call ends up calling `dispatch_input` internally; it
  benefits from the same approval gating, undo log, and execution
  records.
- A browser-automation step is a regular task in a builder's inbox;
  the planner doesn't need to know it's special.
- Agent rules apply pre-dispatch (validate + maybe reject) and
  post-dispatch (audit + apply consequences) using the bus.

This means an extension never has to re-implement: persistence,
approval gating, executions log, undo, theming, gateway exposure, MCP
exposure, etc. Cost of a new feature stays bounded.

## Cross-references

- Skills: [`../../learnings/README.md`](../../learnings/README.md)
- Custom commands: documented in `command_dispatcher.py` (`cmd`)
- MCP: [`./mcp-server.md`](./mcp-server.md)
- Browser: [`./browser-automation.md`](./browser-automation.md)
- Rules: [`./agent-rules.md`](./agent-rules.md)
- Templates: [`./workflow-templates.md`](./workflow-templates.md)
- CoWork: [`./cowork-mode.md`](./cowork-mode.md) (orchestration that
  often invokes browser + plugin + write surfaces in concert)
