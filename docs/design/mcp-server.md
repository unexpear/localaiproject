# Design: MCP Server Wrapper

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — stdio transport, JSON-RPC 2.0 framing, initialize/ping/
shutdown handshake, tools/list, tools/call, resources/list,
resources/read with `sessions://list`, **23 tools** covering inspect
(`status`, `models`, `doctor`, `executions`, `tasks`, `link_status`,
`get_session_state`) and action (`chat`, `dispatch`, `do`, `iterate`,
`inbox_next`, `link`, `set_workspace`, `set_mode`, `approve`, `deny`,
`read_file`, `search_workspace`, `notes_show`, `notes_append`,
`cowork_plan`, `cowork_run`) — is shipped and tested
(`tests/test_mcp_server.py`, 32 tests). Phase 2 (HTTP transport,
streaming progress notifications, all remaining tools, full resource
catalog) and Phase 3 (gateway-internal SSE + bus subscriptions)
remain planned.

Run as a subprocess: ``python -m app.mcp``. Inspect from inside the
dispatcher: ``mcp tools`` / ``mcp config``.

Wrap localaiproject's dispatcher as a Model Context Protocol (MCP)
server so any MCP-aware client (Claude Code, OpenCode, future agents)
can use it as a tool source out of the box.

## Why this is the high-leverage item

We already speak HTTP+SSE (`docs/api/gateway.md`). MCP is the protocol
*every* agent already knows. By wrapping the same dispatcher surface
in MCP, we:

- Get free integration with Claude Code (one entry in `.mcp.json`)
- Get free integration with OpenCode and any other MCP host
- Let agents discover our capabilities without bespoke client code
- Enable a localaiproject session in one machine to be a tool source
  for an agent on another machine (over MCP transports)

## Design

### Server location

`app/mcp/server.py` — a new `app/mcp/` package, separate from
`app/services/gateway/` because MCP is a different protocol with
different framing (JSON-RPC over stdio or HTTP, plus tool/resource
metadata).

### Tool surface

Each high-value dispatcher command becomes one MCP tool. Group by
intent:

**Inspect tools** (read-only, no approval needed):
- `status` — runtime + lanes + version
- `models` — lane / model fit + install state
- `doctor` — full control-plane snapshot
- `executions` — recent dispatches; supports `query`, `lane`, `status`,
  `since`, `limit` filters
- `tasks` — all coordination tasks involving a session
- `link_status` — who's linked to whom
- `get_session_state(session_id)` — same shape as the HTTP `/sessions/<id>/state`

**Action tools**:
- `chat(session_id, text)` — send a chat turn; returns the reply
- `dispatch(session_id, text, builder=None)` — queue a task
- `do(session_id, text, builder=None, timeout_s=60)` — durable verb;
  blocks until terminal
- `inbox_next(session_id)` / `inbox_auto(session_id)` — process tasks
- `set_workspace(session_id, path)`
- `set_mode(session_id, mode)`
- `link(planner_session, builder_session)`
- `approve(session_id)` / `deny(session_id)`
- `read_file(session_id, path)` — with explicit `auto_approve` flag
- `search_workspace(session_id, query)`
- `install_model(role_or_model)` / `uninstall_model(role_or_model)`

**Resource surface**:
- `sessions://list` — returns `{sessions: [...]}`
- `session://default/history` — returns recent chat turns
- `session://default/executions` — returns recent execution records
- `session://default/notes` — once notes buffer ships

### Schema

Each tool advertises a JSON Schema for its arguments. The dispatcher
already validates input → MCP just gives a typed envelope. Example:

```json
{
  "name": "do",
  "description": "Dispatch a task to one or more linked builders and wait until each is terminal. Use this when you need the result before proceeding.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "session_id": {"type": "string", "description": "The planner session"},
      "text": {"type": "string", "description": "The task text"},
      "builder": {"type": "string", "description": "Optional explicit builder; omit to fan out to all linked builders"},
      "timeout_s": {"type": "integer", "default": 60}
    },
    "required": ["session_id", "text"]
  }
}
```

### Transports

MCP supports stdio and HTTP. Ship stdio first (Claude Code's default);
add HTTP later as a thin layer over the existing HTTP gateway.

```
$ python -m app.mcp.server         # stdio mode (default)
$ python -m app.mcp.server --http  # HTTP mode (delegates to gateway internals)
```

### Reuse with the HTTP gateway

The HTTP gateway and MCP server share:
- The dispatcher (`dispatch_input`)
- The bus (subscriptions for streaming)
- Session-state snapshot logic

Factor a small `app/services/gateway/operations.py` module out of
`http_server.py` that contains the concrete operations
(`dispatch_one(session_id, text)`, `state_snapshot(session_id)`, etc.).
Both transports thin-wrap it.

### Error mapping

Use the standard error vocabulary (`docs/design/error-vocabulary.md`)
in MCP tool error responses. MCP defines an error envelope; we put the
`reason` tag in `error.data.reason` so clients can pattern-match.

```json
{
  "error": {
    "code": -32000,
    "message": "Workspace approval required",
    "data": {"reason": "approval_required", "pending_action": {...}}
  }
}
```

### Auth

Same `LOCALAI_GATEWAY_TOKEN` env var. For stdio transport, auth is
implicit (caller has shell access). For HTTP transport, bearer token
mirrors the existing gateway.

## Implementation phases

### Phase 1 — minimal viable ✅ shipped (extended scope)
What landed exceeds the originally-scoped 5+3 tool floor:

- `app/mcp/protocol.py` — JSON-RPC 2.0 framing (line-delimited JSON)
  + dispatch with structured error envelopes
- `app/mcp/tools.py` — 23 tools registered, each a thin wrapper around
  `dispatch_input`; per-tool input schemas; resource registry
- `app/mcp/server.py` — handlers for `initialize` /
  `notifications/initialized` / `ping` / `shutdown` / `tools/list` /
  `tools/call` / `resources/list` / `resources/read`
- `app/mcp/__main__.py` — `python -m app.mcp` stdio entry point
- Dispatcher command: `mcp [info] | mcp tools | mcp config | mcp serve`
- Tool-level exceptions become `isError: true` results (NOT JSON-RPC
  error envelopes) so the client can show them to the user
- Resource: `sessions://list` returns the dispatcher's session names
- 32 tests in `tests/test_mcp_server.py`

**Phase 1.5 (also shipped, 2026-05-04):** browser tools added — 6 MCP
tools wrap the read-only browser surface (`browser_open`,
`browser_navigate`, `browser_screenshot`, `browser_get_text`,
`browser_get_html`, `browser_close`). 4 additional tests verify the
end-to-end flow including the per-origin approval gate. Total: **29
MCP tools** registered.

### Phase 2 — full surface
- All inspect + action tools
- All resources
- Error vocabulary integration

### Phase 3 — HTTP transport
- `--http` flag; delegates to gateway server bootstrap
- Streamable tool calls (use SSE for `do` progress updates)
- Documentation for `.mcp.json` config

## .mcp.json snippet (target)

```json
{
  "mcpServers": {
    "localaiproject": {
      "command": "python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "C:\\Users\\micha\\Desktop\\localaiproject\\.claude\\worktrees\\quizzical-diffie-68ae1d"
    }
  }
}
```

## Testing

- Unit-test each tool through a minimal MCP test harness (no real
  client, no real Ollama)
- Integration-test by spawning the server in a subprocess and driving
  it with a real Python MCP client library (e.g. `mcp` package on PyPI)
- Resource subscriptions tested by setting up a bus subscriber and
  verifying it receives forwarded events

## Open questions

- Do we expose `undo` / `redo` as tools? They're per-process;
  cross-process callers might be surprised.
- Should `notes` get its own resource subscription so a remote agent
  can stream the notes buffer?
- For long-running `do`/`inbox_auto`, do we want intermediate
  `notification`s instead of one final response? MCP's progress
  notification spec covers this; worth implementing.

## Non-goals

- Network deployment — MCP servers usually run colocated with the
  client. We're not building a hosted service.
- Replacing the HTTP gateway. Both ship; clients pick the protocol
  they speak.
- Auto-approval of workspace actions. MCP tools that hit `read` /
  `search` still create a pending approval the user must resolve;
  that's the security model.

## Why not just expose the HTTP gateway as MCP via a generic adapter?

Because:
- The dispatcher's command vocabulary is rich; a generic adapter would
  reduce it to "POST a string". MCP wants typed tools.
- Each command has its own argument schema. We want clients to see
  `do(session_id, text, builder, timeout_s)` not `dispatch("do @x …")`.
- Bus events benefit from MCP's progress notifications, which need
  per-tool integration.
- Tool descriptions are agent-readable; they should be tailored, not
  generic.
