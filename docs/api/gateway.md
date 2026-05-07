# Gateway API

Two layers, one event model. The in-process bus is always running; the
HTTP gateway is opt-in.

- **In-process EventBus** (`app/services/gateway/bus.py`) — Qt panes
  subscribe and live-refresh on dispatches.
- **HTTP+SSE gateway** (`app/services/gateway/http_server.py`) — bridges
  the bus to network clients on 127.0.0.1.

## EventBus reference

```python
from app.services.gateway import (
    EventBus, GatewayEvent,
    KIND_DISPATCH, KIND_TASK, KIND_GATEWAY,
    default_bus,
)
```

### Subscribe

```python
def on_event(event: GatewayEvent) -> None:
    print(event.kind, event.session_id, event.payload)

sub_id = default_bus().subscribe("default", on_event)
# or wildcard — see every event
sub_id = default_bus().subscribe(EventBus.ANY, on_event)
```

### Unsubscribe

```python
default_bus().unsubscribe(sub_id)  # returns True if dropped
```

### Publish (typically internal)

```python
default_bus().publish(GatewayEvent(
    kind=KIND_DISPATCH,
    session_id="default",
    payload={"action": "chat", "output_preview": "...", "result": {...}},
))
```

`publish` returns the count of callbacks invoked. Subscriber exceptions
are swallowed (logged debug); one flaky listener can never abort
delivery to others. Callbacks fire **outside** the bus lock, so a slow
subscriber doesn't block other publishes.

### Event kinds

| Kind            | When                                              | Payload                                                          |
|-----------------|---------------------------------------------------|------------------------------------------------------------------|
| `KIND_DISPATCH` | After every `dispatch_input(...)` call            | `{action, output_preview (≤200 chars), result}`                  |
| `KIND_TASK`     | On every task lifecycle transition                | `{task_id, outcome, status, planner_session, builder_session, error}` |
| `KIND_GATEWAY`  | (Reserved) gateway start/stop                     | `{...}`                                                          |

Task events publish on **both** the planner's and the builder's session
ids so subscribers on either side can react.

### Threading

`publish()` runs synchronously on the publisher's thread. If you need
to touch Qt widgets from a callback, marshal via a Qt signal. Both
`LinkedView` and `LocalAIQtWindow` use the pattern:

```python
class MyWidget(QWidget):
    bus_event_signal = Signal(object)
    def __init__(self):
        ...
        self.bus_event_signal.connect(self._on_bus_event_ui_thread)
        self._sub_id = default_bus().subscribe(default_bus().ANY, self._enqueue)
    def _enqueue(self, event):
        self.bus_event_signal.emit(event)
    def _on_bus_event_ui_thread(self, event):
        ...  # safe to touch widgets here
```

### Test hooks

```python
default_bus().clear()
default_bus().subscriber_count(session_id=None) -> int
reset_default_bus_for_tests()  # drops the singleton
```

---

## HTTP+SSE gateway

Stdlib-only server (`http.server.ThreadingHTTPServer`). Always binds
to `127.0.0.1`. Optional bearer-token auth via the
`LOCALAI_GATEWAY_TOKEN` env var.

### Lifecycle

```
gateway start [host:port]    # default 127.0.0.1:7780
gateway stop
gateway status
```

`gateway start 9000` → `127.0.0.1:9000`. `gateway start host.local:8000`
→ binds to whatever resolves; **don't** override the host unless you
know what you're doing — this is a loopback-only design.

### Auth

```bash
export LOCALAI_GATEWAY_TOKEN=$(openssl rand -hex 16)
```

If set, every request (except `/health`) must send:

```
Authorization: Bearer <token>
```

Without the env var, all loopback callers are trusted.

### Endpoints

#### `GET /health`

Liveness probe. Always public.

```
200 OK
{"ok": true, "name": "localaiproject-gateway"}
```

#### `POST /dispatch`

Run any dispatcher command on a session. Returns the dispatcher's
event dict verbatim.

```
POST /dispatch
Content-Type: application/json
Authorization: Bearer <token>   (if auth is on)

{"session_id": "default", "text": "do refactor router"}
```

```
200 OK
{
  "action": "do",
  "session_id": "default",
  "output": "Fan-out + wait · 1 task(s): 1 done, 0 failed, 0 paused\n  ✓ task_a3b1...  done  refactor router  → ...",
  "result": [ { "id": "task_a3b1...", "status": "done", ... } ]
}
```

Errors:
- `400 {"error": "invalid json body"}`
- `400 {"error": "missing 'text'"}`
- `401 {"error": "unauthorised"}`

#### `GET /sessions/<id>/state`

Read-only snapshot. The cheapest way for a remote agent to plan its
next move.

```
GET /sessions/default/state

200 OK
{
  "session_id": "default",
  "context": { "workspace_path": "...", "active_skills": [...], "mode": "plan", ... },
  "recent_history": [ {"role": "user", "content": "...", "timestamp": "..."}, ... ],
  "inbox": [ /* queued tasks for this session as a builder */ ],
  "in_progress_task": { ... } | null,
  "linked_partners": { "as_planner": [...], "as_builder": [...] }
}
```

`recent_history` is the last 20 entries.

#### `GET /events?session=<id>`

Server-Sent Events stream. One frame per `GatewayEvent`. Use
`session=*` to receive every event regardless of session.

```
GET /events?session=default
Accept: text/event-stream

: connected

data: {"kind":"dispatch","session_id":"default","payload":{"action":"help","output_preview":"Commands\n- help: ...","result":null},"timestamp":1764850000.123}

: keepalive

data: {"kind":"task","session_id":"default","payload":{"task_id":"task_a3b1...","outcome":"done","status":"done","planner_session":"arch","builder_session":"default","error":null},"timestamp":1764850001.456}
```

Slow clients get **events dropped** (bounded queue of 200 per
subscription) rather than blocking the bus. The 15-second `: keepalive`
comment keeps proxies / clients honest.

### Quickstart

```bash
# In one terminal
python -m app.main
> gateway start
Gateway started on http://127.0.0.1:7780
Auth: disabled (loopback only)

# In another
$ curl -s http://127.0.0.1:7780/health | jq
{"ok": true, "name": "localaiproject-gateway"}

$ curl -sX POST http://127.0.0.1:7780/dispatch \
       -H 'content-type: application/json' \
       -d '{"session_id":"default","text":"help"}' | jq .action
"help"

$ curl -N http://127.0.0.1:7780/events?session=default
: connected
data: {"kind":"dispatch", ...}
```

### Python client snippet

```python
import json, urllib.request

def dispatch(session_id, text, token=None):
    req = urllib.request.Request(
        "http://127.0.0.1:7780/dispatch",
        data=json.dumps({"session_id": session_id, "text": text}).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

print(dispatch("default", "doctor"))
```

### Hard rules

- Always loopback. **Never** expose the gateway publicly. There is no
  TLS termination in this code path.
- Auth is bearer-only. No OAuth, no per-route ACLs.
- The gateway is per-process. Two `python -m app.main` instances each
  get their own gateway+bus singletons. They share storage on disk
  (DuckDB or JSON), nothing else.
- Slow SSE clients drop events; the bus stays healthy.

### Caveats

- No request rate-limiting. Trusted-loopback assumption.
- No partial-write streaming on `POST /dispatch` — the dispatcher
  returns the full event when done. Use the SSE `/events` channel for
  progress.
- No batched dispatch. One command per POST.

### Test posture

`tests/test_gateway.py` covers framing, end-to-end POST/GET against an
ephemeral port, token-auth on/off, lifecycle (`start` / `stop` /
`status`), bus pub-sub semantics, and the dispatcher → bus publish hook.
