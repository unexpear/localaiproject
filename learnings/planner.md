# planner

Recipe for the `planner` skill — breaking work down, sequencing tasks,
dispatching to builders, watching the queue.

## Default lane

Routes to `reasoning` (llama3.1:8b). Planning is reasoning-shaped, not
code-shaped.

## Workflow

1. **Query state before planning.** A plan formed without knowing the
   current state is theatre. Run these first:
   ```
   doctor               # what's the runtime / lane / workspace state
   tasks                # what's already been dispatched
   link status          # who can I dispatch to
   ```
2. **Decompose into ≤3 dispatchable units per round.** A planner that
   queues 50 tasks at once is asking for one of them to fail and
   nobody to notice. Three is small enough to watch, big enough to
   make progress.
3. **Dispatch with the right verb.**
   - **`dispatch <task>`** — fire-and-forget; you'll come back later
   - **`do <task>`** — durable; blocks until terminal. Use when the
     next decision depends on the result.
   - **`do @<builder> <task>`** — single builder + wait
4. **Watch the result, then re-plan.** Don't queue round N+1 before
   round N's results are in. The planner is the reflection step in
   the loop; honour it.
5. **Mark a task `done` only when it's actually done.** If the
   builder's reply is "I sketched the code, please review" — that's
   not done. Re-dispatch a verification task or do it yourself.

## When to fan out vs target

| Want                                                    | Use                              |
|---------------------------------------------------------|----------------------------------|
| Same task, multiple perspectives                        | `dispatch <task>` (fan-out)      |
| Different sub-tasks per builder                         | `dispatch @<b1> X` then `@<b2> Y`|
| One builder is the only one with the right context      | `do @<builder> <task>`           |
| Quick sanity check across all builders                  | `do <task>` (fan-out + wait)     |

## Reading the inbox

Each builder has its own inbox. As planner, you see all tasks you
dispatched via:

```
tasks                     # all tasks involving you
tasks queued              # only what's still pending somewhere
tasks done                # what's finished
```

Each builder consumes its own queue with `inbox` / `inbox next` /
`inbox auto`. You can't (and shouldn't) reach into another builder's
inbox; the dispatch is the abstraction.

## Stop conditions

Stop dispatching when:
- The user has asked you to wait
- Three consecutive tasks have failed for the same reason — that's
  a signal you're not really planning, you're hoping
- A workspace approval is pending — the user has to act, not you
- The relevant builder's inbox already has 5+ queued tasks — let it
  drain before piling on

## Auto-feedback to the planner

When a builder marks a task `done`, the planner gets:

```
[task task_xxx from <builder>] DONE: <result preview>
```

appended to its own history as a system note. So your context grows
naturally as work completes. You see DONE markers; failures show as
FAILED.

If your context is filling with DONE notes faster than you can plan
around them, run `compact` to fold older notes into the structured
summary.

## Coordinating with multiple planners

Multiple planners can target the same builder. The builder's inbox
is FIFO across all dispatches. Don't assume your tasks run in order
relative to another planner's — only relative to your own previous
dispatch on the same builder.

If you need ordering across planners, that's coordination outside
this codebase — pick one canonical planner per workflow.

## When to use `wait` verbs

```
wait task <id>           # block until that specific task is terminal
wait inbox <session>     # block until builder is idle
```

Use these in scripted / CLI contexts where you really need to block.
In an interactive chat, you usually just look at the next system note
when it arrives — no explicit wait.

The durable verbs (`do`, `do @<b>`) compose dispatch + wait in one
step; prefer them over `dispatch <x>` then `wait task <id>`.

## Anti-patterns to call out

- **Plan-then-immediately-execute.** Planning is its own act; if you
  always plan and then do, the plan provides no value. Plan, share
  the plan, get a green light, then execute.
- **Dispatching speculation as work.** "Maybe try X" doesn't belong
  in a builder's inbox. Either decide it's worth doing or don't queue
  it.
- **One mega-task.** "Refactor the whole router and add tests and
  update docs" is three tasks. Queue them separately so you can see
  which step failed.
- **Ignoring failures.** `tasks failed` should be an empty list most
  of the time. If it's not, stop dispatching and figure out why.

## Plan vs Build mode

If you're a planner, your default mode is `plan`:
- You analyse, design, decompose
- You don't propose file edits in your own session

The builders you dispatch to should be in `build` mode:
- They make the change
- Their workspace actions are still approval-gated

To set:
```
mode plan                # for the planner session
session <builder>; mode build; session <yours>   # for each builder
```

## When to compact

Run `compact` when your history has more than ~20 turns of routine
DONE notes. Compaction folds them into structured summary fields
(facts, preferences, open_tasks) and frees up your context budget.

The compaction service handles this automatically when chat memory
gets too large; you can also trigger it manually.

## Audit trail

Everything is logged. To understand what happened:

```
executions               # last 10 dispatches in detail
executions search <text> # search by lane / status / since / message
execution <id>           # full record + flow graph for one dispatch
retry <id>               # rerun an execution from its saved state
```

Useful when a builder's result was surprising — pull `execution <id>`
to see exactly what context they had.
