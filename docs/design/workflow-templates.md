# Design: Workflow Templates

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — file-based templates (`templates/*.toml` and `templates/*.json`)
+ `template list / show / run / reload` commands + parameter substitution +
the `command` step kind — is shipped and tested
(`tests/test_template_service.py`, 36 tests). Two starter templates ship
in `templates/` (morning-standup, python-lib-skeleton). Phase 2 (per-session
inline templates, dedicated `cowork_plan` step kind, dispatcher-command
expressions, template chaining) remains planned.

Saved, parameterised cowork plans and custom-command sequences. Lets
you reuse "set up a new Python lib", "do my morning standup", "release
a new version" without rewriting the plan each time.

Adapted from Abacus AI Desktop's "workflow / playbook" concept. We
build it on top of the shipped custom-command surface and the planned
cowork plan format — no new primitives, just a saved-and-parameterised
layer on top.

## Why

Today, two power-user surfaces exist (or are about to):

- **Custom commands** (shipped) — alias for a single prompt
- **CoWork plans** (planned, see `cowork-mode.md`) — JSON
  decomposition of a goal into sub-tasks

Both are one-shot. If your morning routine is "compact, then list
overnight task failures, then dispatch a triage builder", you have to
type the three commands every morning. If your "ship a new version"
ritual is a 6-step cowork plan, you write the 6 steps from scratch
each time.

Workflow templates give those rituals a name and parameters.

## Hard rules

- **Composition over magic.** A template is a sequence of dispatcher
  commands or a saved cowork plan. Nothing a template does couldn't be
  done by typing the commands manually.
- **Approval gates still apply.** A template that includes a
  `write` step still triggers the workspace-write approval flow on
  each step. Templates don't bypass approvals.
- **Parameters are explicit.** A template declares its parameters up
  front; running the template prompts for missing ones. No "guess what
  I meant" inference.
- **Templates can't call themselves.** No recursion in v1. A template
  that invokes another template is fine; a template that invokes
  itself is rejected with `reason: template_recursion`.

## Surface

### Template format

```yaml
# templates/python-lib-skeleton.yaml
id: python-lib-skeleton
description: Set up a fresh Python lib (package, README, pyproject, smoke test)
params:
  - name: package_name
    description: PyPI / import name (snake_case)
    required: true
  - name: author
    description: Author name for pyproject
    default: "{user.name}"
steps:
  - kind: cowork_plan
    plan:
      goal: "Set up a python lib skeleton named {{package_name}}"
      sub_tasks:
        - id: sub_1
          task: "Create src/{{package_name}}/__init__.py and src/{{package_name}}/__main__.py"
          builder: coder
          acceptance: "Both files exist and import cleanly"
        - id: sub_2
          task: "Draft README.md with title '{{package_name}}' and author {{author}}"
          builder: writer
          depends_on: []
          acceptance: "README has title, install, usage sections"
        - id: sub_3
          task: "Write pyproject.toml for package {{package_name}}, author {{author}}"
          builder: coder
          depends_on: []
          acceptance: "pyproject parses with `python -m build --check`"
        - id: sub_4
          task: "Add tests/test_smoke.py with one passing assertion"
          builder: coder
          depends_on: [sub_1]
          acceptance: "pytest -q exits 0"
```

```yaml
# templates/morning-standup.yaml
id: morning-standup
description: Compact memory; list overnight task failures; dispatch triage
params: []
steps:
  - kind: command
    cmd: "compact"
  - kind: command
    cmd: "tasks --status failed --since 18h"
  - kind: command
    cmd: "do @triage-bot review the failed tasks above and propose fixes"
```

```yaml
# templates/release.yaml
id: release
description: Cut a new release (tests, version bump, changelog, tag, push)
params:
  - name: version
    description: Semver version (e.g. 0.5.2)
    required: true
  - name: changelog_entry
    description: One-line changelog summary
    required: true
steps:
  - kind: command
    cmd: "do @test-runner pytest -q"
    require_success: true
  - kind: cowork_plan
    plan:
      goal: "Cut release {{version}}"
      sub_tasks:
        - id: bump
          task: "Bump version to {{version}} in pyproject.toml and __init__.py"
          builder: coder
          acceptance: "Both files reflect {{version}}"
        - id: changelog
          task: "Prepend '## {{version}}\\n- {{changelog_entry}}\\n' to CHANGELOG.md"
          builder: writer
          acceptance: "CHANGELOG starts with the new entry"
  - kind: command
    cmd: "git tag v{{version}} && git push --tags"
```

### Template file location

- `templates/` at the project root — globally available
- `session.context.templates` — per-session inline templates (rare;
  mainly for one-off rituals you don't want to commit)

### Step kinds

| `kind`         | Effect                                                                  |
|----------------|-------------------------------------------------------------------------|
| `command`      | Run a single dispatcher command (`cmd:` field)                          |
| `cowork_plan`  | Submit a pre-built cowork plan; user approves it as today              |
| `template`     | Invoke another template by id (`id:` field, `params:` map)              |
| `wait`         | Pause for human input (`prompt:` field)                                 |

`require_success: true` on a step aborts the template if the step
ends with a non-`done` outcome. Default is `false` (continue).

### Parameter substitution

Mustache-style `{{name}}` substitution into strings. Substitution
happens **before** the step is dispatched. Unresolved parameters fail
with `reason: template_param_missing`.

Special expansion variables:
- `{user.name}` — git user.name
- `{user.email}` — git user.email
- `{date}` — today's date (ISO)
- `{session_id}` — current session id

### Dispatcher-command expressions (planned, sandcastle-inspired)

A second, lazier substitution layer that runs *at step dispatch time*
rather than at template-load time. Syntax: ``!`<dispatcher-command>` ``

```yaml
steps:
  - kind: command
    cmd: "do @triage-bot review these failures: !`tasks --status failed --since 24h`"
```

The expression `` !`tasks --status failed --since 24h` `` invokes the
dispatcher recursively, captures the resulting event's `output` field,
and splices it into the prompt before the step is dispatched.

Hard rules:
- **Recursion-safe.** A dispatcher-command expression can't invoke
  another template (would explode). Refused with
  `reason: template_unknown_step` if the expression resolves to a
  `template run` call.
- **Read-only.** Expressions can only call commands that don't mutate
  state — `tasks`, `executions`, `link status`, `inbox`, etc. The
  template loader maintains an allowlist; non-allowlisted commands
  refuse with `reason: invalid_argument`.
- **Bounded output.** Captured output is clipped to 4 KB; longer
  output gets truncated with `… (output truncated at 4096 bytes)`
  appended. Catches "expression returns the entire executions log."

Useful for:
- "Send the agent the current state of X" without baking it into the
  template at write time
- Chaining commands where one's output feeds another's prompt
- Daily-briefing templates that auto-include "what happened overnight"

Phase 2 addition (after the literal `{{name}}` layer is solid).

### Dispatcher commands

```
template list                       # all templates (global + per-session)
template show <id>                  # full text of one template
template run <id> [k=v ...]         # run a template; prompts for missing params
template save <id> <yaml>           # save inline template to per-session storage
template remove <id>                # remove a per-session template
template reload                     # re-parse templates/ from disk
```

Examples:

```
> template list
templates/morning-standup        Compact memory; list overnight failures; triage
templates/python-lib-skeleton    Set up a fresh Python lib
templates/release                Cut a new release

> template run python-lib-skeleton package_name=mylib
[approval required] cowork plan: Set up a python lib skeleton named mylib
   sub_1  coder   Create src/mylib/...
   sub_2  writer  Draft README.md ...
   …
> approve
… (cowork run starts)

> template run release
template requires param: version
> template run release version=0.5.2 changelog_entry="Add browser skill"
running step 1/3: do @test-runner pytest -q
… (passes)
[approval required] cowork plan: Cut release 0.5.2
…
```

## Storage

Per-session templates live in `session.context.templates`:

```python
"templates": {
    "rituals_morning": {
        "yaml": "id: rituals_morning\\nsteps: ...",
        "saved_at": "...",
    },
    ...
}
```

File-based templates load from `templates/` on dispatcher startup;
`template reload` re-parses on demand. Same in-memory cache pattern as
`agent-rules.md`.

## Bus events

`KIND_TEMPLATE` (new):
- Payload: `{template_id, run_id, step_index, step_total, status}`
- Emitted on: `started`, `step_done`, `step_failed`, `completed`,
  `cancelled`

A subscriber (the Qt UI) renders a small "running template" widget
showing progress.

## Run state

Each template invocation creates a `template_run` entry:

```python
"template_runs": [
    {
        "id": "tr_a1b2c3",
        "template_id": "release",
        "params": {"version": "0.5.2", "changelog_entry": "..."},
        "session_id": "default",
        "status": "running|done|failed|cancelled",
        "started_at": "...",
        "completed_at": "...",
        "current_step": 2,
        "step_results": [
            {"step": 0, "outcome": "done", "task_id": "task_xxx"},
            {"step": 1, "outcome": "done", "task_id": "cw_yyy"},
            ...
        ],
    }
]
```

Cap at 50 runs per session (rotation).

## Cancellation

`template cancel` aborts an in-flight run:
- The current step's cancel event is fired
- Subsequent steps are skipped
- Run status set to `cancelled`
- `KIND_TEMPLATE` `cancelled` event emitted

A template that includes a cowork plan: cancellation cascades to the
cowork run too.

## Composition with other surfaces

| Surface           | Relationship                                                              |
|-------------------|---------------------------------------------------------------------------|
| Custom commands   | A template `command` step can invoke a custom command (`cmd: "/triage"`)  |
| CoWork plans      | A template `cowork_plan` step submits a plan; same approval flow          |
| Agent rules       | Rules apply to template-dispatched commands like any other dispatch       |
| Workspace writes  | Steps that write files trigger the per-file approval flow                 |
| MCP server        | `template run <id>` is a regular dispatcher command — exposed as a tool   |

## Implementation phases

### Phase 1 — file-based templates + command step ✅ shipped
- `templates/*.toml` and `templates/*.json` parsing (TOML preferred for
  readability; both round-trip through `parse_template`)
- `template list / show / run / reload` dispatcher commands
- Parameter substitution: `{{name}}` resolved against supplied + default
  params; `{user.name}` / `{user.email}` / `{date}` / `{session_id}`
  built-ins expanded at dispatch time
- `command` step kind: dispatched via `dispatch_input`; the resulting
  event is captured in the run summary
- Approval-pause aware: a step that returns `reason: approval_required`
  halts the run with `stopped_reason: "paused"` (rest of steps skipped)
- `require_success: true` on a step aborts the run on any non-empty `reason`
- Hard caps: 16 params per template, 32 steps per template
- Two starter templates ship in `templates/`: `morning-standup.toml`,
  `python-lib-skeleton.toml`

Phase 1 deliberately ships only the `command` step kind. To run a cowork
plan from a template, set `cmd = 'cowork plan "..."'` — the cowork
plan command itself returns the approval-required pause natively, and
template runs honour that pause.

### Phase 2 — per-session templates + chaining + dispatcher-command expressions
- `template save / remove` for inline templates
- `template` step kind (one template invokes another, depth-limited to 3)
- `wait` step kind (pause for human input)
- ``!`<dispatcher-command>` `` expressions in step strings (read-only
  command allowlist, 4 KB output cap, refuses recursion into template run)

### Phase 3 — bus events + cancellation
- `KIND_TEMPLATE` events
- `template cancel` cascading cancellation
- Run state + history

### Phase 4 — UI
- Templates panel in Qt UI: list + run + edit
- Live progress widget during a run
- "Save current cowork plan as template" button on the cowork view

### Phase 5 — SKILL.md interop (planned, Hermes-inspired)

Hermes Agent ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
ships a "skills" mechanism: when the agent solves a hard problem it
writes a portable `SKILL.md` document — open standard compatible with
[agentskills.io](https://agentskills.io) — that future runs can attach
to grow the agent's repertoire over time. Our templates are 90% the
same shape (parameterised dispatcher-command sequences); aligning with
the SKILL.md format gets us community interop for free, plus
Markdown-with-frontmatter is more legible to humans + LLMs reading
the file as context than TOML/JSON.

### File format

```markdown
---
name: scaffold-python-lib
description: Scaffold a fresh Python library with package + tests
triggers: ["scaffold a python lib", "new python project"]
required_tools: ["write", "do"]
lanes: ["coding"]
params:
  - name: package_name
    required: true
  - name: author
    default: "{user.name}"
---

# Scaffold a Python library

This skill creates a complete Python package skeleton.

## Steps

```toml
[[steps]]
kind = "command"
cmd = "do @coder create src/{{package_name}}/__init__.py"

[[steps]]
kind = "command"
cmd = "do @writer draft README.md for {{package_name}} by {{author}}"
```

## Why this works

The cowork lane handles parallel execution; the writer lane drafts
prose; the coder lane writes pytest tests last so all earlier files
exist.
```

### Implementation

`template_service` parses both TOML and SKILL.md formats into the
same internal `Template` dataclass. The frontmatter maps directly
to existing fields; the fenced TOML block in the body provides the
step list (so we don't need a second step-format).

New dispatcher commands:

```
skill install <path>           # parse + register a SKILL.md (path only initially)
skill install <url>            # behind an explicit allowlist (mirrors browser-skill)
skill list                     # all installed skills with triggers
skill suggest <query>          # match user input against `triggers` field
```

### Honest concerns

- The agentskills.io standard is young; commit to it as a *parsed*
  format we map to our `Template` dataclass, so the standard can
  evolve without forcing internal rewrites.
- URL install is supply-chain risk; gate behind the same per-origin
  approval whitelist used by `browser-automation.md`.

## Tests

- `tests/test_template_parser.py` — YAML → Template object validation;
  malformed YAML rejected
- `tests/test_template_params.py` — parameter substitution; missing
  required param fails; `{user.*}` resolves
- `tests/test_template_run.py` — step execution flows: command step
  dispatches and waits; cowork_plan step pauses for approval
- `tests/test_template_recursion.py` — self-invocation rejected;
  depth-3 chain works
- `tests/test_template_cancel.py` — cancellation aborts in-flight
  step; cascades to cowork sub-run

## Anti-patterns

- **Don't make templates conditional.** No "if step 1 succeeded, do
  step 2 else step 3" branching in v1. Conditional logic belongs in
  agent rules or in cowork plan dependencies.
- **Don't substitute into shell commands without quoting.** A
  `template run release version="; rm -rf /"` must not run `rm`. The
  parameter substitution layer escapes shell-special characters before
  splicing into a command string.
- **Don't share templates as live links.** A template is a YAML file
  in your repo. Sharing means committing it. We don't fetch templates
  from a URL (supply-chain risk).
- **Don't auto-run templates on a schedule.** Templates are
  user-invoked. If you want "run morning-standup at 9am", that's a
  cron job that pipes the command into the gateway — not a template
  feature.

## Honest gaps

- **No template versioning.** Editing a template in `templates/` is a
  destructive update. Use git for history.
- **No cross-template parameter passing.** A template that invokes
  another template passes static params; you can't say "use the output
  of step 2 as a param to the next template."
- **No dry-run mode.** `template run` actually runs. A future round
  could add `template run <id> --dry` that prints what would happen
  without dispatching.
- **No template marketplace.** Templates live in your repo. We don't
  ship a curated catalog (could in a future round, but trust /
  supply-chain story has to be solid first).

## Open questions

- Should `template show` render the substituted form (with current
  param defaults applied) or the raw YAML? Probably raw, with a
  `--resolved` flag for the substituted view.
- Should templates be able to declare a required mode (`build` /
  `plan`) and fail fast if the session is in the wrong mode? Yes,
  probably. Phase 2 addition.
- Auto-discovery of templates from `~/.localaiproject/templates/` (user-wide,
  not per-project)? Useful for personal rituals (morning-standup) that
  span projects. Defer until someone asks.

## Cross-references

- `cowork-mode.md` — the `cowork_plan` step kind defers to this format
- `agent-rules.md` — both YAML, both load on startup, similar caching
- `extensibility.md` — templates are one of the extension surfaces
- `error-vocabulary.md` — adds `template_param_missing`,
  `template_recursion`, `template_unknown_step`
