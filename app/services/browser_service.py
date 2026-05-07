"""Playwright-driven browser skill (Phase 1 — read-only).

See ``docs/design/browser-automation.md``. The shipped surface:

  - ``open(url)`` / ``navigate(url)`` — load a page (per-origin
    approval gate enforced by the dispatcher BEFORE this layer)
  - ``screenshot(selector?)`` — capture a PNG to a cache dir
  - ``get_text(selector?)`` — innerText of the page or selector
  - ``get_html(selector?)`` — outerHTML of the page or selector
  - ``close()`` — tear down the browser context

Hard rules from the design doc:
  - Local browser only — Playwright's bundled Chromium / Firefox.
    We do not proxy through any remote service.
  - Per-origin approval — checked at the dispatcher layer, not here.
  - Fresh isolated ``BrowserContext`` per session by default. No
    cookies, no logged-in identity. Persistence is a Phase 2 add-on.
  - No cross-origin script injection — Phase 1 is **read-only**, so
    `eval` / `fill` / `click` aren't part of this module yet.

Architecture
------------
- ``BrowserBackend`` is the abstract interface.
- ``FakeBrowserBackend`` is a deterministic in-memory implementation
  used by tests AND as a fallback when Playwright isn't installed
  (so the rest of the system keeps working — calls just return
  ``playwright_not_installed`` until the user installs it).
- ``PlaywrightBackend`` lazy-imports the real ``playwright.sync_api``
  on first use; if the import fails, we surface the canonical
  ``playwright_not_installed`` reason and never raise.
- ``BrowserPool`` keeps one open ``BrowserContext`` per session id.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


# ============================================================
# Cache + URL helpers
# ============================================================

#: Where ``screenshot`` writes its PNGs. Relative to the project root
#: by default; override via ``BROWSER_CACHE_DIR`` env var (mostly for
#: tests and CI).
BROWSER_CACHE_DIR = os.environ.get(
    "BROWSER_CACHE_DIR",
    os.path.join(os.getcwd(), "_browser_cache"),
)


def parse_origin(url: str) -> Optional[str]:
    """Extract ``scheme://host[:port]`` from a URL string. Returns
    ``None`` if the URL is malformed (no scheme or no host)."""
    if not url:
        return None
    try:
        parsed = urlparse(str(url).strip())
    except Exception:  # noqa: BLE001
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    origin = f"{parsed.scheme.lower()}://{parsed.hostname.lower()}"
    if parsed.port:
        origin += f":{parsed.port}"
    return origin


def _cache_path_for(url: str, suffix: str = ".png") -> str:
    os.makedirs(BROWSER_CACHE_DIR, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(BROWSER_CACHE_DIR, f"{digest}{suffix}")


# ============================================================
# Backend Protocol
# ============================================================

class BrowserBackend(Protocol):
    """Abstract interface every backend must satisfy."""

    name: str
    available: bool

    def open_page(self, browser_session_id: str, url: str) -> Dict[str, Any]: ...
    def navigate(self, browser_session_id: str, url: str) -> Dict[str, Any]: ...
    def screenshot(
        self, browser_session_id: str, selector: Optional[str] = None
    ) -> Dict[str, Any]: ...
    def get_text(
        self, browser_session_id: str, selector: Optional[str] = None
    ) -> Dict[str, Any]: ...
    def get_html(
        self, browser_session_id: str, selector: Optional[str] = None
    ) -> Dict[str, Any]: ...
    def close(self, browser_session_id: str) -> Dict[str, Any]: ...
    def list_sessions(self) -> List[str]: ...


# ============================================================
# Fake backend (tests + Playwright-not-installed fallback)
# ============================================================

@dataclass
class _FakePage:
    """Minimal in-memory Page model for the FakeBackend."""
    url: str = "about:blank"
    title: str = "about:blank"
    text: str = ""
    html: str = "<html><body></body></html>"
    selectors: Dict[str, str] = field(default_factory=dict)


class FakeBrowserBackend:
    """Deterministic in-memory backend.

    Tests register canned responses via ``register_page(url, ...)``;
    every navigate to that URL returns the canned page. Unregistered
    URLs return a stub page indicating the host. Screenshot writes a
    1-byte sentinel file so callers can prove the path is real.
    """

    name = "fake"
    available = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: Dict[str, _FakePage] = {}
        self._sessions: Dict[str, _FakePage] = {}  # session_id -> current page

    # --- Test API ------------------------------------------------

    def register_page(
        self, url: str, *, title: str = "", text: str = "", html: str = "",
        selectors: Optional[Dict[str, str]] = None,
    ) -> None:
        """Pre-load a canned page. Tests call this before navigate."""
        page = _FakePage(
            url=url,
            title=title or url,
            text=text or "",
            html=html or f"<html><head><title>{title or url}</title></head><body>{text or ''}</body></html>",
            selectors=selectors or {},
        )
        with self._lock:
            self._pages[url] = page

    # --- Public backend API --------------------------------------

    def open_page(self, browser_session_id: str, url: str) -> Dict[str, Any]:
        return self.navigate(browser_session_id, url)

    def navigate(self, browser_session_id: str, url: str) -> Dict[str, Any]:
        with self._lock:
            page = self._pages.get(url)
            if page is None:
                # Synthesise a minimal page for unregistered URLs so
                # tests can still drive the flow without pre-registering
                # every URL.
                origin = parse_origin(url) or "about:blank"
                page = _FakePage(
                    url=url, title=f"(stub for {origin})",
                    text="", html=f"<html><body>stub for {url}</body></html>",
                )
            self._sessions[browser_session_id] = page
        return {
            "ok": True,
            "url": page.url,
            "title": page.title,
            "session_id": browser_session_id,
        }

    def screenshot(
        self, browser_session_id: str, selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            page = self._sessions.get(browser_session_id)
        if page is None:
            return {"ok": False, "error": "no_active_page"}
        path = _cache_path_for(page.url, suffix=".png")
        # Write a 1-byte sentinel so the path actually exists for
        # callers that assert file presence.
        try:
            with open(path, "wb") as fh:
                fh.write(b"\x00")
        except OSError as exc:
            return {"ok": False, "error": f"io_error: {exc}"}
        return {
            "ok": True,
            "url": page.url,
            "screenshot_path": path,
            "selector": selector,
        }

    def get_text(
        self, browser_session_id: str, selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            page = self._sessions.get(browser_session_id)
        if page is None:
            return {"ok": False, "error": "no_active_page"}
        if selector:
            value = page.selectors.get(selector, "")
            return {"ok": True, "url": page.url, "text": value, "selector": selector}
        return {"ok": True, "url": page.url, "text": page.text}

    def get_html(
        self, browser_session_id: str, selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            page = self._sessions.get(browser_session_id)
        if page is None:
            return {"ok": False, "error": "no_active_page"}
        if selector:
            value = page.selectors.get(selector, "")
            return {"ok": True, "url": page.url, "html": value, "selector": selector}
        return {"ok": True, "url": page.url, "html": page.html}

    def close(self, browser_session_id: str) -> Dict[str, Any]:
        with self._lock:
            removed = self._sessions.pop(browser_session_id, None)
        return {"ok": True, "closed": browser_session_id, "had_page": removed is not None}

    def list_sessions(self) -> List[str]:
        with self._lock:
            return sorted(self._sessions.keys())


# ============================================================
# Playwright backend (real, lazy-imported)
# ============================================================

class _PlaywrightUnavailableBackend:
    """Stub that always reports `playwright_not_installed`. Returned by
    ``get_default_backend()`` when the real Playwright import fails."""

    name = "unavailable"
    available = False

    def _err(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": "playwright_not_installed",
            "hint": (
                "Install with: `pip install playwright` and then "
                "`playwright install chromium` (~150 MB). Phase 1 of "
                "browser-automation; tests use the fake backend."
            ),
        }

    def open_page(self, *_a, **_kw): return self._err()
    def navigate(self, *_a, **_kw): return self._err()
    def screenshot(self, *_a, **_kw): return self._err()
    def get_text(self, *_a, **_kw): return self._err()
    def get_html(self, *_a, **_kw): return self._err()
    def close(self, *_a, **_kw): return self._err()
    def list_sessions(self): return []


class PlaywrightBackend:
    """Sync-API Playwright backend. Lazy-imports on first use.

    Keep this thin: each call resolves a per-session ``BrowserContext``
    on demand and routes through it. Heavy lifting lives in Playwright.
    """

    name = "playwright"

    def __init__(self) -> None:
        self.available = True
        self._lock = threading.Lock()
        self._playwright = None
        self._browser = None
        # session_id -> {"context": BrowserContext, "page": Page}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def _ensure_browser(self):
        """Lazy-start the Playwright runtime + a single Chromium browser."""
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("playwright_not_installed") from exc
        self._playwright = sync_playwright().start()
        # Headless by default — design doc anti-pattern: "Headful mode
        # by default. Default headless. Headful is opt-in for debugging."
        self._browser = self._playwright.chromium.launch(headless=True)

    def _ensure_session(self, browser_session_id: str):
        with self._lock:
            sess = self._sessions.get(browser_session_id)
            if sess is not None:
                return sess
            self._ensure_browser()
            context = self._browser.new_context()
            page = context.new_page()
            sess = {"context": context, "page": page}
            self._sessions[browser_session_id] = sess
            return sess

    def open_page(self, browser_session_id: str, url: str) -> Dict[str, Any]:
        return self.navigate(browser_session_id, url)

    def navigate(self, browser_session_id: str, url: str) -> Dict[str, Any]:
        try:
            sess = self._ensure_session(browser_session_id)
            page = sess["page"]
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return {
                "ok": True,
                "url": page.url,
                "title": page.title(),
                "session_id": browser_session_id,
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("PlaywrightBackend.navigate failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def screenshot(
        self, browser_session_id: str, selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            sess = self._sessions.get(browser_session_id)
        if sess is None:
            return {"ok": False, "error": "no_active_page"}
        try:
            page = sess["page"]
            target = page.locator(selector) if selector else page
            path = _cache_path_for(page.url, suffix=".png")
            target.screenshot(path=path)
            return {
                "ok": True,
                "url": page.url,
                "screenshot_path": path,
                "selector": selector,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def get_text(
        self, browser_session_id: str, selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            sess = self._sessions.get(browser_session_id)
        if sess is None:
            return {"ok": False, "error": "no_active_page"}
        try:
            page = sess["page"]
            text = page.locator(selector).inner_text() if selector else page.inner_text("body")
            return {
                "ok": True,
                "url": page.url,
                "text": text,
                "selector": selector,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def get_html(
        self, browser_session_id: str, selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            sess = self._sessions.get(browser_session_id)
        if sess is None:
            return {"ok": False, "error": "no_active_page"}
        try:
            page = sess["page"]
            html = page.locator(selector).inner_html() if selector else page.content()
            return {
                "ok": True,
                "url": page.url,
                "html": html,
                "selector": selector,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def close(self, browser_session_id: str) -> Dict[str, Any]:
        with self._lock:
            sess = self._sessions.pop(browser_session_id, None)
        if sess is None:
            return {"ok": True, "closed": browser_session_id, "had_page": False}
        try:
            sess["context"].close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "closed": browser_session_id, "had_page": True}

    def list_sessions(self) -> List[str]:
        with self._lock:
            return sorted(self._sessions.keys())


# ============================================================
# Module-level backend (swap in tests via `set_backend`)
# ============================================================

_backend: Optional[BrowserBackend] = None
_backend_lock = threading.Lock()


def get_default_backend() -> BrowserBackend:
    """Return the active backend, creating one on first call.

    Picks Playwright if installed, otherwise the unavailable stub.
    Tests bypass this entirely via ``set_backend(FakeBrowserBackend())``.
    """
    global _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        try:
            import playwright  # noqa: F401  — probe only
            _backend = PlaywrightBackend()
        except ImportError:
            _backend = _PlaywrightUnavailableBackend()
        return _backend


def set_backend(backend: Optional[BrowserBackend]) -> None:
    """Test hook: swap the active backend (or reset with ``None``)."""
    global _backend
    with _backend_lock:
        _backend = backend


def reset_for_tests() -> None:
    """Drop the active backend so the next call re-creates one."""
    set_backend(None)
