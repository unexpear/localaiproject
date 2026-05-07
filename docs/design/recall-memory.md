# Design: Recall Memory (searchable session history)

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — `recall <query>` dispatcher command + `recall` MCP tool
+ relative-time parser (`24h` / `7d` / `1w` / ISO timestamps) + Python-
side substring matcher across all session histories with citation
output — is shipped and tested (`tests/test_recall_service.py`,
35 tests). Backend-agnostic: works against JSON or DuckDB storage
unchanged. Phase 1.5 (DuckDB FTS swap), Phase 2 (cowork + executions
sources), and Phase 4 (Qt UI search bar) remain planned.

A first-class `recall <query>` surface that lets the user (and the
model, when function-calling is enabled) search across historical
chat turns + workspace operations + cowork runs without leaving the
dispatcher.

Adapted from Letta's
([letta-ai/letta](https://github.com/letta-ai/letta)) "recall memory"
tier, which makes the full conversation history searchable from
inside the agent loop. Our local-first interpretation: the same
storage we already write to (DuckDB or JSON) becomes searchable
through a single dispatcher command + an MCP tool.

## Why

Today our state is fully introspectable but not query-able:

- `history [n]` shows the last N messages — linear scroll only
- `executions search <text>` filters by lane/status/since — execution
  records, not chat turns
- `tasks` lists coordination tasks — terminal state only

The gap: "what did I ask the assistant about that auth bug last week?"
or "find the cowork plan that produced the failing test" requires
the user (or another agent) to scroll. We have the data; we don't
have a query surface.

## Hard rules

- **No new dependencies.** DuckDB ships with FTS5; the JSON backend
  gets a Python-side Boolean substring matcher (slow but correct,
  warned about above ~1000 sessions).
- **Read-only.** `recall` never mutates state. It queries existing
  storage; cited results render in the dispatcher with provenance
  (session id, timestamp, message id).
- **No embeddings in v1.** Vector search drags in a model dependency
  we don't want; FTS gets us 80% of the way for a single-user log.
- **Approval gating preserved.** `recall` itself doesn't need
  approval (it's reading data the user already owns); but if a result
  references a workspace path the user wants to *open*, that goes
  through the existing read approval gate.

## Surface

### Dispatcher commands

```
recall <query>                  # FTS over chat history; default 10 hits
recall <query> --since 24h      # bound by recency
recall <query> --session <id>   # bound by session
recall <query> --kind chat|task|cowork
recall task <task-id>           # exact id lookup; shows full thread
recall cowork <run-id>          # exact id lookup; shows plan + synthesis
```

Output: one citation block per hit, e.g.

```
[recall · 3 hits for "auth bug"]
1. session=default · 2026-04-21T15:30Z · user
   "investigate the auth bug in the JWT decoder"
   ↳ assistant: "the issue is `decode_token` doesn't handle…"
2. session=arch · 2026-04-22T09:12Z · cowork cw_a1b2c3d4
   goal: "fix auth bug + write regression test"
   status: done · 5 sub-tasks
3. session=default · 2026-04-25T11:00Z · execution exe_…
   lane=reasoning · "verify the fix landed; jwt tests pass"
```

### MCP tool

`recall(query, *, since, session, kind, limit)` — exposed alongside the
existing `executions` tool. External agents (Claude Code) can search
the user's localaiproject history through the MCP gateway.

## Storage

### DuckDB backend (preferred)

Use DuckDB's [FTS extension](https://duckdb.org/docs/extensions/full_text_search.html):

```sql
PRAGMA create_fts_index('chat_messages', 'id', 'content');
PRAGMA create_fts_index('cowork_runs', 'id', 'goal', 'synthesis');
```

`recall` runs:

```sql
SELECT id, session_id, role, content, ts,
       fts_main_chat_messages.match_bm25(id, ?) AS score
FROM chat_messages
WHERE score > 0
  AND (? IS NULL OR session_id = ?)
  AND (? IS NULL OR ts > ?)
ORDER BY score DESC, ts DESC
LIMIT ?;
```

Index is rebuilt incrementally on `_mutate` for chat tables (cheap;
DuckDB FTS supports `update_fts_index`).

### JSON backend (fallback)

Iterate every session's history; substring match on lowercased
content; rank by recency. Document the slow-path warning above ~1000
sessions; emit `KIND_DISPATCH` with `reason: recall_slow_path` when
the JSON backend is in play AND the corpus exceeds the threshold.

## Implementation phases

### Phase 1 — chat-history search ✅ shipped
What landed (`app/services/recall_service.py`,
`app/services/command_dispatcher.py`,
`app/mcp/tools.py`, `tests/test_recall_service.py`):

- `parse_relative_time(spec)` accepts `"24h"` / `"7d"` / `"1w"` /
  `"30m"` / `"2y"` / ISO timestamps; returns an ISO 8601 string with
  trailing `Z` matching `local_memory._now()`'s format
- `recall(query, *, since, session, limit)` returns a list of
  `RecallHit` dataclasses; tokenises the query (whitespace-split,
  lowercased), substring-matches across each session's history,
  scores by unique-terms-matched + per-occurrence bonus, sorts by
  score then recency
- `format_hits(hits, query)` renders a citation block (`session=…
  · timestamp · role` headers + content excerpt + `↳` follow-up
  preview)
- Dispatcher command `recall <query> [--since 24h] [--session <id>]
  [--limit N]` — flags parsed greedily from the tail; bad `--since`
  / `--limit` values surface as `invalid_argument`
- MCP tool `recall(query, since?, session?, limit?)` registered
  alongside the existing 29 (now 30 total); structuredContent
  includes the full citation list
- Phase 1 backend-agnostic: works on JSON storage today, will work
  on DuckDB unchanged. FTS swap is a Phase 1.5 optimisation that
  preserves the same return shape

### Phase 2 — multi-source recall
- Add cowork runs + execution records as additional FTS sources
- `recall <query> --kind` filter

### Phase 2a — BM25 retrieval over workspace docs ✅ shipped (2026-05-06)

Adopted from Local AI Workbench's
``retrieval/indexer.py``. New ``app/services/retrieval_index.py``
ships a pure-Python BM25 index:

- ``BM25Index`` — standard k1=1.5, b=0.75 BM25 with Robertson IDF
  and term-frequency saturation. Vocabulary cap (default 50K) +
  doc cap (default 5K) bound memory; vocabulary eviction drops
  the least-common postings when over cap.
- ``index_workspace(path)`` — walks the pinned workspace, indexes
  text files by extension whitelist (``.py / .md / .txt / .js /
  .ts / .json / ...``), skips binary (null-byte probe) + oversize
  (default 256 KB) + common build/VCS dirs (``.git``,
  ``__pycache__``, ``node_modules``, ``dist``, etc.).
- Per-session in-memory cache (``_INDEXES`` dict guarded by a
  module lock). Lazy-build: first ``retrieve <query>`` call walks
  the disk; subsequent queries reuse the index.
- Dispatcher: ``retrieve <query>`` (lazy build + search) /
  ``retrieve build`` (force rebuild) / ``retrieve status`` /
  ``retrieve clear``.

Output format mirrors the legacy ``recall`` command: top-N hits
with path, BM25 score, and matched terms, plus a 120-char
preview of the first 240 chars of each doc.

38 tests in ``tests/test_retrieval_index.py``: tokenizer
(stopwords, lowercase), BM25 math (exact match wins, **rare
term scores higher than common term**, multi-term sum,
**term-frequency saturation** asserts 50× tf does NOT 50× score),
mutation (add / replace / remove / max_docs cap), workspace
walker (extensions, dot dirs + ``node_modules`` excluded,
**oversize files skipped**, **binary detection by null byte**,
max_files cap), per-session cache (lazy-build, rebuild replaces,
clear drops), dispatcher round-trip (status / lazy-build query
/ build / clear).

### Phase 2b — prompt-slot integration ✅ shipped (2026-05-06)

New ``retrieved_docs_slot`` in ``app/services/prompt_assembly.py``
auto-injects the top-5 BM25 hits as a system message on every chat
turn. Conservative gating:

- Returns None (no slot) when no workspace is pinned.
- Returns None when the BM25 index isn't cached yet — **the user
  opts in by running ``retrieve build`` once after pinning a
  workspace** (avoids a surprise multi-second index pause on the
  first chat turn).
- Returns None when the user message is shorter than 8 chars
  (greetings / yes-no don't merit retrieval).
- Returns None when the message has no in-vocab terms.

When fired, emits a single system message at priority 28 (between
``mentions=25`` and ``memory:*=30+``) with the citation format:

```
Workspace context (top BM25 matches for the user's message).
Cite as [file:<path>] in your reply when you use one:

[file:src/auth.py]  (score: 1.823)
  def login(user, password):
      validate_token(password)

[file:README.md]  (score: 0.470)
  # Auth Service ...
```

Slot budget default: 10 KB. Threaded through
``build_runtime_assembly`` via a new ``session_id`` kwarg; existing
callers (``handle_chat``, the MCP ``get_prompt_assembly`` tool, the
Qt ``PromptAssemblyView`` panel, the dispatcher's ``prompt show``
command) all updated.

10 new tests in ``tests/test_prompt_assembly.py`` covering: slot
producer rejects each missing-input case (no workspace / no
session_id / short query / no cached index), end-to-end with a
real BM25 index over a temp workspace (citations present, message
ranks docs by BM25 score), **slot skipped without `retrieve build`
opt-in**, slot skipped for short messages, slot skipped when query
has no in-vocab terms, **slot priority sits between mentions and
memory blocks**, default budget pinned to 10 KB.

### Phase 3 — MCP tool

### Phase 3 — MCP tool

### Phase 3 — MCP tool
- Wrap as `recall` MCP tool; goes through the same `dispatch_input`
- Results include the same citation format so external agents see
  the provenance

### Phase 4 — UI
- Search bar in the Qt LinkedView that hits `recall` and renders the
  citation list as clickable items
- Each hit jumps the transcript to the cited turn

## Anti-patterns

- **No "summarise these N hits"**. `recall` returns *citations*; if
  the user wants a summary they pipe the hits into a chat turn. Mixing
  retrieval and synthesis hides provenance.
- **No vector search in v1**. See hard rules. Reconsider only if
  someone has a real corpus where FTS demonstrably misses.
- **No auto-recall in chat**. The model doesn't get to silently
  inject recall results into its context; if a chat turn benefits
  from recall, the user runs `recall` first then chats with the
  result visible. (Function-calling lanes are an exception; the
  model can call the recall tool but the result still appears in
  the visible transcript.)

## Cross-references

- `notes-buffer.md` — Phase 2 memory blocks are an alternate
  short-term memory surface; `recall` is the long-term log
- `architecture.md` — extends the storage layer (no schema change for
  JSON; one new FTS index per table for DuckDB)
- `mcp-server.md` — Phase 3 here adds one new MCP tool
- `error-vocabulary.md` — adds `recall_slow_path` (informational, not
  an error) and `recall_no_results` (used when `--kind` filters
  exclude every match)
