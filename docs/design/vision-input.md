# Design: Vision Input

**Status: Phase 1 + Phase 2 + Phase 3 + Phase 3b shipped · 2026-05-07**

We've had a `vision` lane in the router since day one — keyword
matching ("describe this image", "what's in the photo")
correctly routes to vision-capable models. But until now, **no
images could actually reach the model**. The vision lane was an
empty promise.

This is **net-new design**, not a phase increment. Adopted from
the same explicit-mention pattern `mentions_service` uses for
text files (`@app/foo.py`); vision input gets the parallel
`@image:<path>` syntax.

## Why

Three concrete user moments this enables:

1. *"Look at this screenshot of my login page and tell me why
   the button is misaligned."* — drop a `.png`, get layout
   feedback.
2. *"Compare this design mock to my current home page."* —
   multi-image attachments (up to 4 per turn).
3. *"Describe the data in this chart."* — visual data
   extraction without OCR plumbing.

Today the user has to copy-paste descriptions of images. With
`@image:`, the model sees the actual pixels.

## Hard rules

- **`@image:` syntax disambiguates from `@<path>`.** Generic file
  mentions still mean "inline this text"; image mentions mean
  "attach as visual input." A single message can use both:
  `@image:design.png against @app/views/home.py`.
- **Same consent model as text mentions.** The user typing
  `@image:foo.png` IS the explicit consent — no separate
  approval gate. Path traversal still rejected
  (`@image:../escape.png` → `invalid_path`).
- **Per-image and per-message caps.** 10 MB per image, max 4
  images per turn. Anything beyond is silently dropped (count)
  or returned as a typed failure (size). Mirrors the
  `mentions_service` per-file/total byte caps.
- **Symlinks refused.** Same as workspace_service writes —
  symlinks could escape the workspace boundary.
- **Failed attachments surface inline.** A bad path doesn't
  silently disappear; the metadata view is recorded on the
  user-message execution record so the user can see "we tried
  but it didn't work."

## Surface

### `@image:<path>` syntax

```
@image:screenshots/login.png describe what's wrong here
```

Lookbehind matches `@image:` only when not preceded by a word
character — so `user@image:foo` (a hypothetical email) doesn't
trigger. Trailing punctuation stripped:
`@image:foo.png.` → path is `foo.png`.

### Recognised extensions

`.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp` — anything else gets
`unsupported_format`. The mime mapping is in
`EXTENSION_MIME_TYPES`.

### `attach_image_mentions(text, workspace_path)` returns

```python
{
    "original": <input>,
    "stripped": <input with @image:foo replaced by [image:foo]>,
    "had_workspace": bool,
    "attachments": [  # full, with base64 payload
        {"path": "...", "ok": True, "base64_data": "...",
         "mime_type": "image/png", "size_bytes": 12345, "error": ""},
        ...
    ],
    "metadata": [  # safe-to-log subset (no base64)
        {"path": "...", "ok": True, "mime_type": "image/png",
         "size_bytes": 12345, "error": ""},
        ...
    ],
    "expanded": bool,    # any successful attachment
    "summary": str,
}
```

### `apply_to_messages(messages, attachments)`

Mutates `messages` in place: adds `images: [<base64>, ...]` to
the trailing user message. No-op when there are no successful
attachments or when the trailing message isn't user role.

### Failure tags (returned on individual attachments)

| `error`                | When |
|------------------------|------|
| `no_workspace`         | No workspace pinned for this session |
| `invalid_path`         | Path traversal (`..` segments) detected |
| `outside_workspace`    | Resolved path escapes the pinned workspace |
| `symlink_unsupported`  | Path is a symlink |
| `path_not_found`       | File doesn't exist |
| `unsupported_format`   | Extension not in the recognised set |
| `size_exceeded`        | File > `per_image_max_bytes` (10 MB) |
| `read_failed: <os error>`     | Disk read error |
| `encode_failed: <error>`      | Base64 encoding crashed |
| `non_vision_model`     | Phase 3: lane resolved to a model without `is_vision_capable`; image stripped from runtime payload |

These overlap with `error_vocabulary.py` tags but are NOT
canonical reason tags — they're field-level errors on a
per-attachment dict, not dispatch refusals. The chat dispatch
itself still succeeds even when all attachments fail; the user
sees the attempts in the execution record's
`image_attachments` metadata.

## Implementation phases

### Phase 1 ✅ shipped (this doc)

- `app/services/vision_service.py` — parser + base64 encoder +
  apply helper + summary formatter.
- Wired into `chat_service.handle_chat` BEFORE
  `mentions_service.expand_mentions` (vision runs first so
  `@image:foo` doesn't get shadowed by the generic `@<path>`
  regex).
- `image_attachments` metadata field on user-message execution
  records (metadata-only view; no base64 in the log).
- 25 tests in `tests/test_vision_service.py` (1 skipped on
  Windows for symlink semantics).

### Phase 2 ✅ shipped (2026-05-07)

New `app/ui/image_drop_input.py` ships an `ImageDropInput`
widget — a `QLineEdit` subclass that:

- Accepts dragged image files via `dragEnterEvent` (extension
  filter only — no workspace probe at drag time so a
  workspace pinned mid-drag still works).
- Resolves dropped files at `dropEvent` time against an
  injected `workspace_path_provider` callable. The provider
  is queried fresh on every drop so workspace-pin changes
  take effect immediately.
- Inserts `@image:<rel-path>` tokens at the cursor position
  for every successfully-resolved file. Tokens are
  space-separated; the widget pads with leading/trailing
  spaces so the inserted text doesn't fuse with surrounding
  characters.
- Caps the number of inserted tokens at
  `vision_service.MAX_IMAGES_PER_MESSAGE` (4).
- Emits two signals:
  - `image_dropped(rel_path)` — for each successful drop.
  - `image_drop_rejected(absolute_path, reason)` — per-file
    failure with one of the four `REJECT_*` constants
    (`no_workspace`, `outside_workspace`,
    `unsupported_format`, `not_a_file`).
- Public `handle_dropped_paths(paths)` method exposes the
  resolution + insertion logic without requiring a synthetic
  `QDropEvent` — both the widget's `dropEvent` and the unit
  tests use this entry point. (Synthetic `QDropEvent`
  construction segfaults on some PySide6 versions.)

`qt_main_window.py` swaps its `QLineEdit` input for
`ImageDropInput`, wires `workspace_path_provider` to a
zero-arg helper that pulls the current session's pinned
workspace from `local_memory`, and connects
`image_drop_rejected` to a transcript hint that surfaces
the rejection reason in plain English (e.g.
"`shot.png: drop refused (file is outside the pinned
workspace)`").

19 tests in `tests/test_image_drop_input.py`:
- `_resolve_drop` validation (6): valid path, non-existent,
  non-image extension, no workspace, outside workspace,
  empty path.
- `handle_dropped_paths` end-to-end (10): single, multi,
  cap, mixed valid+rejected (signals fire), `image_dropped`
  signal payload, **token padded with spaces around existing
  text**, **token inserted at mid-text cursor position**,
  no-workspace rejects all, **provider exception treated
  as no_workspace**, empty list, empty-string paths skipped.
- Construction smoke (2): `acceptDrops` enabled, signals
  exposed.

### Phase 3 — model capability awareness (planned)

### Phase 3 ✅ shipped (2026-05-07)

- `MODEL_REGISTRY["vision"]` now carries an explicit
  `vision_capable: True` flag.
- New `is_vision_capable(model_name)` helper in
  `app/models/model_registry.py`. Resolution order:
  1. Explicit registry flag wins.
  2. Substring heuristic against
     `_VISION_NAME_HEURISTICS` — catches the common families
     (`vision`, `-vl`, `:vl`, `llava`, `moondream`, `bakllava`,
     `minicpm-v`, `pixtral`).
  Heuristic is deliberately permissive — false positives
  (sending images to a model that ignores them) are cheap;
  false negatives (refusing useful queries) are annoying.
- `chat_service.handle_chat` Phase 3 capability gate: after
  resolving the lane → model AND detecting successful image
  attachments, checks `is_vision_capable(resolved_model)`. On
  non-vision models:
  - Each successful attachment is flipped to
    `ok=False, error="non_vision_model"`.
  - The base64 payload is dropped from the cached
    attachment (no point keeping the upload-sized blob in
    memory).
  - `apply_to_messages` is skipped — the runtime payload
    has no `images` field at all (instead of sending it to a
    blind model).
  - `image_expansion["summary"]` becomes `"image
    attachments dropped — resolved model {model!r} is not
    vision-capable"`.
- The user sees the rejection in the user-message execution
  record's `image_attachments` metadata, same channel as the
  Phase 1 per-attachment failures (`path_not_found`,
  `unsupported_format`, etc.).

10 new tests in `tests/test_vision_service.py` (35 total):
- `IsVisionCapableTests` (7): registry flag, non-vision
  registered models, vision-keyword heuristic, `-vl` suffix,
  llava family, other families (moondream / bakllava /
  minicpm-v / pixtral), empty/None input.
- `CapabilityGateInHandleChatTests` (3): **vision-capable model
  receives images** (asserts `images` field in runtime
  payload), **non-vision model strips images** (asserts
  absent from runtime payload), **non-vision attempt records
  failure in metadata** with `error="non_vision_model"`.

### Phase 3b ✅ shipped (2026-05-07)

New `vision_lane_bias(text)` helper in `vision_service.py`:

- Counts `@image:` tokens via the existing
  `extract_image_mentions` parser (same lookbehind, same
  dedup).
- Returns `min(count * PER_IMAGE_VISION_BIAS,
  MAX_VISION_BIAS)` — currently +5 per image, capped at +10.
- Counts INTENT (token presence in the user's text), not
  successful attachment. The bias should fire even when the
  file path turns out wrong; the router's job is to pick the
  right LANE based on what the user is asking for.
  Phase 3a's capability gate handles the actual attachment
  failures.

`chat_service.handle_chat` adds the bias to `lane_biases`
before calling `router.route_message`. The bias composes
additively with skill biases (Phase 1 of skills) and
classifier biases (Phase 5b of agent-rules) — same
`lane_biases: Dict[str, int]` shape the router has always
consumed.

10 new tests in `tests/test_vision_service.py`:
- `VisionLaneBiasTests` (7): no images → 0, empty/None → 0,
  one image → per-image bias, two images → doubled,
  **three+ images capped at MAX_VISION_BIAS**, lookbehind
  excludes email-like tokens, dedupe behaviour.
- `VisionBiasInHandleChatTests` (3): **end-to-end via
  `handle_chat` with mocked `route_message`** asserting the
  captured `lane_biases["vision"]` equals
  `PER_IMAGE_VISION_BIAS` for one image, `MAX_VISION_BIAS`
  for three images, absent (or zero) when no `@image:`
  tokens present.

### Phase 4 — programmatic image input (planned)

### Phase 4 — programmatic image input (planned)

- MCP tool `attach_image(session_id, path)` that an external
  agent can call directly — same consent model (the agent must
  pass an explicit path, no auto-discovery).

## Honest gaps

- **No automatic downscaling.** A 9.9 MB photo squeezes through
  but takes meaningful upload time. Future versions could
  Pillow-resize to a model-friendly 1024×1024 max.
- **Base64 payloads in execution records would bloat
  storage.** That's why we store the metadata-only subset
  (`image_attachments` field). The downside: replaying an
  execution record can't reconstruct the original images
  (the on-disk file might have changed since).
- **No drag-and-drop in CLI.** The user has to type
  `@image:path` by hand. Phase 2 fixes this for Qt; CLI users
  type it themselves (which mirrors how `@file` mentions work
  today).
- **Multi-image turns blast the prompt size.** 4 × 10 MB =
  40 MB of base64 in one HTTP request. Local Ollama handles
  it; cloud providers may hit request-size limits. Phase 3
  could add a "blend hint" so cloud models receive lower-res
  thumbnails.

## Cross-references

- `mentions_service.py` — the `@file` text mention pattern this
  module mirrors.
- `chat-providers.md` — Ollama's `/api/chat` already accepts
  `images` per message; cloud providers will need their own
  vision API plumbing in Phase 2/3.
- `streaming.md` — orthogonal; vision attachments work the
  same in batch + streaming flows.
