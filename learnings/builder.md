# builder

Recipe for the `builder` skill — implementing features, writing code,
shipping changes.

## Default lane

Routes to `coding` (qwen2.5-coder:7b). Falls back to `reasoning` if
coding isn't installed.

## Workflow

1. **Read the architecture doc once.** `docs/architecture.md` —
   know which module owns what before you touch anything.
2. **Write the test first when you can.** The suite under `tests/`
   runs in ~10s on a warm cache. A failing test you can iterate
   against is faster than reasoning about a feature in the abstract.
3. **Match the surrounding style.** This codebase uses:
   - Plain `unittest` (not pytest), `mock` from `unittest.mock`
   - Type hints where they pay rent; not religiously everywhere
   - Module-level docstrings explaining *why* the file exists, not what
   - Small classes; module-level functions for stateless helpers
4. **One concern per PR-equivalent change.** Don't bundle a feature
   with an unrelated cleanup. The undo log can save you intra-session;
   a focused commit can save the next reader.
5. **Run the tests after every functional change.**
   `QT_QPA_PLATFORM=offscreen python -m unittest discover tests`.
   Don't ship green-by-luck.

## Where to put new code

| Adding…                                  | Goes in                                        |
|------------------------------------------|------------------------------------------------|
| A new dispatcher command                 | `app/services/command_dispatcher.py` + handler |
| A new durable verb                       | `app/services/goals.py`                        |
| A new session-state field                | `app/memory/local_memory.py` (`_default_context` + `_normalize_context`) |
| A new event kind                         | `app/services/gateway/bus.py` constants        |
| A new HTTP route                         | `app/services/gateway/http_server.py`          |
| A new Qt widget                          | `app/ui/`                                      |
| A theme tweak                            | `app/ui/themes_qss/foundry.qss`                |
| A new skill                              | `app/services/skill_service.py` + `learnings/<name>.md` |

## Dispatcher conventions

When you add a new command:

- Add a one-line entry to `HELP_TEXT`
- Add the bare name to `_RESERVED_CMD_NAMES` so users can't shadow it
  with `cmd add`
- Wire the route inside `_dispatch_input_inner` near related commands
  (alphabetical-ish; status/models/doctor cluster, install/uninstall
  cluster, link/dispatch/inbox cluster, etc.)
- Return shape: `{"action": "<name>", "session_id": session_id,
  "output": "<human text>", "result": <optional structured>}`
- On error, set `event["reason"] = "<canonical_tag>"` per
  `docs/design/error-vocabulary.md`

## Storage conventions

Reads: call `local_memory.get_*` directly. They go through the storage
backend's read path. No locking concerns.

Writes: never call `save_memory(load_memory(...))` — use
`local_memory._mutate(fn)` so the read-modify-write is atomic. All the
public mutators (`set_workspace`, `add_task`, `mark_task`, etc.)
already do this.

## Bus / event conventions

When your handler causes a state change worth subscribers knowing
about, publish an event:

```python
from app.services.gateway.bus import GatewayEvent, KIND_TASK, default_bus

default_bus().publish(GatewayEvent(
    kind=KIND_TASK,
    session_id=session_id,
    payload={"task_id": ..., "outcome": ..., ...},
))
```

Best-effort: wrap in try/except so a missing/broken bus never aborts
your code path. See `_publish_task_event` in `command_dispatcher.py`
for the canonical wrapper pattern.

The dispatcher already publishes `KIND_DISPATCH` after every
`dispatch_input` call automatically — you don't need to publish that
again from inside a handler.

## Tests

- Unit tests in `tests/test_<module>.py`
- Use `_isolated_memory(tmp_dir)` to give each test its own JSON
  storage so they don't stomp each other (pattern lives in
  `tests/test_coordination.py`)
- For Qt widgets, set `QT_QPA_PLATFORM=offscreen` and use the
  `_qapp()` helper. Don't try to test paint output; assert state /
  signal emission
- Mock `handle_chat` whenever you'd otherwise need an Ollama call
  running. There's never a good reason to make tests hit a real model

## Don'ts

- **Don't** edit user data on disk in tests without using a tempdir +
  `_reset_storage_for_tests`
- **Don't** call goal verbs (`dispatch_and_wait`, etc.) from the Qt
  main thread; spawn a worker
- **Don't** introduce a third storage backend without writing the
  cross-backend contract test (see `tests/test_storage.py`)
- **Don't** print to stdout for diagnostic output — use `logging`
  with the module's logger
- **Don't** commit `_design/*.png` or `*.duckdb` files; the
  `.gitignore` should cover both

## When to dispatch vs do it yourself

If you're driving as a planner agent against a builder session:

- **`dispatch <task>`** — fire-and-forget; OK when you're going to
  walk away or do something else
- **`do <task>`** — durable; blocks until terminal. Use when you need
  the result before deciding what's next
- **`do @<builder> <task>`** — single-builder durable; same as `do` if
  only one is linked

Don't use `dispatch` followed by `wait task <id>` when `do` works —
they compose to the same thing, but `do` is one round trip and reads
better.

## Reading the audit / undo log

If something seems wrong, check:
- `executions <n>` — last N dispatches with status, lane, tokens
- `executions search <text>` — substring search across history
- `tasks` — coordination tasks involving this session
- `undo` — revert the last memory mutation (intra-session only)

The execution records carry enough state to **replay** a turn — see
`retry_saved_execution` in `chat_service.py`.
