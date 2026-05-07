# Templates

Saved + parameterised dispatcher-command sequences. Drop a `.toml` or
`.json` file in this directory; the dispatcher loads it on first
access and on `template reload`.

## Format

```toml
id = "kebab-case-id"
description = "human-readable one-liner"

[[params]]
name = "version"
description = "semver string"
required = true

[[params]]
name = "author"
default = "{user.name}"           # built-in expansion happens at run time

[[steps]]
kind = "command"                   # only kind in Phase 1
cmd = "do @release-bot cut version {{version}} for {{author}}"

[[steps]]
kind = "command"
cmd = "git tag v{{version}}"
require_success = true             # abort if the prior step set a `reason`
```

## Substitution

Two layers happen at run time, in order:

1. **`{{name}}` references** — resolved against the params dict (CLI
   `k=v` pairs merged with the template's declared defaults). An
   unresolved `{{name}}` aborts the run with `reason: template_param_missing`.
2. **`{user.name}` / `{user.email}` / `{date}` / `{session_id}`**
   built-ins expand at dispatch time. `{user.*}` reads `git config`;
   missing values substitute as empty strings.

## Running

```
> template list                                      # show all templates
> template show python-lib-skeleton                  # inspect one
> template run python-lib-skeleton package_name=foo  # execute
> template reload                                    # re-parse the directory
```

## Phase 1 limitations

- Only the `command` step kind is supported. To run a cowork plan from
  a template, use `cmd = 'cowork plan "..."'` — the dispatcher's plan
  command produces an approval-required pause that templates honour
  natively (the run halts at the pause; user approves; user re-runs
  the template or the cowork command directly).
- No template-invokes-template chaining (Phase 2).
- No `!`<dispatcher-command>`` expressions (Phase 2).
- No per-session inline templates (Phase 2).

See `../docs/design/workflow-templates.md` for the full design.
