"""ImageDropInput · Qt drag-and-drop image attachment widget.

Phase 2 of `docs/design/vision-input.md`. Subclass of ``QLineEdit``
that accepts dragged image files and inserts ``@image:<rel-path>``
tokens at the cursor. The model + chat_service layer (Phase 1) does
the actual base64 attachment; this widget is just the friendly
drag-drop affordance for users who don't want to type the path.

Drop rules (mirrors ``vision_service`` Phase 1 validation):

* File must have a recognised image extension
  (``.png / .jpg / .jpeg / .gif / .webp / .bmp``).
* File must be inside the pinned workspace (relative path is
  computed from the workspace root). Out-of-workspace drops are
  refused with an ``image_drop_rejected`` signal so the main
  window can surface the reason inline.
* Multiple files dropped together are inserted in sequence,
  separated by single spaces, capped at
  ``vision_service.MAX_IMAGES_PER_MESSAGE``.

The widget is workspace-aware via an injected callable
(``workspace_path_provider``) so it stays decoupled from
``local_memory`` and testable without a session fixture.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QLineEdit

from app.services import vision_service


#: Reasons a drop can be rejected (per file). Surfaced via the
#: ``image_drop_rejected`` signal so the host UI can show the user
#: what went wrong.
REJECT_NO_WORKSPACE = "no_workspace"
REJECT_OUTSIDE_WORKSPACE = "outside_workspace"
REJECT_UNSUPPORTED_FORMAT = "unsupported_format"
REJECT_NOT_A_FILE = "not_a_file"


class ImageDropInput(QLineEdit):
    """``QLineEdit`` that turns dragged image files into
    ``@image:<rel-path>`` tokens.

    ``workspace_path_provider`` is a zero-arg callable that returns
    the currently-pinned workspace path (or None). The widget
    re-queries it on every drop so workspace pin changes take
    effect immediately.
    """

    #: Emitted when at least one file was successfully resolved
    #: and inserted. Carries the relative path that was inserted.
    image_dropped = Signal(str)

    #: Emitted when a dropped file was refused. Carries
    #: ``(absolute_path, reason)``. Reasons are the
    #: ``REJECT_*`` constants in this module.
    image_drop_rejected = Signal(str, str)

    def __init__(
        self,
        workspace_path_provider: Callable[[], Optional[str]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._workspace_provider = workspace_path_provider
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------
    # Qt drag-drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt API)
        """Accept the drag if the MIME data contains at least one
        local file with a recognised image extension. Don't probe
        the workspace yet — that happens at drop time so a
        workspace pinned mid-drag still works."""
        mime = event.mimeData()
        if not mime.hasUrls():
            super().dragEnterEvent(event)
            return
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            ext = os.path.splitext(url.toLocalFile())[1].lower()
            if ext in vision_service.EXTENSION_MIME_TYPES:
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt API)
        """Resolve every dropped file against the pinned workspace
        and insert ``@image:<rel-path>`` tokens. Delegates to
        ``handle_dropped_paths`` so unit tests can drive the same
        logic without constructing a synthetic ``QDropEvent`` (which
        is awkward / unstable across PySide6 versions)."""
        mime = event.mimeData()
        if not mime.hasUrls():
            super().dropEvent(event)
            return
        paths = [
            url.toLocalFile()
            for url in mime.urls()
            if url.isLocalFile()
        ]
        self.handle_dropped_paths(paths)
        event.acceptProposedAction()

    def handle_dropped_paths(self, paths: List[str]) -> None:
        """Public entry point used by ``dropEvent`` AND by tests.
        Resolves each path against the pinned workspace; on success
        inserts an ``@image:<rel-path>`` token; on failure emits
        ``image_drop_rejected``. Per-file failures don't abort the
        rest — valid files in the same batch still go through.

        Caps the number of inserted tokens at
        ``vision_service.MAX_IMAGES_PER_MESSAGE``; rejects beyond
        the cap are silently dropped (no signal, since the user
        already exceeded what one chat turn can carry)."""
        workspace_path = None
        try:
            workspace_path = self._workspace_provider()
        except Exception:  # noqa: BLE001
            workspace_path = None

        tokens: List[str] = []
        max_per_drop = vision_service.MAX_IMAGES_PER_MESSAGE

        for absolute in paths:
            if len(tokens) >= max_per_drop:
                break
            if not absolute:
                continue
            rel, reason = self._resolve_drop(absolute, workspace_path)
            if reason is not None:
                self.image_drop_rejected.emit(absolute, reason)
                continue
            tokens.append(f"@image:{rel}")
            self.image_dropped.emit(rel)

        if tokens:
            self._insert_tokens_at_cursor(tokens)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve_drop(
        self, absolute: str, workspace_path: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Validate one dropped file. Returns
        ``(relative_path, None)`` on success or
        ``(None, REJECT_*)`` on failure."""
        if not absolute:
            return None, REJECT_NOT_A_FILE
        if not os.path.isfile(absolute):
            return None, REJECT_NOT_A_FILE
        ext = os.path.splitext(absolute)[1].lower()
        if ext not in vision_service.EXTENSION_MIME_TYPES:
            return None, REJECT_UNSUPPORTED_FORMAT
        if not workspace_path:
            return None, REJECT_NO_WORKSPACE
        try:
            workspace_abs = os.path.abspath(workspace_path)
            file_abs = os.path.abspath(absolute)
            common = os.path.commonpath([workspace_abs, file_abs])
        except ValueError:
            # commonpath raises across Windows drives → outside.
            return None, REJECT_OUTSIDE_WORKSPACE
        if common != workspace_abs:
            return None, REJECT_OUTSIDE_WORKSPACE
        rel = os.path.relpath(file_abs, workspace_abs).replace("\\", "/")
        return rel, None

    # ------------------------------------------------------------------
    # Insertion
    # ------------------------------------------------------------------

    def _insert_tokens_at_cursor(self, tokens: List[str]) -> None:
        """Insert the tokens at the current cursor position. Pads
        with a leading + trailing space when needed so adjacent
        text doesn't fuse with the @image: prefix.

        Mirrors how a user would type — the goal is for the input
        to look like the user typed `@image:foo.png ` themselves."""
        if not tokens:
            return
        joined = " ".join(tokens)
        text = self.text()
        cursor = self.cursorPosition()
        # Ensure a single space before the inserted token if there
        # isn't one already and we're not at the start.
        prefix_pad = ""
        if cursor > 0 and cursor <= len(text) and text[cursor - 1] not in (" ", "\t"):
            prefix_pad = " "
        # And a single trailing space so the user can keep typing.
        suffix_pad = ""
        if cursor < len(text) and text[cursor] not in (" ", "\t"):
            suffix_pad = " "
        elif cursor == len(text):
            suffix_pad = " "
        new_text = (
            text[:cursor] + prefix_pad + joined + suffix_pad + text[cursor:]
        )
        self.setText(new_text)
        # Move cursor to just after the trailing space so the user
        # keeps typing where they left off.
        new_cursor = cursor + len(prefix_pad) + len(joined) + len(suffix_pad)
        self.setCursorPosition(new_cursor)
