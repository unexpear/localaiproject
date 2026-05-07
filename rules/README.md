# Rules

Declarative dispatcher constraints. Drop a `.toml` or `.json` file in
this directory; the dispatcher loads it on first access and on
`rules reload`. See `docs/design/agent-rules.md` for the full design.

## Format

```toml
id = "kebab-case-id"
description = "human-readable one-liner"

[when]
event_kind = "dispatch"            # always "dispatch" in Phase 1 pre-dispatch
action     = "delete"              # the first verb of the user's input
session_id = "production"          # exact match
session_tag = "production"         # any tag in session.context.tags
text_match = "rm -rf"              # substring match (cheap)
text_regex = "\\bsudo\\b"          # regex match (compiled once at parse)
path_glob  = "src/**"              # fnmatch on `write <path>` / `delete <path>`

[then.refuse]                      # short-circuits the dispatch
reason = "invalid_argument"        # canonical error tag (see error-vocabulary.md)
output = "delete is forbidden by policy"
```

```toml
[then.forbid_provider]             # Phase 1 stub: emits a KIND_RULE bus
not_in = ["ollama"]                # event but does NOT change routing.
reason = "production_local_only"   # Active when chat-providers ships.
```

All keys in `[when]` are AND'd. To express OR, write multiple rules
with the same `[then]`.

## Hard rules

- **The `rules`, `help`, `exit`, `doctor`, `status`, `undo`, `redo`
  commands are EXEMPT** from rule evaluation. A misbehaving rule can't
  lock you out of the controls used to disable it.
- **Phase 1 only evaluates pre-dispatch** — rules don't fire on
  workspace_write, task lifecycle, or other bus events yet. Phase 2
  adds post-dispatch + bus-event evaluation.
- **Phase 1 supports `refuse` and `forbid_provider`** only. Other
  actions (`dispatch`, `notify`, `audit`) are spec'd but not
  implemented.

## Disable a rule without editing files

```
rules disable my-overzealous-rule
rules enable my-overzealous-rule    # bring it back
```

Disabled state survives `rules reload` but not a process restart.

## Apply tags to a session

```
tags add production
tags                                # show current tags
tags remove production
tags clear
```

A rule with `session_tag = "production"` only fires when the session
has that tag set.
