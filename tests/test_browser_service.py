"""Tests for browser-automation Phase 1 (`docs/design/browser-automation.md`).

Five layers:
  - URL helpers (`parse_origin`)
  - FakeBackend behaviour (navigate/screenshot/get_text/get_html/close)
  - Storage round-trip (approved_origins + page_snapshot)
  - Dispatcher commands with the FakeBackend swapped in
  - End-to-end approval flow: first navigate proposes; approve adds the
    origin; subsequent navigates on the same origin run immediately
"""
from __future__ import annotations

import os
import tempfile
import unittest

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import browser_service as bs
from app.services.command_dispatcher import dispatch_input
from app.services.gateway.bus import (
    KIND_BROWSER,
    default_bus,
    reset_default_bus_for_tests,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# ============================================================
# parse_origin
# ============================================================

class ParseOriginTests(unittest.TestCase):
    def test_simple_https(self):
        self.assertEqual(
            bs.parse_origin("https://github.com/anthropics"),
            "https://github.com",
        )

    def test_with_explicit_port(self):
        self.assertEqual(
            bs.parse_origin("http://localhost:7780/health"),
            "http://localhost:7780",
        )

    def test_normalises_case(self):
        self.assertEqual(
            bs.parse_origin("HTTPS://EXAMPLE.com/PATH"),
            "https://example.com",
        )

    def test_returns_none_for_malformed(self):
        self.assertIsNone(bs.parse_origin(""))
        self.assertIsNone(bs.parse_origin("not a url"))
        self.assertIsNone(bs.parse_origin("/just/a/path"))


# ============================================================
# FakeBackend behaviour
# ============================================================

class FakeBackendTests(unittest.TestCase):
    def setUp(self):
        self.cache_tmp = tempfile.TemporaryDirectory()
        self._orig_cache = bs.BROWSER_CACHE_DIR
        bs.BROWSER_CACHE_DIR = self.cache_tmp.name

    def tearDown(self):
        bs.BROWSER_CACHE_DIR = self._orig_cache
        self.cache_tmp.cleanup()

    def test_navigate_to_registered_page_returns_canned_data(self):
        backend = bs.FakeBrowserBackend()
        backend.register_page("https://example.com/", title="Example", text="hello")
        result = backend.navigate("s1", "https://example.com/")
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Example")

    def test_navigate_to_unregistered_url_returns_stub(self):
        backend = bs.FakeBrowserBackend()
        result = backend.navigate("s1", "https://random.example/")
        self.assertTrue(result["ok"])
        # Stub title contains the origin to make tests obvious.
        self.assertIn("random.example", result["title"])

    def test_get_text_without_navigate_errors(self):
        backend = bs.FakeBrowserBackend()
        result = backend.get_text("never-opened")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_active_page")

    def test_get_text_returns_canned_text(self):
        backend = bs.FakeBrowserBackend()
        backend.register_page("https://example.com/", title="X", text="body text")
        backend.navigate("s1", "https://example.com/")
        result = backend.get_text("s1")
        self.assertEqual(result["text"], "body text")

    def test_get_text_with_selector_uses_selectors_dict(self):
        backend = bs.FakeBrowserBackend()
        backend.register_page(
            "https://x/", text="full text",
            selectors={"h1": "Heading"},
        )
        backend.navigate("s1", "https://x/")
        result = backend.get_text("s1", selector="h1")
        self.assertEqual(result["text"], "Heading")

    def test_screenshot_writes_a_file(self):
        backend = bs.FakeBrowserBackend()
        backend.register_page("https://example.com/", title="X", text="hi")
        backend.navigate("s1", "https://example.com/")
        result = backend.screenshot("s1")
        self.assertTrue(result["ok"])
        self.assertTrue(os.path.exists(result["screenshot_path"]))

    def test_close_drops_session(self):
        backend = bs.FakeBrowserBackend()
        backend.register_page("https://x/", text="x")
        backend.navigate("s1", "https://x/")
        self.assertIn("s1", backend.list_sessions())
        backend.close("s1")
        self.assertNotIn("s1", backend.list_sessions())

    def test_list_sessions_initially_empty(self):
        backend = bs.FakeBrowserBackend()
        self.assertEqual(backend.list_sessions(), [])


# ============================================================
# PlaywrightUnavailableBackend (graceful fallback)
# ============================================================

class UnavailableBackendTests(unittest.TestCase):
    def test_all_methods_return_playwright_not_installed(self):
        backend = bs._PlaywrightUnavailableBackend()
        for fn_name in ("open_page", "navigate", "screenshot", "get_text", "get_html", "close"):
            method = getattr(backend, fn_name)
            try:
                result = method("session", "url")
            except TypeError:
                # close() takes only session_id
                result = method("session")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "playwright_not_installed")

    def test_available_flag_is_false(self):
        backend = bs._PlaywrightUnavailableBackend()
        self.assertFalse(backend.available)


# ============================================================
# Storage helpers
# ============================================================

class BrowserStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_default_state_is_empty(self):
        state = local_memory.get_browser_state("default")
        self.assertEqual(state["approved_origins"], [])
        self.assertIsNone(state["active_session_id"])
        self.assertIsNone(state["page_snapshot"])

    def test_add_approved_origin_dedups_and_lowercases(self):
        local_memory.add_approved_origin("default", "https://Example.com")
        local_memory.add_approved_origin("default", "https://example.com")
        local_memory.add_approved_origin("default", "https://Other.org")
        approved = local_memory.get_approved_origins("default")
        self.assertEqual(approved, ["https://example.com", "https://other.org"])

    def test_remove_approved_origin(self):
        local_memory.add_approved_origin("default", "https://x.com")
        self.assertTrue(local_memory.remove_approved_origin("default", "https://x.com"))
        self.assertFalse(local_memory.remove_approved_origin("default", "https://x.com"))

    def test_record_page_snapshot(self):
        local_memory.record_page_snapshot(
            "default",
            snapshot={
                "url": "https://x.com",
                "title": "X",
                "screenshot_path": "/tmp/x.png",
                "text_excerpt": "hello",
            },
        )
        snap = local_memory.get_browser_state("default")["page_snapshot"]
        self.assertEqual(snap["url"], "https://x.com")
        self.assertEqual(snap["title"], "X")
        self.assertIn("captured_at", snap)


# ============================================================
# Dispatcher integration
# ============================================================

class BrowserDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        self.cache_tmp = tempfile.TemporaryDirectory()
        self._orig_cache = bs.BROWSER_CACHE_DIR
        bs.BROWSER_CACHE_DIR = self.cache_tmp.name
        # Wire the fake backend.
        self.fake = bs.FakeBrowserBackend()
        bs.set_backend(self.fake)
        reset_default_bus_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        bs.reset_for_tests()
        bs.BROWSER_CACHE_DIR = self._orig_cache
        self.cache_tmp.cleanup()
        reset_default_bus_for_tests()

    # --- info / list / unknown -----------------------------------

    def test_browser_info(self):
        ev = dispatch_input("browser", session_id="default")
        self.assertIn("backend=fake", ev["output"])
        self.assertIn("Approved origins", ev["output"])

    def test_browser_unknown_subcommand(self):
        ev = dispatch_input("browser hax", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    # --- approval flow on first navigate -------------------------

    def test_first_navigate_emits_approval_required(self):
        self.fake.register_page("https://example.com/", title="X", text="hi")
        ev = dispatch_input("browser open https://example.com/", session_id="default")
        self.assertEqual(ev.get("reason"), "approval_required")
        self.assertEqual(ev["pending_action"]["kind"], "browser_navigate")
        self.assertEqual(ev["pending_action"]["origin_host"], "https://example.com")

    def test_invalid_url_emits_invalid_argument(self):
        ev = dispatch_input("browser navigate not-a-url", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_navigate_without_url_emits_invalid_argument(self):
        ev = dispatch_input("browser navigate", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_approve_adds_origin_and_runs_navigate(self):
        self.fake.register_page("https://example.com/", title="Example", text="hi")
        dispatch_input("browser open https://example.com/", session_id="default")
        approve = dispatch_input("approve", session_id="default")
        self.assertNotIn("reason", approve)
        self.assertIn("Loaded 'https://example.com/'", approve["output"])
        # Origin added to whitelist.
        self.assertIn(
            "https://example.com",
            local_memory.get_approved_origins("default"),
        )

    def test_subsequent_navigate_within_approved_origin_skips_approval(self):
        self.fake.register_page("https://example.com/", title="X", text="hi")
        dispatch_input("browser open https://example.com/", session_id="default")
        dispatch_input("approve", session_id="default")
        # Now visit another path on the same origin — should NOT prompt.
        self.fake.register_page("https://example.com/page2", title="P2", text="x")
        ev = dispatch_input("browser navigate https://example.com/page2", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertIn("page2", ev["output"])

    def test_navigate_to_different_origin_re_prompts(self):
        self.fake.register_page("https://a.com/", title="A", text="a")
        self.fake.register_page("https://b.com/", title="B", text="b")
        dispatch_input("browser open https://a.com/", session_id="default")
        dispatch_input("approve", session_id="default")
        # Different origin must re-prompt.
        ev = dispatch_input("browser navigate https://b.com/", session_id="default")
        self.assertEqual(ev.get("reason"), "approval_required")

    # --- inspect commands ----------------------------------------

    def test_get_text_without_active_page_returns_invalid_argument(self):
        ev = dispatch_input("browser get-text", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_get_text_after_navigate(self):
        self.fake.register_page("https://x/", title="X", text="page body")
        dispatch_input("browser open https://x/", session_id="default")
        dispatch_input("approve", session_id="default")
        ev = dispatch_input("browser get-text", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertIn("page body", ev["output"])

    def test_screenshot_after_navigate_writes_file(self):
        self.fake.register_page("https://x/", title="X", text="x")
        dispatch_input("browser open https://x/", session_id="default")
        dispatch_input("approve", session_id="default")
        ev = dispatch_input("browser screenshot", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertTrue(os.path.exists(ev["result"]["screenshot_path"]))

    def test_close_clears_active_session(self):
        self.fake.register_page("https://x/", title="X", text="x")
        dispatch_input("browser open https://x/", session_id="default")
        dispatch_input("approve", session_id="default")
        ev = dispatch_input("browser close", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertIsNone(local_memory.get_browser_state("default")["active_session_id"])

    # --- bus events ---------------------------------------------

    def test_navigate_publishes_kind_browser_event(self):
        self.fake.register_page("https://x/", title="X", text="x")
        captured = []
        bus = default_bus()
        bus.subscribe(bus.ANY, lambda e: captured.append(e))

        dispatch_input("browser open https://x/", session_id="default")
        dispatch_input("approve", session_id="default")

        browser_events = [e for e in captured if e.kind == KIND_BROWSER]
        self.assertGreater(len(browser_events), 0)
        navigate_events = [
            e for e in browser_events if e.payload.get("event") == "navigate"
        ]
        self.assertEqual(len(navigate_events), 1)
        self.assertEqual(navigate_events[0].payload["url"], "https://x/")

    # --- unavailable-backend fallback ----------------------------

    def test_navigate_with_unavailable_backend_emits_playwright_not_installed(self):
        bs.set_backend(bs._PlaywrightUnavailableBackend())
        ev = dispatch_input("browser open https://x.com/", session_id="default")
        self.assertEqual(ev.get("reason"), "playwright_not_installed")


if __name__ == "__main__":
    unittest.main()
