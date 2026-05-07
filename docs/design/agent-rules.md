# Design: Agent Rules

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — file-based rules (`rules/*.toml` and `rules/*.json`),
pre-dispatch evaluation, `refuse` and `forbid_provider` actions,
`rules` and `tags` dispatcher commands, `KIND_RULE` bus events — is
shipped and tested (`tests/test_rule_service.py`, 40 tests). Phase 2
(post-dispatch + bus-event evaluation, the full `then:` action set
including `dispatch` / `notify` / `audit`, `result.<json.path>`
predicates, per-session inline rules) remains planned. Read this whole
doc for the full design; Phase 1's actual surface is summarised at the
top of `app/services/rule_service.py`.

In place of the YAML examples shown below, the shipped code uses TOML
and JSON (no new dependencies; consistent with the templates surface).
The schema is otherwise identical.

A composable, declarative rule layer beneath `AGENTS.md`, mode flags,
and skill biases. Rules let you state behavioural constraints
declaratively and have the dispatcher enforce them — instead of
relying on the model to "remember" guidance from a system prompt.

Adapted from Abacus AI Desktop's "agent rules" feature.

## Why

Today behaviour is shaped through:
- **System prompts** (the `CONTROL_SYSTEM_MESSAGE`, mode messages,
  skill biases, mention expansion, recipe inclusion) — model reads
  them; may or may not honour them
- **Approval gates** (workspace reads, planned writes, planned
  browser navigations) — hard enforcement at the dispatcher
- **`AGENTS.md`** — human / agent guide; not enforced

Gap: there's no middle layer for **soft constraints that should fail
loudly when violated**. Examples:

- "After every write to `src/`, the builder must run the test suite."
- "No chat dispatch may consume more than 4000 input tokens."
- "Sessions tagged `production` cannot use cloud providers."
- "Custom commands may not invoke `delete`."

Rules are the layer for these. They're declarative (YAML), composable
(rules combine), and the dispatcher checks them on every action.

## Surface

### Rule format

```yaml
# rules/post-write-tests.yaml
id: post-write-tests
description: Run tests after any workspace write under src/
when:
  event_kind: workspace_write
  path_glob: "src/**"
then:
  dispatch: "do @test-runner run pytest tests/ -q"
  on_session: "{event.session_id}"
```

```yaml
# rules/cap-input-tokens.yaml
id: cap-input-tokens
description: Refuse chat turns over 4000 input tokens
when:
  event_kind: dispatch
  action: chat
  result.usage.input_tokens: ">4000"
then:
  refuse:
    reason: input_tokens_capped
    output: "Input over 4000 tokens; split your question or compact memory first."
```

```yaml
# rules/production-no-cloud.yaml
id: production-no-cloud
description: Production sessions never route to cloud providers
when:
  session_tag: production
  event_kind: dispatch
  action: chat
then:
  forbid_provider:
    not_in: ["ollama"]
    reason: production_local_only
```

### Rule file location

- `rules/` at the project root — globally applied
- `rules.session.<session_id>.yaml` (in storage) — per-session

### Rule semantics

Each rule has:
- `id` — unique within the rule set
- `description` — human-readable
- `when` — predicate; the rule fires when all conditions match
- `then` — action; one of `dispatch`, `refuse`, `forbid_provider`,
  `forbid_action`, `notify`, `audit`

### Trigger points

Rules are evaluated at three points in the dispatcher:

1. **Pre-dispatch** (before `_dispatch_input_inner` runs)
   - `then: refuse` short-circuits the dispatch with a `reason` event
   - `then: forbid_provider` strips the provider binding and falls
     back to local Ollama
2. **Post-dispatch** (after the dispatcher returns)
   - `then: dispatch` queues a follow-up command
   - `then: notify` emits a bus event without changing state
   - `then: audit` writes to a per-session audit log
3. **On bus events** (any KIND_DISPATCH / KIND_TASK / KIND_WORKSPACE_WRITE)
   - Same `then:` actions; lets rules react to state transitions
     anywhere in the system

### Action types

| `then.<key>`           | Effect                                                                  |
|------------------------|-------------------------------------------------------------------------|
| `dispatch: "<text>"`   | Queue a dispatcher call on a session (default: same session)            |
| `refuse: {reason, output}` | Short-circuit the action; emit the reason; never run                |
| `forbid_provider: {not_in: [...], reason}` | Force the dispatch onto an allowed provider          |
| `forbid_action: {reason, output}` | Refuse a specific action (e.g. `delete`)                       |
| `notify: {channel, message}` | Emit a notification event the UI / external subscriber can show   |
| `audit: {tag}`         | Write a structured entry to the session audit log                       |

### Predicate language

Small DSL — avoids embedding a full expression language.

```yaml
when:
  event_kind: dispatch | task | workspace_write | workspace_read | browser
  action: <action_name>           # dispatcher event action
  session_tag: <tag>              # any tag in session.context.tags
  session_id: <exact id or glob>
  path_glob: <fnmatch pattern>    # for workspace events
  status: queued | done | failed | ...
  outcome: done | failed | paused | empty
  reason: <error tag>             # see error-vocabulary.md
  result.<json.path>: <comparator>   # e.g. result.usage.input_tokens: ">4000"
```

Comparators: `==`, `!=`, `>`, `<`, `>=`, `<=`, `~=` (regex).
Multiple keys are AND'd. For OR, define multiple rules with the same
`then`.

## Examples

### "After write to src/, run the test suite"

```yaml
id: post-write-tests
when:
  event_kind: workspace_write
  path_glob: "src/**"
then:
  dispatch: "do @test-runner run pytest -q"
```

### "No chat over 4000 input tokens"

```yaml
id: cap-input-tokens
when:
  event_kind: dispatch
  action: chat
  result.usage.input_tokens: ">4000"
then:
  refuse:
    reason: input_tokens_capped
    output: "Input over 4000 tokens. Run `compact` first or split."
```

### "Production sessions stay local"

```yaml
id: production-no-cloud
when:
  session_tag: production
  event_kind: dispatch
  action: chat
then:
  forbid_provider:
    not_in: ["ollama"]
    reason: production_local_only
```

### "Notify on any failure on the production session"

```yaml
id: notify-prod-failures
when:
  session_tag: production
  outcome: failed
then:
  notify:
    channel: bus
    message: "Production task failed: {event.payload.task_id}"
```

### "Audit every workspace delete"

```yaml
id: audit-deletes
when:
  event_kind: workspace_write
  action: delete
then:
  audit:
    tag: workspace_delete
```

## Storage

Rules in YAML on disk live at `rules/*.yaml` in the project root.
Per-session rules live in `session.context.rules` as a list of inline
YAML strings (loaded + parsed lazily). Both share the same evaluator.

A small in-memory cache holds parsed rules; reloading happens on
`rules reload` or when files change (using stdlib `os.path.getmtime`
checks at dispatcher entry, not a watcher).

## Dispatcher commands

```
rules                            # list active rules + per-session overrides
rules show <id>                  # full text of one rule
rules reload                     # re-parse rules/ from disk
rules add-session <yaml>         # add an inline rule to current session
rules remove-session <id>        # drop a per-session rule
rules disable <id>               # disable globally for current process
rules enable <id>                # re-enable
```

## Bus events

- `KIND_RULE` (new) — published when a rule fires.
  Payload: `{rule_id, action, target_event}`. Subscribers see exactly
  which rule did what to which event.

## Audit log

When a rule's `then` is `audit`, the dispatcher appends a structured
entry to `session.context.audit_log`:

```python
"audit_log": [
    {
        "ts": "...",
        "rule_id": "audit-deletes",
        "tag": "workspace_delete",
        "event": {...},  # the triggering event
    },
    ...
]
```

Capped at 200 entries per session (rotation).

## Implementation phases

### Phase 1 — file-based rules + refuse + forbid_provider
- `rules/*.yaml` parsing
- Pre-dispatch evaluation
- `refuse` and `forbid_provider` actions
- `rules` CLI commands (list / show / reload / disable / enable)
- Tests

### Phase 2 — post-dispatch + bus rules
- `dispatch`, `notify`, `audit` actions
- Bus subscription (so rules fire on KIND_TASK, KIND_WORKSPACE_WRITE)

### Phase 3 — per-session rules
- `rules add-session` / `rules remove-session`
- Storage round-trip

### Phase 4 — UI
- Settings dialog: Rules tab listing active rules with toggle switches
- Per-session view in LinkedView showing inline rules

### Phase 5 — learned lane classifier ✅ Phase 5a shipped (2026-05-06)

Adopted from Local AI Workbench's
``training/classifier_trainer.py``. New
``app/services/lane_classifier.py`` ships a pure-Python
Multinomial Naive Bayes classifier (no ML deps; vocab cap +
Laplace smoothing) that learns lane assignments from the
session's own execution history.

Phase 5a (shipped):
- ``NaiveBayesClassifier`` — train / predict / serialize.
  Tokenizer drops one-char tokens; vocab cap defaults to 5000;
  Laplace smoothing α=1.
- ``train_from_executions(session_id)`` walks
  ``local_memory.get_executions``, pulls every record with a
  ``resolved_lane``, fits a fresh model.
- ``predict_lane(text, session_id)`` returns
  ``(lane, confidence)`` (or ``(None, 0.0)`` for untrained
  models / out-of-vocab inputs).
- Persistence: ``session.context["classifier"]`` carries the
  serialised model (vocab + log priors + log counts). Survives
  process restart through the existing
  ``_normalize_context`` round-trip.
- Dispatcher: ``classifier status | classifier train | classifier
  predict <text> | classifier reset``.

35 tests in ``tests/test_lane_classifier.py``: tokenizer,
``train`` happy path + edge cases (empty samples, all-same
label, vocab cap respected, idempotent re-train), ``predict``
on in-vocab + out-of-vocab + untrained model + empty text,
**round-trip via ``to_dict`` / ``from_dict`` preserves
predictions exactly**, ``train_from_executions`` collects only
records with ``resolved_lane``, persistence via
``session.context``, ``model_status``, **dispatcher
end-to-end (train → status → predict)**.

**Phase 5b shipped (2026-05-06)**: classifier predictions now
blend into the live router via the existing ``lane_biases``
parameter. New helpers in
``app/services/lane_classifier.py``:

- ``classifier_lane_biases(message, session_id, factor=4.0,
  min_confidence=0.55)`` — returns ``{predicted_lane: int}`` or
  ``{}``. Suppressed when no model is trained, no prediction is
  possible (out-of-vocab), or confidence is below
  ``min_confidence``. Bias = ``round(confidence * factor)``,
  minimum 1 above threshold.
- ``is_autoroute_enabled(session_id)`` /
  ``set_autoroute(session_id, bool)`` — persisted boolean flag
  in ``session.context["classifier_autoroute"]``. Off by default.

``chat_service.handle_chat`` reads the flag and, when enabled,
merges classifier biases on top of skill-based biases before
calling ``router.route_message``. New dispatcher subcommand:
``classifier autoroute [on|off]``.

Conservative blend strategy:
- Skill biases stay the baseline.
- Classifier biases ADD to existing scores; never overwrite.
  A 0.85-confident vote contributes ~3 points — meaningful next
  to the rule-router's keyword signals (~10 each) but not
  overwhelming.
- ``min_confidence`` floor (0.55) silently drops three-way-split
  predictions; the rule-router decides alone in those cases.

16 new tests in ``tests/test_lane_classifier.py``: bias shape
(no-model / empty-msg / strong-match / low-confidence /
threshold-suppression / factor-scaling), autoroute flag round-
trip, dispatcher on/off, and **end-to-end via ``handle_chat``
with a mocked ``route_message``** asserting the captured
``lane_biases`` carries the classifier's vote iff autoroute is on.

## Tests

- `tests/test_rules_parser.py` — YAML → Rule object validation
- `tests/test_rules_predicates.py` — every comparator works
- `tests/test_rules_actions.py` — each `then` action has the
  documented effect
- `tests/test_rules_dispatcher.py` — pre-dispatch refuse short-circuits
  the call; bus rules fire on KIND_TASK
- `tests/test_rules_per_session.py` — session-scoped rules don't leak
  to other sessions

## Anti-patterns

- **Don't put model behaviour in rules.** "Be polite" isn't a rule;
  it's a system prompt. Rules are for actions the dispatcher can
  enforce — refuses, redirects, follow-ups, audits.
- **Don't write rules that depend on model output content.** Refusing
  based on `result.response: ~="forbidden"` is brittle. Use rules for
  structural conditions (token counts, paths, action kinds).
- **Don't make rules self-trigger.** A `dispatch:` action that fires
  the same rule again loops forever. We add a per-event "rule
  evaluation depth" counter; depth >2 short-circuits with
  `reason: rule_loop`.

## Honest gaps

- **No conditional rule chains.** Each rule is independent; "if rule
  A fired, run rule B" requires writing rule B's `when:` to match
  rule A's bus event.
- **No precedence.** All matching rules fire. If two rules both
  `refuse`, the first to fire wins; the second's refusal is recorded
  but moot.
- **No regex on `path_glob`.** `fnmatch` only. Predictable; less
  expressive.
- **No remote rules.** Rules live on disk in this project's `rules/`
  folder; we don't fetch from a server.

## Cross-references

- `error-vocabulary.md` — rules emit canonical reason tags
- `extensibility.md` — rules are one of five extension surfaces
- `cowork-mode.md` — cowork executes plans; rules can constrain what a
  cowork plan is allowed to do (refuse plans that violate)
- `workspace-writes.md` — rule examples that audit / require tests
  after writes
