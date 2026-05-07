"""Tests for the gateway layer (EventBus + HTTP server + dispatcher hook).

The bus tests are pure-Python. The HTTP tests bind to a random ephemeral
port on 127.0.0.1 and use stdlib urllib to drive real HTTP requests so we
exercise the full code path (framing, routing, JSON serialisation, SSE).
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services.command_dispatcher import dispatch_input
from app.services.gateway import (
    GatewayEvent,
    GatewayServer,
    KIND_DISPATCH,
    TOKEN_ENV,
    default_bus,
    reset_default_bus_for_tests,
    reset_singleton_for_tests,
)


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


def _free_port():
    """Pick an ephemeral port the OS just gave back."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ============================================================
# EventBus
# ============================================================

class EventBusTests(unittest.TestCase):
    def setUp(self):
        reset_default_bus_for_tests()
        self.bus = default_bus()

    def tearDown(self):
        reset_default_bus_for_tests()

    def test_publish_delivers_to_session_subscribers(self):
        received = []
        self.bus.subscribe("default", received.append)
        self.bus.publish(GatewayEvent(kind="dispatch", session_id="default", payload={"x": 1}))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload, {"x": 1})

    def test_publish_does_not_leak_across_sessions(self):
        a, b = [], []
        self.bus.subscribe("alpha", a.append)
        self.bus.subscribe("beta", b.append)
        self.bus.publish(GatewayEvent(kind="dispatch", session_id="alpha"))
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 0)

    def test_any_subscriber_sees_every_event(self):
        every = []
        self.bus.subscribe(self.bus.ANY, every.append)
        self.bus.publish(GatewayEvent(kind="dispatch", session_id="alpha"))
        self.bus.publish(GatewayEvent(kind="dispatch", session_id="beta"))
        self.bus.publish(GatewayEvent(kind="task", session_id=None))
        self.assertEqual(len(every), 3)

    def test_unsubscribe_drops_callback(self):
        received = []
        sub_id = self.bus.subscribe("x", received.append)
        self.assertTrue(self.bus.unsubscribe(sub_id))
        self.bus.publish(GatewayEvent(kind="dispatch", session_id="x"))
        self.assertEqual(received, [])

    def test_unsubscribe_unknown_id_returns_false(self):
        self.assertFalse(self.bus.unsubscribe(999_999))

    def test_subscriber_exception_does_not_break_others(self):
        good = []

        def angry(_event):
            raise RuntimeError("boom")

        self.bus.subscribe("x", angry)
        self.bus.subscribe("x", good.append)
        delivered = self.bus.publish(GatewayEvent(kind="dispatch", session_id="x"))
        # Both attempted; one succeeded (good.append), one raised
        self.assertEqual(len(good), 1)
        self.assertEqual(delivered, 1)

    def test_publish_stamps_timestamp(self):
        captured = []
        self.bus.subscribe("x", captured.append)
        self.bus.publish(GatewayEvent(kind="dispatch", session_id="x"))
        self.assertGreater(captured[0].timestamp, 0)

    def test_subscriber_count(self):
        self.assertEqual(self.bus.subscriber_count(), 0)
        self.bus.subscribe("a", lambda _e: None)
        self.bus.subscribe("a", lambda _e: None)
        self.bus.subscribe("b", lambda _e: None)
        self.assertEqual(self.bus.subscriber_count(), 3)
        self.assertEqual(self.bus.subscriber_count("a"), 2)
        self.assertEqual(self.bus.subscriber_count("b"), 1)
        self.assertEqual(self.bus.subscriber_count("ghost"), 0)


# ============================================================
# Dispatcher → Bus integration
# ============================================================

class DispatcherPublishesEventsTests(unittest.TestCase):
    def setUp(self):
        reset_default_bus_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        reset_default_bus_for_tests()
        self._tmp.cleanup()

    def test_every_dispatch_publishes_one_event(self):
        received = []
        default_bus().subscribe("default", received.append)
        dispatch_input("help", session_id="default")
        # The publish is best-effort/non-blocking but synchronous within dispatch_input
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].kind, KIND_DISPATCH)
        self.assertEqual(received[0].payload["action"], "help")

    def test_publish_failure_does_not_break_dispatch(self):
        # A subscriber that raises must not abort dispatch.
        default_bus().subscribe("default", lambda _e: (_ for _ in ()).throw(RuntimeError("x")))
        # Should NOT raise.
        ev = dispatch_input("help", session_id="default")
        self.assertEqual(ev["action"], "help")


# ============================================================
# HTTP gateway
# ============================================================

class HttpGatewayTests(unittest.TestCase):
    def setUp(self):
        reset_default_bus_for_tests()
        reset_singleton_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        # Prevent stale env from previous test
        os.environ.pop(TOKEN_ENV, None)
        self.port = _free_port()
        self.server = GatewayServer(host="127.0.0.1", port=self.port)
        result = self.server.start()
        self.assertTrue(result["ok"], f"server failed to start: {result}")

    def tearDown(self):
        self.server.stop()
        local_memory._reset_storage_for_tests(None)
        reset_default_bus_for_tests()
        reset_singleton_for_tests()
        os.environ.pop(TOKEN_ENV, None)
        self._tmp.cleanup()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get_json(self, path, headers=None):
        req = urllib.request.Request(self._url(path), headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _post_json(self, path, body, headers=None):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url(path), data=data, method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health_endpoint_returns_ok(self):
        status, body = self._get_json("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_post_dispatch_runs_command(self):
        status, body = self._post_json("/dispatch", {"session_id": "default", "text": "help"})
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "help")

    def test_post_dispatch_rejects_missing_text(self):
        try:
            self._post_json("/dispatch", {"session_id": "x"})
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_get_session_state_snapshot(self):
        # Seed a small history
        local_memory.append_message("user", "hello", session_id="default")
        status, body = self._get_json("/sessions/default/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], "default")
        self.assertGreaterEqual(len(body["recent_history"]), 1)
        self.assertIn("context", body)
        self.assertIn("inbox", body)

    def test_unknown_path_returns_404(self):
        try:
            self._get_json("/no-such-thing")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_token_auth_when_env_set(self):
        # Restart the server with a token configured.
        self.server.stop()
        os.environ[TOKEN_ENV] = "secret-xyz"
        self.server = GatewayServer(host="127.0.0.1", port=_free_port())
        self.assertTrue(self.server.start()["ok"])
        self.port = self.server.port
        # Unauthorised: 401
        try:
            self._get_json("/sessions/default/state")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
        # With the correct bearer: 200
        status, body = self._get_json(
            "/sessions/default/state",
            headers={"Authorization": "Bearer secret-xyz"},
        )
        self.assertEqual(status, 200)

    def test_health_does_not_require_auth_even_with_token(self):
        self.server.stop()
        os.environ[TOKEN_ENV] = "secret-xyz"
        self.server = GatewayServer(host="127.0.0.1", port=_free_port())
        self.assertTrue(self.server.start()["ok"])
        self.port = self.server.port
        status, body = self._get_json("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


# ============================================================
# Server lifecycle
# ============================================================

class GatewayServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        reset_singleton_for_tests()
        os.environ.pop(TOKEN_ENV, None)

    def tearDown(self):
        reset_singleton_for_tests()
        os.environ.pop(TOKEN_ENV, None)

    def test_start_returns_running_state_and_port(self):
        srv = GatewayServer(host="127.0.0.1", port=_free_port())
        try:
            r = srv.start()
            self.assertTrue(r["ok"])
            self.assertTrue(srv.is_running())
            self.assertEqual(r["host"], "127.0.0.1")
        finally:
            srv.stop()

    def test_double_start_returns_already_running(self):
        srv = GatewayServer(host="127.0.0.1", port=_free_port())
        try:
            srv.start()
            r = srv.start()
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "already_running")
        finally:
            srv.stop()

    def test_stop_when_not_running_returns_not_running(self):
        srv = GatewayServer()
        r = srv.stop()
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "not_running")


# ============================================================
# Dispatcher gateway commands
# ============================================================

class GatewayDispatcherCommandTests(unittest.TestCase):
    def setUp(self):
        reset_singleton_for_tests()
        os.environ.pop(TOKEN_ENV, None)
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        reset_singleton_for_tests()
        os.environ.pop(TOKEN_ENV, None)
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_status_when_not_running(self):
        ev = dispatch_input("gateway", session_id="default")
        self.assertEqual(ev["action"], "gateway")
        self.assertIn("not running", ev["output"].lower())

    def test_start_then_status_then_stop(self):
        port = _free_port()
        ev = dispatch_input(f"gateway start 127.0.0.1:{port}", session_id="default")
        self.assertIn("started on", ev["output"])
        try:
            ev2 = dispatch_input("gateway status", session_id="default")
            self.assertIn("running on", ev2["output"])
            self.assertIn(str(port), ev2["output"])
        finally:
            ev3 = dispatch_input("gateway stop", session_id="default")
            self.assertIn("stopped", ev3["output"].lower())

    def test_start_with_invalid_port_returns_usage(self):
        ev = dispatch_input("gateway start banana", session_id="default")
        self.assertIn("Usage", ev["output"])

    def test_unknown_subcommand_returns_usage(self):
        ev = dispatch_input("gateway dance", session_id="default")
        self.assertIn("Usage:", ev["output"])

    def test_gateway_is_reserved_for_custom_commands(self):
        from app.services.command_dispatcher import _RESERVED_CMD_NAMES
        self.assertIn("gateway", _RESERVED_CMD_NAMES)


if __name__ == "__main__":
    unittest.main()
