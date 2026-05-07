# Design: Standard Error Vocabulary

**Status: Phase 1 shipped, Phase 2 mostly shipped · 2026-05-03**

Phase 1 (constant + guard test) and most of Phase 2 (handler wiring) landed.
Still planned: chat_service approval emit, runtime_client error mapping,
MCP error envelope (Phase 4 — depends on the MCP server itself).

A small fixed set of `reason` tags emitted on every dispatcher event
that carries an error. Lets agents pattern-match without parsing
English.

## Why

Today error reporting is heterogeneous:
- Some commands return `{"output": "Could not …"}` with no machine tag
- Some return `{"error": str(exc)}`
- Some throw exceptions
- Workspace approval has its own `pending_action` envelope
- `inbox` returns "empty" / "paused" via the `outcome` key

For an agent driving the system, this is annoying — it has to grep
strings to know if "Could not install" means "no such model" or "Ollama
offline" or "version too old".

Standardise: every event that fails or refuses an action carries
`event["reason"] = "<tag>"` from a fixed enum below. The text in
`event["output"]` stays human-readable; the tag is for machines.

## Tag enum

```
no_workspace          The session has no pinned workspace
path_not_found        A path doesn't exist inside the workspace
outside_workspace     A path tries to escape the workspace root
binary_file           A path resolves to a binary file (read/search refused)
is_directory          A path resolves to a directory where a file was expected

approval_required     A workspace read/search needs user approval first
approval_pending      Action queued, awaiting `approve` / `deny`

no_link               A `dispatch`/`do` issued with no linked builder
unknown_builder       `@<builder>` referenced an unlinked builder
inbox_empty           `inbox next` / `inbox auto` ran with empty queue

unknown_target        `install` / `uninstall` got an unknown role/model
runtime_offline       Ollama unreachable
model_unavailable     Lane resolves but model isn't installed and can't pull now
version_too_old       Model needs newer Ollama than is installed
hardware_too_small    Model is too big for available RAM (`is_model_too_big`)

cancelled             Long-running op aborted via cancel event
timeout               Wait verb / dispatch hit a timeout
already_running       Action couldn't start because a prior instance is in flight

invalid_argument      Command parsed but arguments failed validation
unknown_command       Unrecognised dispatcher command
internal_error        Unexpected exception bubbled up; check logs

# --- Mode + workspace-write extensions (see workspace-writes.md) ---
mode_required         Action requires the session to be in a specific mode
                      (the message names which: "mode_required: build")
write_cap_reached     Per-session soft cap on file writes hit
write_backup_stale    Undo skipped: file changed between propose and undo
symlink_unsupported   A write proposed creating a symlink (not allowed)

# --- Chat provider extensions (see chat-providers.md) ---
provider_unknown          Lane bound to a provider id we don't know
provider_unauthenticated  Provider needs creds; env / keyring / 1Password empty
provider_unreachable      Provider responded with network / 5xx errors
provider_quota_exceeded   Provider returned a rate-limit or quota error

# --- Skill extensions (see browser-automation.md, extensibility.md) ---
skill_unavailable         A skill required to handle the command isn't installed
origin_not_approved       Browser action attempted on a non-whitelisted origin
eval_refused              `browser eval` denied (always-prompt for raw JS)
playwright_not_installed  Browser skill needed; Playwright extras absent

# --- Rule + template extensions (see agent-rules.md, workflow-templates.md) ---
rule_loop                 Rule re-fired itself past depth cap (loop guard)
input_tokens_capped       Rule refused dispatch over the input-token threshold
production_local_only     Rule refused cloud routing on a `production` session
template_param_missing    Template run missing a required parameter
template_recursion        Template invoked itself directly or via cycle
template_unknown_step     Template step `kind` not in {command, cowork_plan, ...}
```

These are the canonical names. Don't invent new ones without updating
this doc and the test that pins the set.

## Where it goes

The dispatcher's event dict gains an optional `reason` field:

```python
{
  "action": "install",          # already there
  "session_id": "default",      # already there
  "output": "Could not install vision: Ollama 0.12.7+ required.",
  "reason": "version_too_old",  # NEW
  "result": {...}               # already there
}
```

Successful events leave `reason` unset. The HTTP gateway forwards it
verbatim. The MCP error envelope places it in `error.data.reason`.

## Examples

### Workspace approval

```python
{
  "action": "chat",
  "session_id": "default",
  "output": "[approval required] read app/services/router.py",
  "reason": "approval_required",
  "result": {
    "approval_required": True,
    "pending_action": {...},
  }
}
```

### Install fails on hardware

```python
{
  "action": "install",
  "session_id": "default",
  "output": "Could not install vision (qwen3-vl): Model too large for available RAM.",
  "reason": "hardware_too_small",
  "result": {
    "role": "vision",
    "model": "qwen3-vl",
    "success": False,
    "output": "Model too large for available RAM.",
    "warnings": [],
  }
}
```

### Inbox empty

```python
{
  "action": "inbox",
  "session_id": "coder",
  "output": "Inbox is empty — nothing to run.",
  "reason": "inbox_empty",
}
```

### Unknown command

```python
{
  "action": "chat",         # falls through to chat fallback
  "session_id": "default",
  "output": "I don't recognise the command 'thingify'.",
  "reason": "unknown_command",
}
```

(For now the chat fallback IS the unknown-command behaviour; future:
distinguish "user wanted to chat" from "user typo'd a command".)

## Implementation plan

### Phase 1 — guard test ✅ shipped
- `app/services/error_vocabulary.py` defines the canonical frozenset
  + per-tag named constants
- `command_dispatcher.py` re-exports `ERROR_REASONS`
- `tests/test_error_vocabulary.py` pins the enum verbatim and lints
  every `"reason": "<literal>"` in dispatcher source against it

### Phase 2 — wire emit sites ✅ mostly shipped
Walk every handler in `command_dispatcher.py`. For every error or
"refused" return path, set `event["reason"] = "<tag>"`. Touch points
(roughly):

| File / handler                            | Tags emitted                                                  | Status   |
|-------------------------------------------|---------------------------------------------------------------|----------|
| `_handle_workspace_command`               | `path_not_found`, `invalid_argument`                          | ✅ shipped |
| `_handle_read_command` / `_handle_search` | `no_workspace`, `invalid_argument`, `approval_required`       | ✅ shipped |
| `_handle_approval_command` / `_handle_deny_command` | `invalid_argument` (nothing pending)                | ✅ shipped |
| `install` / `uninstall` (inline in dispatcher) | `unknown_target`, `version_too_old`, `hardware_too_small`, `runtime_offline`, `model_unavailable`, `invalid_argument` | ✅ shipped (via `_classify_install_result`) |
| `_handle_link_command`                    | `invalid_argument` (self-link, empty)                         | ✅ shipped |
| `_handle_dispatch_command`                | `no_link`, `unknown_builder`, `invalid_argument`              | ✅ shipped (via `_classify_dispatch_value_error`) |
| `_handle_inbox_command`                   | `inbox_empty`, `invalid_argument`                             | ✅ shipped |
| `_handle_tasks_command`                   | `invalid_argument` (unknown status)                           | ✅ shipped |
| `_handle_do_command`                      | `no_link`, `unknown_builder`, `invalid_argument`              | ✅ shipped |
| `_handle_wait_command`                    | `invalid_argument`, `timeout`, `unknown_target`               | ✅ shipped |
| `_handle_gateway_command`                 | `already_running`, `invalid_argument`, `internal_error`       | ✅ shipped |
| `_handle_lsp_command`                     | `invalid_argument`                                            | ✅ shipped (basic; deeper diagnose paths still planned) |
| `_handle_cmd_command`                     | `invalid_argument`                                            | ✅ shipped |
| `chat_service.handle_chat`                | `approval_required` on the approval-needed return             | 🚧 planned |
| `runtime_client.pull_model`               | finer-grained `runtime_offline` / `model_unavailable` mapping | 🚧 planned |

### Phase 3 — propagate to chat & approval

`handle_chat` should set `reason = "approval_required"` on the
approval-needed return. `pull_model` failure modes should map to
`runtime_offline` / `model_unavailable` / `version_too_old` consistently
(today they return `(False, "<text>")`).

### Phase 4 — propagate to MCP

The MCP server (see `mcp-server.md`) places `reason` in the JSON-RPC
error envelope's `data` field.

## What this changes for AGENTS.md

The "standard error vocabulary" section in `AGENTS.md` references this
doc. Once Phase 2 is in, that section becomes the authoritative quick
reference; this design doc retires to "we did the design here" status.

## What it doesn't change

- The text in `event["output"]` is still freeform and human-readable.
  Don't try to parse it.
- Successful events don't get `reason`. Absence of the key means
  "everything fine".
- We don't add new HTTP status codes. The gateway still returns 200 +
  the dispatcher event; the agent looks at `event["reason"]` to
  classify.

## Test posture

- One source-of-truth test pins the enum (`tests/test_error_vocabulary.py`)
- Per-handler tests assert the right tag on the relevant error paths
- A lint-style test scans dispatcher source for `event["reason"] = "..."`
  literals and asserts each one is in the canonical set (catches
  typos / new tags creeping in undocumented)
