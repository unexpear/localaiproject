# Design: Browser Automation Skill

**Status: Phase 1 shipped · 2026-05-04**

Phase 1 — read-only browser commands (`open` / `navigate` / `screenshot`
/ `get-text` / `get-html` / `close`), per-origin approval whitelist on
`session.context.browser`, sync Playwright backend with graceful
fallback when not installed, fake backend for tests — is shipped
(`tests/test_browser_service.py`, 32 tests). Phase 2 (`fill` / `click`
+ persistent contexts) and Phase 3 (`eval` with always-prompt gate +
multi-tab + file-download capture) remain planned.

A skill that gives a builder Playwright-driven browser capabilities so
it can navigate sites, fill forms, scrape pages, and screenshot
results — packaged as a regular skill the planner can dispatch to.

Adapted from Abacus AI Desktop's "agentic browser automation" /
upcoming Chrome extension. We do it with Playwright because that's
what we already use in tests + huashu-design.

## Why

Lots of real-world tasks straddle "have the model think about it" and
"interact with the web":

- "Sign me in to GitHub and grab my open PRs"
- "Pull the docs page for `httpx` and summarise"
- "Fill out the form on this URL with the values in `data.csv`"
- "Take a screenshot of the rendered output of `examples/foo.html`"

Today the builder can READ files via the workspace gate, but it can't
touch the web. The browser-automation skill closes that.

## Hard rules

- **Local-first preserved.** The browser runs locally (Playwright's
  Chromium / Firefox bundles, or a system browser). We don't proxy
  through any remote service.
- **Per-action approval, opt-in.** A "browser navigate" action goes
  through the same approval gate as workspace reads. Default off.
  User must `approve` each navigate / form-fill / click on a new
  origin (cached per session per origin).
- **No persistent identity by default.** Each browser session uses a
  fresh, isolated Playwright `BrowserContext`. Persistent profiles
  are an explicit opt-in.
- **No cross-origin script injection.** We don't inject anything into
  pages we didn't request — Playwright API only.

## Surface

### Skill registration

```python
# app/services/skill_service.py
SKILLS["browser"] = {
    "lane_bias": "reasoning",
    "recipe_file": "browser.md",  # learnings/browser.md
    "tools": ["browser.*"],       # the dispatcher commands listed below
}
```

### Dispatcher commands

```
browser open <url>                  # opens a tab, returns title + screenshot
browser navigate <url>              # within the current session's browser
browser screenshot [<selector>]     # full page or just a CSS selector
browser get-text [<selector>]       # plain text (no scripts)
browser get-html [<selector>]       # raw HTML
browser fill <selector> <value>     # type into an input
browser click <selector>            # click an element
browser eval <expression>           # run JS in page context (gated, see below)
browser close                       # tear down the browser session

browser session list                # active browser sessions
browser session new <id>            # spin up a fresh isolated context
browser session use <id>            # subsequent commands use this session
```

Each command is a regular dispatcher command emitting a normal event.
The "browser" prefix groups them.

### Per-session browser state

`session.context.browser`:

```python
"browser": {
    "active_session_id": "br_xxx",
    "approved_origins": ["https://github.com", "https://docs.python.org"],
    "page_snapshot": {
        "url": "...",
        "title": "...",
        "screenshot_path": "_browser_cache/abc.png",
    },
}
```

Approved-origins list serves as the per-session whitelist. Once the
user `approve`s `browser navigate https://example.com`, subsequent
navigates within `https://example.com` skip the approval gate. A
different origin re-prompts.

### Approval flow

```
> browser navigate https://github.com/anthropics
[approval required] browser navigate https://github.com/anthropics
   (origin not yet approved for this session: github.com)
> approve
… (page loaded; screenshot at _browser_cache/abc.png; title: "anthropics · GitHub")

> browser navigate https://github.com/anthropics/claude-code
… (origin already approved; runs immediately)

> browser navigate https://random-site.example
[approval required] browser navigate https://random-site.example
   (new origin: random-site.example)
```

### Bus events

- `KIND_BROWSER` (new) — published on navigate / click / fill / eval.
  Payload includes URL, title, action kind. Subscribers (Qt UI) can
  show a small browser-state widget.
- Approval flow emits `KIND_DISPATCH` with `reason: approval_required`
  as today.

## Implementation

### Module: `app/services/browser_service.py`

Wraps Playwright sync API. Browser sessions live as instance state on
a process-wide `BrowserPool`:

```python
class BrowserPool:
    def get_or_create(self, session_id: str) -> BrowserHandle:
        ...
    def close(self, session_id: str): ...
    def list(self) -> list[str]: ...
```

Each `BrowserHandle` owns one Playwright `BrowserContext` + one Page.
Context is persisted to `app/services/browser_cache/<session_id>/` if
the user opts into persistence (`browser session new <id> --persist`).

### Snapshot artifacts

After every navigate / click / fill, we capture:
- Screenshot (PNG) — `_browser_cache/<sha>.png`
- Plain-text view (rendered DOM text) — for the model's context
- A small JSON summary `{url, title, screenshot, text_excerpt}`

The chat handler appends the JSON summary as a system message after
the action so the model sees the result.

### `browser eval` is special

Running arbitrary JavaScript in the page is powerful and dangerous.
`browser eval` is **always** approval-gated (no per-origin caching) and
restricted by default to a no-network, no-storage Playwright `Page`
(Playwright's permissions default is reasonable; we can lock down
further with `context.set_extra_http_headers` etc.).

A future round adds an "eval whitelist" of vetted snippets the user
trusts (e.g. "extract the OpenGraph metadata") that auto-approve.

### Persistence

Default: every `browser session new` starts with a fresh, empty
context. No cookies, no logged-in identity.

Opt-in:
```
browser session new mygithub --persist
```

Stores cookies + localStorage in `app/services/browser_cache/mygithub/`.
Re-using the session loads the persisted state. Useful for "be logged
in to GitHub for this whole batch of tasks". Persisted contexts are
cleared with `browser session clear-persisted <id>`.

### Test mode

In tests, swap the real Playwright pool for a fake that returns canned
HTML / screenshots. Pattern lives in `tests/test_browser_service.py`.

## Integration with cowork

CoWork sub-tasks can target the browser skill:

```yaml
# inside a cowork plan
sub_3:
  task: "Pull the API rate-limit docs from https://api.example.com/docs"
  builder: research-bot
  acceptance: "Returns the rate-limit number"
```

If `research-bot` has the `browser` skill active, it can call
`browser navigate` and `browser get-text` as part of its dispatch
chain.

## File deliverables

`browser screenshot <path.png>` writes through the workspace-write
approval flow (see `workspace-writes.md`). Same gate; the file lands
in the user's pinned workspace.

## Implementation phases

### Phase 1 — read-only browser
- `browser open / navigate / screenshot / get-text / get-html`
- Per-session approval whitelist
- Sync Playwright; one browser per process
- Tests with a fake pool

### Phase 2 — interaction
- `browser fill / click`
- Persistent contexts (opt-in)
- Bus events

### Phase 3 — eval + advanced
- Whitelisted JS snippets
- Multi-tab support
- File download capture

### Phase 4 — UI
- Browser preview pane in Qt UI showing the latest screenshot
- Click an element in the preview to compose a `click <selector>`
  command

## Tests

- `tests/test_browser_service.py` — fake pool; navigate produces
  expected payload; approval flow gates new origins
- `tests/test_browser_eval_gate.py` — eval ALWAYS prompts even on
  approved origins
- `tests/test_browser_persistence.py` — persisted context survives
  pool teardown; non-persisted does not
- `tests/test_browser_dispatcher.py` — `browser open <url>` returns
  expected event shape; `reason: approval_required` on first
  unapproved origin

## Costs and constraints

- Playwright is ~150 MB on disk (browser binaries). Optional install:
  `pip install localaiproject[browser]` triggers
  `playwright install chromium`.
- Browser sessions are heavy: each takes ~100 MB RAM. Hard cap of 5
  concurrent sessions per process.
- If Playwright isn't installed, the `browser` skill is hidden from
  `skill add` listings and dispatching the commands returns
  `reason: skill_unavailable`.

## Anti-patterns

- **Auto-approve all origins.** No. The whitelist is per-session and
  per-origin. The user reviews each new domain.
- **Persistent context by default.** No. Logged-in state is sticky;
  the user must opt in per session.
- **Cross-session cookie sharing.** No. Each session is its own
  Playwright context. If you want shared identity, name the same
  session id.
- **Headful mode by default.** Default headless. Headful is opt-in
  for debugging (`browser session new debug --headful`).

## Honest gaps

- **No screen reader / accessibility tree query.** Just plain-text +
  screenshots. AT integration is heavy; not in v1.
- **No video capture.** Single screenshots. Could add via
  Playwright's `record_video` API in a future round.
- **No CAPTCHA handling.** If a page demands a CAPTCHA we surface a
  screenshot to the user and stop. No bypass.
- **No reverse-proxy / MITM.** We don't intercept HTTPS. Whatever the
  browser sees is what we see.

## Related to Abacus's Chrome extension

Abacus's pitch is a Chrome extension that "gives contextual help on
any website you visit." That's a different shape — it lives in the
user's existing browser; we're spinning up a separate browser via
Playwright. Trade-offs:

| Approach                 | Pros                                                     | Cons                                                            |
|--------------------------|----------------------------------------------------------|-----------------------------------------------------------------|
| Chrome extension         | Uses user's existing identity / state out of the box     | OS / browser deployment overhead; permissions story complex     |
| Playwright (this design) | Fully scriptable; tests work; per-session isolation      | Doesn't help with "the page I happen to be on" without copy-paste|

We pick Playwright because:
- It's what we already use in tests + the huashu-design skill
- It composes with cowork
- It doesn't require browser-vendor cooperation

A Chrome extension could come later as a complementary surface: it
posts to the gateway with current-page context. Same dispatcher, same
approval gates. Out of scope for v1.

## Cross-references

- `cowork-mode.md` — the most natural consumer
- `workspace-writes.md` — for screenshot / scraped-data deliverables
- `extensibility.md` — where this skill fits in the extension catalog
- `error-vocabulary.md` — adds `skill_unavailable`,
  `origin_not_approved`, `eval_refused`, `playwright_not_installed`
