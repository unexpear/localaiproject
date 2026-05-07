# Design: Streaming Chat

**Status: Phase 1 shipped · 2026-05-06**

A staged rollout of token-by-token streaming for chat turns —
modern chat UIs render the model's output as it generates rather
than waiting for the full response. Today every chat turn is
atomic: the user types, the dispatcher blocks on the model, then
the full text appears at once. Streaming would make the agent feel
2-5× more responsive without changing what it actually does.

This is **net-new design work**, not a phase increment of an
existing doc. None of the surveyed external projects (`ttttt`,
`personal app`) shipped streaming, so we get to choose the shape.

## Why

The existing batch flow has three drawbacks the user feels:

1. **Visible latency on long answers.** A 2000-token answer takes
   ~10s on a local 8B model. Today the user stares at a spinner
   for the whole 10s. Streaming would surface the first tokens in
   ~500ms.
2. **No early-cancellation feedback loop.** If the user sees the
   model going off the rails in the first sentence, they have to
   wait for the full response before they can correct it.
3. **No partial-progress for the orchestration layer.** CoWork
   sub-tasks could update their status board with first-line
   excerpts as they generate, instead of binary "queued" → "done."

## Hard rules

- **Streaming MUST be opt-in per-call.** Every existing call site
  uses `dispatch_chat` (batch) and must keep working unchanged.
  Adding a `stream_callback` arg or a parallel
  `dispatch_chat_stream` function — never overload the existing
  one.
- **Cloud providers without native streaming MUST degrade
  gracefully.** Phase 1 falls back to batch-then-emit-as-one
  `done` event for OpenAI / Anthropic. Callers writing streaming
  loops still work; they just don't see chunks for those
  providers.
- **Fenced-block detection requires the FULL text.** The chat-flow
  parser (`parse_fenced_writes`) needs to see complete `file=path`
  blocks before it can validate them. Streaming UIs render chunks
  AS IF they were the final text, but the typed-patch / approval
  gate runs only after the terminal `done` event when the full
  buffered string is available.
- **Terminal event guaranteed exactly once.** Every streaming
  generator MUST emit exactly one `done` OR `error` event as the
  final yield. Callers can write `for event in stream:` loops with
  a simple `break` on `kind != "chunk"`.

## Surface

### Event shape

A streaming generator yields dicts. Three event kinds:

```python
{"kind": "chunk", "content": "<fragment>"}      # 0..N times
{"kind": "done",  "content": "<full-text>",     # exactly once on success
                  "raw": {<provider-final-event>}}
{"kind": "error", "error": "<reason-tag>",      # exactly once on failure
                  "content": "<partial>"}        # what we accumulated
```

The `done` event's `content` is the concatenation of every
preceding chunk — callers that want batch behaviour can wait for
the single terminal event without accumulating themselves. The
`error` event's `content` carries whatever the model produced
before the failure (may be empty).

### Phase 1 — provider primitive ✅ shipped

- `app/models/runtime_client.py::infer_chat_stream(model, *, message=None, messages=None)`
  — Ollama-native stream via `/api/chat` with `stream: true`.
  Tolerates the alternate `response` field shape (older models),
  silently skips malformed JSON lines (transport hiccups), drops
  empty content chunks.
- `app/services/chat_providers.py::dispatch_chat_stream(provider_id, model, messages, options=None)`
  — registry-aware streaming dispatcher. Routes `ollama` to the
  native streamer above; routes other providers to the existing
  batch `chat_fn` and synthesizes a single `done` event so
  callers get a uniform interface.
- 14 tests in `tests/test_streaming.py` covering the contract.

### Phase 2 — native cloud streaming (planned)

- OpenAI `/v1/chat/completions` with `stream: true` (uses SSE format
  `data: {...}\n\n` per chunk). Switch from `urllib` to a
  chunked-read pattern.
- Anthropic `/v1/messages` with `stream: true` (similar SSE shape
  with content_block_delta events).
- Both fall through `dispatch_chat_stream` without changing the
  yielded event schema.

### Phase 3 — chat_service streaming entry point (planned)

- New `handle_chat_stream(message, session_id="default", ...)`
  generator that:
  - Runs all the same routing / approval / mention-expansion
    logic as `handle_chat`.
  - Yields `kind="chunk"` events as the model produces.
  - Yields exactly one `kind="done"` event with the full
    `result` dict (same shape as today's `handle_chat` return).
  - Records the assistant message + execution record only AFTER
    the `done` event (we need the full text for fenced-block
    detection + write proposals).
- Existing `handle_chat` becomes a thin wrapper that drains the
  generator and returns the final `done.result`.

### Phase 4 — Qt UI integration (planned)

- `qt_main_window` transcript widget gains an "in-progress"
  bubble that updates incrementally as `chunk` events arrive on
  the bus.
- New bus event kind `KIND_CHAT_CHUNK` for cross-thread delivery
  (the streaming generator runs on a worker thread; UI updates
  marshal through the existing `bus_event_signal`).
- Cancel button wires to a `CancelToken` that the streaming
  generator checks between chunks (graceful early stop, server
  request stays open until next chunk boundary).

### Phase 5 — orchestration integration (planned)

- CoWork sub-tasks emit their first-line excerpt to
  `KIND_COWORK` bus events as they stream, so `CoWorkView`
  updates the sub-task table mid-execution.
- `iterate_until_complete` can short-circuit on streaming —
  if the model emits `<task-complete>` mid-stream, stop reading
  the rest of the response.

## Implementation phases

### Phase 1 ✅ shipped (this doc)

- `infer_chat_stream` in `runtime_client.py`
- `dispatch_chat_stream` in `chat_providers.py`
- 14 unit tests

### Phase 2 (planned) — cloud providers

- Native OpenAI / Anthropic streaming. Same event schema; no
  caller changes.

### Phase 3 (planned) — chat_service entry point

- `handle_chat_stream` generator. Maintains the existing
  approval / record-keeping invariants.
- Existing `handle_chat` → thin wrapper around the streaming
  variant for callers that don't care.

### Phase 4 (planned) — Qt UI

- Live token rendering in transcript.
- Cancel button hooks `CancelToken`.
- Bus events for cross-thread updates.

### Phase 5 (planned) — orchestration

- CoWork progress events as sub-tasks stream.
- `iterate_until_complete` early-termination on `<task-complete>`.

## Honest gaps

- **No mid-stream cancellation in Phase 1.** The Ollama HTTP
  request stays open until the model finishes or errors. Phase 4
  adds a `CancelToken` check between chunk yields; Phase 4+ adds
  request-level cancel via `requests`'s connection-close
  semantics.
- **Fenced-block latency.** Even with streaming, write proposals
  appear at the END of the response (we need the full text).
  Future versions could parse fenced blocks incrementally as they
  close — but that's invasive.
- **No streaming for chat history records.** Phase 3 records the
  assistant's full message after the `done` event; the
  intermediate stream isn't replayable. Replays will appear as
  batch responses.

## Cross-references

- `chat-providers.md` — the existing batch dispatch this layer
  parallels.
- `cancel.md` (planned) — `CancelToken` semantics for mid-stream
  cancel (Phase 4).
- `cowork-mode.md` Phase 5+ — orchestration integration.
