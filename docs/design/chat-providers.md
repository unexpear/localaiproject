# Design: Chat Providers (optional cloud lanes)

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — provider registry (Ollama / OpenAI / Anthropic), per-session
`lane_providers` storage, `provider list / show / bind / unbind`
dispatcher commands, env-var auth, chat_service integration that
swaps the inference call when a binding exists, and `forbid_provider`
rule action activation — is shipped and tested
(`tests/test_chat_providers.py`, 42 tests). Phase 2 (Google Gemini
adapter, keyring + 1Password CLI auth fallback, streaming,
per-lane temperature/max_tokens config) remains planned.

**Adapted from "Dedicated Chat Mode".**

Add an opt-in provider layer beneath the lane router so a session can
route to a cloud model (Claude, Gemini, OpenAI) instead of the local
Ollama model on a per-lane basis. Local-first stays the default; cloud
is **strictly opt-in per lane**.

## Why

Today every lane resolves to one Ollama model:

| Lane         | Model                  |
|--------------|------------------------|
| reasoning    | llama3.1:8b            |
| coding       | qwen2.5-coder:7b       |
| vision       | qwen3-vl               |
| unfiltered   | dolphin3:8b            |

That's the right default for a local control layer. But sometimes you
want a frontier model for a hard reasoning task without leaving the
shell, the dispatcher, the executions log, or the planner/builder
flow. Adding a "provider" abstraction lets you bind a lane to any
backend that speaks chat — Ollama, OpenAI, Anthropic, Google — without
forking the codebase.

## Hard constraints

- **Local-first remains the default.** A fresh checkout never sends
  network traffic. Cloud providers are off until configured.
- **No tracking, no auto-routing.** A user explicitly binds a lane
  to a cloud provider; there's no heuristic that "decides" to go cloud.
- **Per-session opt-in.** Provider bindings live in session context;
  one session can be cloud-backed while another is local-only.
- **Approval gating still wins.** Workspace reads/writes go through
  the same gate; the model never gets to bypass them, no matter who
  hosts it.
- **No telemetry from us.** We don't aggregate usage, costs, latency
  beyond what the user can see in their executions log.

## Surface

### Provider registry

New module `app/models/provider_registry.py`. Same shape as
`model_registry.py`:

```python
PROVIDER_REGISTRY = {
    "ollama":     {"label": "Local Ollama",     "kind": "ollama",     "client": ...},
    "openai":     {"label": "OpenAI",           "kind": "openai",     "client": ...},
    "anthropic":  {"label": "Anthropic Claude", "kind": "anthropic",  "client": ...},
    "google":     {"label": "Google Gemini",    "kind": "google",     "client": ...},
}
```

Each provider has:
- A **client adapter** (subprocess / HTTP) that maps our `infer_chat`
  signature to the provider's API
- A **credential lookup** (env var, OS keyring, or 1Password CLI)
- A **default model** the lane can resolve to
- A **cost / latency hint** the doctor can show (rough)

### Session-scoped lane→provider bindings

Add `lane_providers` to `session.context`:

```python
"context": {
    ...
    "lane_providers": {
        "reasoning": "anthropic:claude-sonnet-4.5",
        # Other lanes default to "ollama" (the only registered local provider).
    },
}
```

Format: `<provider_id>:<model_id>`. Empty / missing means "use local
Ollama for this lane" (current behaviour).

### Dispatcher commands

```
providers                         # list registered providers + auth state
providers status <id>             # one provider's auth + recent latency
provider bind <lane> <provider>:<model>      # bind for current session
provider bind <lane> <provider>:<model> --global  # default for new sessions
provider unbind <lane>            # revert to local Ollama
```

### Auth

Reads credentials from one of (in priority order):

1. **Env var** — e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `GOOGLE_API_KEY`. Documented per provider.
2. **OS keyring** via `keyring` package (cross-platform). Only loaded
   if installed.
3. **1Password CLI** (`op://localaiproject/<provider>/credential`)
   if `op` is on PATH.

Never persisted in `local_memory.json` / `local_memory.duckdb`. The
storage layer carries lane bindings, not credentials.

### Cost / rate-limit visibility

Provider responses include `usage` data when available. We surface it
in the execution record:

```python
"provider_usage": {
    "input_tokens": 1234,
    "output_tokens": 567,
    "estimated_cost_usd": 0.012,
    "latency_ms": 1430,
}
```

Plus a top-line indicator in the chassis bar (Qt UI) showing the
running provider for each lane and a small `$` if any cloud lane is
active.

## Routing flow with providers

```
user sends "explain this regex"
       │
       ▼
router picks lane = "reasoning"
       │
       ▼
session.context.lane_providers["reasoning"] == "anthropic:claude-sonnet-4.5"
       │                            │
       │ if missing                 │ if set
       ▼                            ▼
ollama client                AnthropicProvider.infer_chat(...)
   │                                │
   ▼                                ▼
local model returns          API returns
   │                                │
   └────────────────┬───────────────┘
                    ▼
        execution record + history append + bus event
              (same as today)
```

The router is unchanged. The model_registry resolution gets a
provider-aware step in front. Everything downstream — executions,
history, compact memory, planner/builder feedback — stays the same.

## Provider adapter interface

```python
class ChatProvider(Protocol):
    name: str
    def infer_chat(self, model: str, *, messages: list[dict],
                   stream: bool = False, cancel_event=None) -> dict:
        """Return shape:
        {
            "ok": bool,
            "content": str,
            "error": str | None,
            "raw": dict,        # provider-specific
            "usage": dict | None,
        }
        """
```

`OllamaProvider` already exists as `runtime_client.infer_chat` — wrap
it. Each new provider adds one file: `app/models/providers/openai.py`,
`anthropic.py`, `google.py`. Optional install:

```
pip install localaiproject[providers]
# or
pip install anthropic openai google-generativeai
```

If the package isn't installed, the provider's `is_available()` returns
False and it doesn't show up in `providers list`.

## Plan vs Build mode

Cloud providers honour the existing `mode plan` / `mode build` system
prompt. Same hint goes to Claude / GPT / Gemini as goes to Ollama.
This is policy-via-prompt; provider-side guardrails (Anthropic's
constitutional AI etc.) still apply on top.

## What changes for executions

The execution record gains `provider` and `provider_usage`:

```python
{
    "id": "exe_a3b1...",
    "lane": "reasoning",
    "provider": "anthropic",
    "model": "claude-sonnet-4.5",
    "provider_usage": {...},
    ...  # same fields as today
}
```

`executions search lane=reasoning provider=anthropic` becomes a
queryable filter. The DuckDB executions_index gets a `provider` column
in a small migration; JSON falls back to in-payload search.

## What changes for the planner / builder coordination

Nothing. A planner can fan out tasks to builders, and each builder
session has its own provider bindings. So you can route the planner to
local Ollama (cheap, fast) and one builder to cloud Claude (expensive,
strong) for high-stakes work.

## Implementation phases

### Phase 1 — provider abstraction
- `app/models/providers/__init__.py` with the `ChatProvider` protocol
- `OllamaProvider` extracted from `runtime_client.py` (no behaviour
  change; just give it a class face)
- `provider_registry.py` with PROVIDER_REGISTRY (ollama only)
- `lane_providers` field added to session context
- `provider bind / unbind / providers list` commands

### Phase 2 — first cloud provider (Anthropic)
- `app/models/providers/anthropic.py`
- Auth via env var (`ANTHROPIC_API_KEY`)
- Streaming support for the chat endpoint
- Cost calculation from documented per-token rates
- Tests with a fake HTTP server (no real API call)

### Phase 3 — OpenAI + Google
- Same shape as Anthropic; mostly fill-in-the-blank

### Phase 4 — secrets management
- Keyring integration as fallback
- 1Password CLI integration
- `provider auth-test <id>` to verify credentials without burning a turn

### Phase 5 — UI surfacing
- Chassis bar shows the active provider per lane (small mono text)
- Doctor report includes per-provider auth + recent latency
- Settings dialog gets a Providers tab for binding lanes

### Phase 6 — provider-runtime resolver (planned, Hermes-inspired)

Hermes Agent ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
ships a single `ProviderResolver` that maps `(provider_id, model_alias)`
→ `(api_mode, api_key, base_url)` for 18+ providers. Adopting that
shape early prevents N×M sprawl as we add Google, OpenRouter,
Together, Groq, Anyscale, etc.

```python
@dataclass
class RuntimeBinding:
    api_mode: str          # "openai_chat_completions" | "anthropic_messages" | "ollama_chat" | ...
    base_url: str
    auth_header_factory: Callable[[str], dict]
    request_shaper: Callable[[list[dict], dict], dict]
    response_parser: Callable[[dict], dict]

def resolve(provider_id: str, model_alias: str) -> RuntimeBinding:
    """Single source of truth for routing a (provider, model) pair to
    its concrete HTTP shape. Aliases (e.g. ``gpt-4o-mini`` →
    ``openai:gpt-4o-mini``) live in one registry file."""
```

Both `runtime_client` and `chat_providers` consume `RuntimeBinding`s
instead of branching on provider id. Aliases get a single
`PROVIDER_ALIASES` dict so the user can write
`provider bind reasoning sonnet` and have it expand to
`anthropic:claude-sonnet-4-5`.

**Honest gap**: pure refactor — no user-visible win until we add the
*next* provider. Ship it the next time we add a provider (Google
Gemini is the natural first beneficiary), not as standalone work.

## Tests

- `tests/test_providers.py` — Ollama provider behaviour matches
  current `runtime_client.infer_chat` output
- `tests/test_provider_anthropic.py` — fake HTTP server returns canned
  responses; assert mapping into our event shape
- `tests/test_dispatcher_provider_commands.py` — `providers list`,
  `provider bind`, `provider unbind` round-trip; bound lane uses the
  expected provider in chat flow
- Auth tests — env var read, keyring read, missing creds → clear
  `reason` tag (`provider_unauthenticated`)

## Honest gaps

- **No request batching across lanes.** If you bind both `reasoning`
  and `coding` to OpenAI, that's two separate API calls per turn (one
  per lane); we don't multiplex.
- **No fallback chain.** If a cloud provider is down, the lane fails
  rather than auto-falling-back to Ollama. Predictable; you can build
  fallback in user-space with `goals` retries.
- **No cost cap.** We log usage; we don't refuse to dispatch when you've
  spent $X today. A future round could add a budget-per-session
  setting.
- **No streaming yet for non-Anthropic providers.** Phase 2 ships
  streaming for Anthropic only; OpenAI / Google stream support follows.

## Open questions

- Should provider bindings be **per-session only**, or also have a
  user-default? Default-by-user makes sense; design it but ship
  per-session first.
- Do we expose **temperature / top_p** via lane bindings? Probably
  yes as optional dict on the binding: `lane_providers["reasoning"]
  = "anthropic:claude-sonnet-4.5?temp=0.2"`.
- How does the **router lane confidence score** map to "should I
  upgrade to cloud here"? Doesn't, by design — routing decisions stay
  local-only; provider choice is explicit.

## Why this isn't "frontier-model-by-default"

Because the project's principle is **local-first**. The user owns the
data, the inference, the kill switch. Cloud lanes are a sharp tool;
we expose them, we don't lean on them.

## Anti-pattern to avoid

- "Default to cloud, fall back to local" — wrong direction; reverses
  the principle.
- Routing-time auto-selection ("this looks hard, send to Claude") —
  hidden behaviour; surprising costs; reverses the principle.
- Storing API keys in `local_memory.json` — credentials never go in
  the storage layer, period.
