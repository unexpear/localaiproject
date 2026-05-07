# Goal Verbs API

`app/services/goals.py` — durable, polling-based verbs that loop until a
real-world condition is reached. Layer over the primitives in
`local_memory`.

## When to use what

| Need                                                | Verb                                                       |
|-----------------------------------------------------|------------------------------------------------------------|
| Queue a task and forget about it                    | `local_memory.add_task` (primitive)                        |
| Queue a task and block until it finishes            | `goals.dispatch_and_wait`                                  |
| Re-dispatch until the builder emits `<task-complete>` | `goals.iterate_until_complete`                          |
| Send the same task to every linked builder + wait   | `goals.fan_out_and_wait`                                   |
| Drain a builder's whole inbox right now             | `goals.drain_inbox`                                        |
| Wait for a specific task id to terminal             | `goals.wait_for_task`                                      |
| Wait for a builder to be idle                       | `goals.wait_for_inbox_empty`                               |
| Wait for any session-context predicate              | `goals.wait_for_state`                                     |

## Conventions

Every verb takes:
- `timeout: float | None` — seconds; `None` means wait forever
- `cancel_event: threading.Event` — set the event to abort within one
  poll interval
- `poll_interval: float = 0.1` — seconds between checks

Verbs return on **any** of: success, terminal failure, timeout,
cancellation. They never raise on operational outcomes (timeouts and
failures are returned data, not exceptions). They raise `ValueError`
only on bad input shape.

## Terminal statuses

```python
TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})
```

Plus a special case: a task in `in_progress` with `result =
"(awaiting workspace approval)"` is treated as **terminal-for-now** so
wait verbs don't hang when an approval is pending.

## Reference

### `dispatch_and_wait(planner, text, *, builder=None, timeout=None, cancel_event=None, runner=None)`

Queue a task and process the builder's inbox until that task is
terminal.

```python
final = goals.dispatch_and_wait("arch", "refactor _pull_with_retry")
assert final["status"] in {"done", "failed", "skipped"} or final["result"] == "(awaiting workspace approval)"
```

If the planner has multiple linked builders, you must pass `builder=`
to disambiguate. If only one builder is linked, that one is used.

`runner` is the per-task callable the verb invokes; defaults to the
dispatcher's `_run_one_inbox_task`. Pass a custom runner in tests to
avoid spinning up real model calls.

Behavior on `paused` outcome (workspace approval needed): the verb
returns the task as it stands (still `in_progress` with the
approval-pending marker). The user must `approve` or `deny`, then call
`dispatch_and_wait` again or run `inbox next` manually.

### `iterate_until_complete(planner, text, *, builder=None, max_iterations=5, timeout=None, runner=None)` ✅ shipped

Repeatedly re-dispatch the same task to the same builder until either:

- the builder's response contains a `<task-complete>` marker line, OR
- `max_iterations` is reached, OR
- the task ends in a terminal `failed` / `skipped` status, OR
- the user denies a workspace approval mid-iteration

```python
final = goals.iterate_until_complete(
    "arch",
    "scaffold a new Python lib named foo and verify pytest passes",
    max_iterations=8,
)
print(final["iterations"], final["completed"], final["last_status"])
```

Returns a dict ``{iterations, completed, last_status, last_result,
task_ids: [...]}`` summarising the run. Each iteration is a fresh
`dispatch_and_wait` call; the builder accumulates context across
iterations via its session history (today's existing behaviour).

Distinct from `dispatch_and_wait` because:
- Today's `do` ends after one builder turn even if the work isn't done
- Multi-iteration tasks would otherwise need the planner to re-dispatch
  the same task by hand, which loses the "until complete" semantic

Hard rules:
- Builder MUST opt in by emitting `<task-complete>` on its own line at
  the end of any response. Anything else means "I need another turn."
- Iteration count is capped at 20 absolute (safety) regardless of
  `max_iterations`.
- Approval pauses inside an iteration return immediately with
  `last_status = "paused"`; the caller resumes by re-invoking after
  approval.

Dispatcher equivalent: ``iterate <task>`` and ``iterate @<builder>
<task> [--max <n>]``. See `tests/test_goals.py::IterateUntilCompleteTests`
for canned-runner test cases.

### `fan_out_and_wait(planner, text, *, timeout=None, cancel_event=None, runner=None)`

Dispatch the same text to every linked builder, then drain each in
turn. Returns the final task dicts in dispatch order.

```python
results = goals.fan_out_and_wait("arch", "run the tests", timeout=120)
for task in results:
    print(task["builder_session"], task["status"], task["result"])
```

Builders are processed **serially** (one inbox at a time). Parallel
fan-out is queued for a future round; the storage `mutate(fn)` lock
already serialises concurrent writers correctly so the invariant work
is done.

`timeout` covers the **whole** fan-out, not per builder. Each builder
gets the remaining budget when its turn comes.

### `drain_inbox(builder, *, max_iterations=100, timeout=None, cancel_event=None, runner=None, stop_on_pause=True)`

Pull and run tasks from `builder`'s inbox until empty / stopped.
Returns one outcome dict per task processed, in order:

```python
outcomes = goals.drain_inbox("coder", timeout=300)
done   = sum(1 for o in outcomes if o["outcome"] == "done")
failed = sum(1 for o in outcomes if o["outcome"] == "failed")
```

Stops on (whichever comes first):
- inbox empty
- cancellation
- timeout
- a task pauses for approval **and** `stop_on_pause=True` (default)
- `max_iterations` reached (safety cap)

### `wait_for_inbox_empty(builder, *, timeout=None, cancel_event=None, poll_interval=0.1)`

Block until `builder` has no queued or in-progress tasks. Returns
`True` if the inbox emptied, `False` on timeout / cancel.

A task in approval-pending state counts as "drained for now" so this
won't hang waiting on a stuck approval.

### `wait_for_task(task_id, *, timeout=None, cancel_event=None, poll_interval=0.1)`

Block until the task is `done` / `failed` / `skipped` (or paused for
approval). Returns the final task dict, or:
- the latest non-terminal snapshot if `timeout` elapsed
- `None` if the task id was never found

### `wait_for_state(session_id, predicate, *, timeout=None, cancel_event=None)`

Block until `predicate(session_context_dict)` returns truthy.

```python
ok = goals.wait_for_state(
    "default",
    lambda ctx: ctx.get("workspace_path") is not None,
    timeout=10,
)
```

Predicate exceptions are treated as "not yet" so a flaky check doesn't
abort the wait.

## CLI surface

| Command                                | Maps to                              |
|----------------------------------------|--------------------------------------|
| `do <task>`                            | `fan_out_and_wait` (or `dispatch_and_wait` if 1 builder) |
| `do @<builder> <task>`                 | `dispatch_and_wait(builder=<builder>)` |
| `wait inbox <session> [<sec>]`         | `wait_for_inbox_empty`                |
| `wait task <task-id> [<sec>]`          | `wait_for_task`                       |

## Bus events

When a goal verb calls into the dispatcher, the dispatcher publishes
`KIND_DISPATCH` events as usual. Task lifecycle transitions inside
`_run_one_inbox_task` publish `KIND_TASK` events. Subscribers can react
to either.

Future round will replace polling inside the wait verbs with bus
subscriptions, lowering latency from `poll_interval` (~100ms default)
to ~ms. Today the bus events serve UI live-refresh; the verbs poll.

## Cancellation

```python
import threading
cancel = threading.Event()

# In one thread
threading.Timer(5.0, cancel.set).start()

# In another
final = goals.dispatch_and_wait("arch", "long task",
                                 cancel_event=cancel)
# Returns within ~poll_interval after `cancel.set()`.
```

Cancellation is responsive: the verb checks the event before each poll
and waits on it (rather than `time.sleep`) so it returns promptly.

## Threading rules

These verbs **may block for a long time**. Don't call them from the Qt
main thread; spawn a worker (`threading.Thread`,
`concurrent.futures.ThreadPoolExecutor`, `QThread`).

The CLI dispatcher is fine because it's not on the GUI loop.

## Composition example

A "do this until it actually succeeds, max 3 retries" wrapper:

```python
def do_with_retries(planner, text, *, max_retries=3, timeout=120):
    last = None
    for _ in range(max_retries):
        last = goals.dispatch_and_wait(planner, text, timeout=timeout)
        if last and last["status"] == "done":
            return last
    return last  # gave up
```

## Test posture

`tests/test_goals.py` covers each verb plus dispatcher-level
integration. The pure helpers (`_is_terminal`, `_deadline`,
`_sleep_with_cancel`) have direct tests too. Custom runners are used
throughout to avoid Ollama dependency.
