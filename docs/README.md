# Documentation

Index for the `docs/` folder. The agent-facing entry point is `../AGENTS.md`
at the project root.

## Reading order

For someone joining the project cold:

1. `../AGENTS.md` — the rules and the command vocabulary
2. `architecture.md` — the system at a glance + the roadmap
3. `vision/surfaces.md` — the surfaces (CLI / Tk / Qt / gateway) and
   how to choose between them
4. `api/gateway.md` — how to drive the system from outside
5. `api/goals.md` — durable goal verbs vs raw primitives
6. `../learnings/README.md` — how skill recipes work
7. `design/*.md` — designs for items not yet shipped
8. `CREDITS.md` — external projects whose ideas shaped this codebase

## Reference (already shipped)

| Doc                         | Covers                                                              |
|-----------------------------|---------------------------------------------------------------------|
| `architecture.md`           | System overview, two-tier API, gateway, roadmap                     |
| `api/gateway.md`            | HTTP endpoints, SSE event stream, EventBus reference                |
| `api/goals.md`              | `dispatch_and_wait`, `wait_for_inbox_empty`, `drain_inbox`, friends |

## Vision

Higher-level framing of where the project sits across surfaces and how
different consumers reach it.

| Doc                          | What it describes                                          |
|------------------------------|------------------------------------------------------------|
| `vision/surfaces.md`         | Shipped + planned surfaces; decision tree for which to use |

## Design docs (planned, not yet shipped)

These describe agreed-upon designs. The implementations are in flight or
queued.

### Foundations

| Doc                                | What it describes                                          | Status |
|------------------------------------|------------------------------------------------------------|--------|
| `design/error-vocabulary.md`       | Standard `reason` tags emitted on errors                   | Phase 1+2 shipped |
| `design/cli-mode.md`               | `python -m app.cli --session ... --command ... --json`     | planned |
| `design/notes-buffer.md`           | Per-session free-form markdown notes (+ structured memory blocks Phase 2) | ✅ Phase 1 shipped |
| `design/sandbox.md`                | Nix flake / Dockerfile so agents can spin up an env        | planned |
| `design/workspace-writes.md`       | Approval-gated file writes (+ chat-flow detection Phase 2 + isolation audit Phase 3.5) | Phase 1 + 1.5 + 2 + 3.5 shipped |
| `design/patch-taxonomy.md`         | Typed mutation envelope: 7 patch kinds, L1/L2/L3 friction tiers, atomic bundles, proactive `honest null` + auto-scheduler | ✅ Phase 1+2+3 shipped |
| `design/recall-memory.md`          | Searchable session history + BM25 over workspace docs + auto-injected retrieval slot   | ✅ Phase 1 + 2a + 2b shipped |
| `design/streaming.md`              | Token-by-token streaming for chat turns (provider primitive + UI integration roadmap)   | ✅ Phase 1 shipped |
| `design/vision-input.md`           | `@image:<path>` attachments + Qt drag-drop + capability gate + auto-route | ✅ Phase 1 + 2 + 3 + 3b shipped |
| `design/tool-calling.md`           | ` ```dispatcher` fenced blocks the model emits; one-click approval; multi-call bundles | ✅ Phase 1 + 2 + 3a + 3b shipped |
| `design/prompt-assembly.md`        | Layered prompt construction with per-slot budgets + provenance | Phase 1+2+3+4 shipped |
| `design/compact-prompt.md`         | Opt-in dense slot rendering: same info, fewer tokens (off by default) | ✅ Phase 1 shipped |
| `design/custom-instructions.md`    | Typed/conditional/addressable behavioural rules — better than ChatGPT-style custom instructions | ✅ Phase 1 + 2 + 2.5 + 3a shipped |

### Orchestration & chat

| Doc                                | What it describes                                          | Status |
|------------------------------------|------------------------------------------------------------|--------|
| `design/cowork-mode.md`            | Plan → approve → execute → synthesize state machine        | Phase 1 shipped |
| `design/chat-providers.md`         | Opt-in multi-provider chat lanes (OpenAI / Anthropic / Google) | Phase 1 shipped |

### Extension surfaces

| Doc                                | What it describes                                          | Status |
|------------------------------------|------------------------------------------------------------|--------|
| `design/extensibility.md`          | Umbrella: skill / command / plugin / MCP / browser / rules | umbrella |
| `design/mcp-server.md`             | Wrapping the dispatcher as an MCP server                   | Phase 1 shipped |
| `design/browser-automation.md`     | Playwright-driven browser skill with per-origin approval   | Phase 1 shipped |
| `design/agent-rules.md`            | Composable YAML rule layer the dispatcher enforces         | Phase 1 shipped |
| `design/workflow-templates.md`     | Saved + parameterised cowork plans / command sequences     | Phase 1 shipped |

## Architectural references that already live elsewhere

- `../project_plan.txt` — strategic plan including DuckDB choice + phased
  storage migration + drift control note
- `../_design/*.html` — hi-fi mockups + screenshots used during the
  Foundry theme design pass
- `../app/ui/themes_qss/foundry.qss` — the QSS spec for the Qt UI

## Credits

External projects whose ideas shaped this codebase live in
[`CREDITS.md`](CREDITS.md). Patterns adapted, not code — but the
right thing to do is to acknowledge where the thinking came from.
Currently credits **OpenCode**, **Sandcastle**, **Letta**, **Hermes
Agent**, and **Abacus AI Desktop**.

(The single batch-sizing aphorism from `rs-sdk` — "a failed 5-minute
run wastes more time than five 30-second diagnostic runs" — stays
credited inline at its point of use in `../AGENTS.md` §2 rather
than as a top-level credit; it's a quote, not an architecture
adoption.)

## Internal lineage

The user's own earlier projects whose patterns landed here. Not a
credit (you don't credit yourself) — kept here only as a traceability
note so a future reader who recognises a pattern can find the
original.

| Project | Patterns landed here | Where they live |
|---------|----------------------|------------------|
| **Local AI Workbench** (user's earlier Tkinter project) | Cross-workspace path-leakage audit, cancellation-token pattern, typed pipeline reports, naive-bayes lane classifier + live blend, BM25 retrieval index + prompt-slot integration | `design/workspace-writes.md` Phase 3.5 (✅), `design/cowork-mode.md` Phase 5.5 + 5.6 (✅), `design/agent-rules.md` Phase 5a + 5b (✅), `design/recall-memory.md` Phase 2a + 2b (✅) |
| **Personal App** (user's self-modifying browser agent) | Typed patch taxonomy (7 kinds), L1/L2/L3 friction tiers, atomic bundle approval, "honest null" proactive trigger pattern, shell-generated impact statements (not model-generated) | `design/patch-taxonomy.md` (new), `design/agent-rules.md`, `design/workspace-writes.md` Phase 4 |

## Conventions

- Prose is wrapped at ~80 columns where convenient; markdown rendering
  doesn't care, but it makes diffs readable
- Code samples are runnable (or marked clearly as schematic)
- "Planned" / "shipped" labels are honest — don't claim what isn't there
- Every design doc carries a `Status:` line at the top so a reader can
  tell at a glance whether it's shipped, in flight, or sketch-only
