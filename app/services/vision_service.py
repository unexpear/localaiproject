"""@image: attachments for chat input — Phase 1 of vision-input.md.

When a user types ``@image:screenshots/login.png describe what's
happening here``, the chat service extracts the image reference,
base64-encodes the file, and attaches it to the user message in
the format Ollama's ``/api/chat`` expects (``images: [<b64>]`` on
the message dict). Vision-capable models (llama3.2-vision,
qwen2-vl, etc.) consume the field; non-vision models silently
ignore it, so the same mention is safe across lanes.

Mirrors ``mentions_service.expand_mentions`` for text files: the
parser is conservative (``@image:`` not preceded by a word
character so ``user@image:foo`` doesn't match), the
attachment is bounded by per-image and per-message caps, and the
operation is read-only (no workspace approval required because
the user's explicit ``@image:`` token is the consent gesture).

The ``@image:`` prefix disambiguates from generic ``@<path>``
text mentions — keep them separate so a single message can both
attach an image AND inline a text file:

    @image:screenshots/login.png check this against @app/auth.py

This module ships the primitive (parser + base64 encoder + attach
helper). ``chat_service.handle_chat`` calls it alongside the
existing mention-expansion step.
"""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ============================================================
# Caps
# ============================================================

#: Hard cap on a single image's encoded size. 10 MB matches the
#: practical limit most Ollama vision models accept; bigger
#: images get downscaled by the server but eat upload time, so we
#: refuse rather than silently truncate.
PER_IMAGE_MAX_BYTES = 10 * 1024 * 1024

#: Hard cap on the number of images per chat turn. Multi-image
#: support varies by model; 4 is a sensible upper bound that
#: works across llama3.2-vision, qwen2-vl, etc. Beyond this we
#: silently drop trailing matches.
MAX_IMAGES_PER_MESSAGE = 4


# ============================================================
# Recognised image extensions + mime mapping
# ============================================================

#: Mapping from lowercase extension to MIME type. Anything else is
#: rejected at parse time with reason="unsupported_format".
EXTENSION_MIME_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


# ============================================================
# Parser
# ============================================================

#: Match ``@image:<path>`` where ``@`` is not preceded by a word
#: character. Same lookbehind pattern as ``mentions_service``.
_IMAGE_MENTION_RE = re.compile(r"(?<![\w])@image:([^\s]+)")


def extract_image_mentions(text: str) -> List[str]:
    """Return the unique ``@image:<path>`` paths in ``text``, in
    first-seen order. Trailing punctuation (``,;:!?)``) is stripped
    so ``@image:foo.png.`` resolves to ``foo.png``."""
    if not text:
        return []
    seen: Dict[str, None] = {}
    for match in _IMAGE_MENTION_RE.finditer(text):
        token = match.group(1).rstrip(".,;:!?)")
        if not token or token in seen:
            continue
        seen[token] = None
    return list(seen.keys())


# ============================================================
# Attachment dataclass
# ============================================================

@dataclass
class ImageAttachment:
    """One resolved image attachment. ``ok`` distinguishes
    successful loads from failures the caller should report
    inline."""

    path: str               # original ``@image:<path>`` path as typed
    ok: bool = False
    base64_data: str = ""    # raw base64 (no data: URI prefix)
    mime_type: str = ""
    size_bytes: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        # Excludes base64_data from the dict view since it can be
        # multi-megabyte; surfaces just the metadata that's safe
        # to log / persist on execution records.
        return {
            "path": self.path,
            "ok": self.ok,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "error": self.error,
        }


# ============================================================
# Parser entry point
# ============================================================

def attach_image_mentions(
    text: str,
    workspace_path: Optional[str],
    *,
    per_image_max_bytes: int = PER_IMAGE_MAX_BYTES,
    max_images: int = MAX_IMAGES_PER_MESSAGE,
) -> Dict[str, Any]:
    """Extract ``@image:<path>`` tokens, resolve them against the
    workspace, base64-encode each one, and return the result for
    the chat flow.

    Returns a dict with the same shape as
    ``mentions_service.expand_mentions``:

      {
        "original": <input text>,
        "stripped": <input with @image:foo replaced by [image:foo]>,
        "had_workspace": bool,
        "attachments": [ImageAttachment-as-dict, ...],  # full incl. base64
        "metadata": [ImageAttachment.to_dict(), ...],   # safe-to-log subset
        "expanded": bool,                                # any successful attachment
        "summary": str,                                  # one-line human description
      }

    Conservative gating mirrors ``mentions_service``:

    * No workspace pinned → returns input unchanged with
      ``had_workspace=False``.
    * Path resolution failures (traversal, missing file, wrong
      extension) get an ``ImageAttachment`` with ``ok=False`` so
      the caller can surface "tried to attach foo.png but it
      doesn't exist" inline.
    * Size cap → ``ok=False, error="size_exceeded"`` and the
      attachment is NOT included in the model payload.
    * Count cap → matches beyond ``max_images`` are dropped from
      the parser output entirely (don't even appear as failures).
    """
    text = text or ""
    result: Dict[str, Any] = {
        "original": text,
        "stripped": text,
        "had_workspace": bool(workspace_path),
        "attachments": [],
        "metadata": [],
        "expanded": False,
        "summary": "",
    }

    paths = extract_image_mentions(text)
    if not paths:
        return result

    # Strip the @image: tokens regardless of whether they resolve;
    # the user's chat history keeps the natural mention so they
    # remember what they asked, but the model receives a cleaner
    # ``[image:foo]`` placeholder.
    stripped = text
    for path in paths:
        stripped = re.sub(
            r"(?<![\w])@image:" + re.escape(path) + r"(?=\W|$)",
            f"[image:{path}]",
            stripped,
        )
    result["stripped"] = stripped

    if not workspace_path:
        # Surface this as a per-attachment refusal so the user sees
        # "I noticed you wanted to attach an image but no workspace
        # is pinned" — better than silently dropping.
        for path in paths[:max_images]:
            attachment = ImageAttachment(
                path=path, ok=False, error="no_workspace",
            )
            result["attachments"].append(_attachment_view(attachment, full=True))
            result["metadata"].append(attachment.to_dict())
        result["summary"] = (
            f"{len(paths)} image mention(s) ignored — no workspace pinned"
        )
        return result

    successful = 0
    for path in paths[:max_images]:
        attachment = _resolve_and_encode(
            path, workspace_path, per_image_max_bytes,
        )
        if attachment.ok:
            successful += 1
        result["attachments"].append(_attachment_view(attachment, full=True))
        result["metadata"].append(attachment.to_dict())

    result["expanded"] = successful > 0
    if successful:
        result["summary"] = (
            f"{successful} image attachment(s) ready"
            + (f" ({len(paths) - successful} failed)" if successful != len(paths) else "")
        )
    else:
        result["summary"] = f"{len(paths)} image mention(s) failed to attach"
    return result


def _attachment_view(att: ImageAttachment, *, full: bool) -> Dict[str, Any]:
    """Return the attachment as a dict. ``full=True`` includes the
    base64 payload (chat_service uses this to populate Ollama's
    ``images`` field). ``full=False`` is the metadata-only subset
    safe for logging / persistence."""
    if not full:
        return att.to_dict()
    return {
        **att.to_dict(),
        "base64_data": att.base64_data,
    }


def _resolve_and_encode(
    path: str, workspace_path: str, per_image_max_bytes: int,
) -> ImageAttachment:
    """Single-attachment pipeline: resolve path, validate
    extension + size, base64-encode."""
    # Path resolution mirrors what workspace_service does for
    # mentions: refuse traversal, refuse symlinks, require the
    # resolved path to be inside the workspace.
    #
    # Use an explicit substring strip for ``./`` rather than
    # ``lstrip("./")`` — the latter is a Python footgun that
    # silently collapses ``../`` to nothing, defeating the
    # traversal check below. (Bug found in the same module's
    # cousin code via the workspace-isolation audit.)
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or ".." in cleaned.split("/"):
        return ImageAttachment(
            path=path, ok=False, error="invalid_path",
        )
    absolute = os.path.normpath(os.path.join(workspace_path, cleaned))
    workspace_abs = os.path.abspath(workspace_path)
    if not os.path.abspath(absolute).startswith(workspace_abs + os.sep) and \
       os.path.abspath(absolute) != workspace_abs:
        return ImageAttachment(
            path=path, ok=False, error="outside_workspace",
        )
    if os.path.islink(absolute):
        return ImageAttachment(
            path=path, ok=False, error="symlink_unsupported",
        )
    if not os.path.isfile(absolute):
        return ImageAttachment(
            path=path, ok=False, error="path_not_found",
        )

    ext = os.path.splitext(absolute)[1].lower()
    mime = EXTENSION_MIME_TYPES.get(ext)
    if not mime:
        return ImageAttachment(
            path=path, ok=False, error="unsupported_format",
        )

    try:
        size = os.path.getsize(absolute)
    except OSError as exc:
        return ImageAttachment(
            path=path, ok=False, error=f"stat_failed: {exc}",
        )
    if size > per_image_max_bytes:
        return ImageAttachment(
            path=path, ok=False, mime_type=mime, size_bytes=size,
            error="size_exceeded",
        )

    try:
        with open(absolute, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return ImageAttachment(
            path=path, ok=False, mime_type=mime, size_bytes=size,
            error=f"read_failed: {exc}",
        )

    try:
        b64 = base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        return ImageAttachment(
            path=path, ok=False, mime_type=mime, size_bytes=size,
            error=f"encode_failed: {exc}",
        )

    return ImageAttachment(
        path=path, ok=True,
        base64_data=b64,
        mime_type=mime,
        size_bytes=size,
    )


# ============================================================
# Apply to runtime messages
# ============================================================

def apply_to_messages(
    messages: List[Dict[str, Any]],
    attachments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Mutate ``messages`` in place: add the ``images`` field to
    the last user message with the base64 payloads of every
    successful attachment. Returns ``messages`` for fluent chaining.

    Idempotent in the sense that calling twice with the same
    attachments overwrites rather than appending — but typical use
    is one call per chat turn.

    No-op when:
    * ``messages`` is empty.
    * No attachment has ``ok=True``.
    * The last message isn't a ``user`` role (defensive — the
      assembly always ends with one).
    """
    if not messages or not attachments:
        return messages
    successful_b64 = [
        att.get("base64_data")
        for att in attachments
        if att.get("ok") and att.get("base64_data")
    ]
    if not successful_b64:
        return messages
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return messages
    last["images"] = list(successful_b64)
    return messages


# ============================================================
# Phase 3b: router bias hint
# ============================================================

#: Bias added per ``@image:`` token (1-based count) to the
#: ``vision`` lane in the router's score table. Total cap below
#: keeps the bias additive but bounded.
PER_IMAGE_VISION_BIAS = 5

#: Cap on the total ``vision`` bias from image mentions. Two
#: images already strongly imply a vision query; beyond that,
#: more tokens don't justify a runaway boost.
MAX_VISION_BIAS = 10


def vision_lane_bias(text: Optional[str]) -> int:
    """Count ``@image:`` tokens in ``text`` and return the
    suggested ``vision`` lane bias for the router. Phase 3b of
    ``docs/design/vision-input.md``.

    Returns 0 when no tokens; otherwise
    ``min(count * PER_IMAGE_VISION_BIAS, MAX_VISION_BIAS)``.

    Counts INTENT — token presence in the user's text — not
    successful attachment, because the bias should fire even if
    the file path turns out to be wrong; the router's job is to
    pick the right LANE based on what the user is asking for. The
    capability gate in ``chat_service`` handles the actual
    attachment failures (Phase 3a).
    """
    count = len(extract_image_mentions(text))
    if count <= 0:
        return 0
    return min(count * PER_IMAGE_VISION_BIAS, MAX_VISION_BIAS)


def format_attachment_summary(attachments: List[Dict[str, Any]]) -> str:
    """Render a one-line summary of the attachment list for
    execution metadata / chat-flow event payloads. Skips the
    base64 to keep logs small.

    Format: ``"foo.png (image/png, 12.3 KB), bar.jpg (FAIL: size_exceeded)"``
    """
    bits: List[str] = []
    for att in attachments or []:
        path = att.get("path") or "?"
        if att.get("ok"):
            mime = att.get("mime_type") or "image/?"
            size_kb = (att.get("size_bytes") or 0) / 1024
            bits.append(f"{path} ({mime}, {size_kb:.1f} KB)")
        else:
            err = att.get("error") or "failed"
            bits.append(f"{path} (FAIL: {err})")
    return ", ".join(bits)
