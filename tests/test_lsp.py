"""LSP integration tests.

The LspClient drives a real subprocess that speaks the protocol. These
tests substitute a fake server implemented in pure Python that follows
the same Content-Length JSON-RPC framing the real servers use, so we can
verify the full handshake -> diagnostics -> shutdown flow without
requiring pyright/pylsp/etc. to be installed.
"""
import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

from app.services.lsp import client as lsp_client
from app.services.lsp import registry as lsp_registry
from app.services.lsp import service as lsp_service


# ============================================================
# In-process fake server (used as a Python subprocess)
# ============================================================

# This script is launched by the LspClient under test. It:
#   1. Reads JSON-RPC messages from stdin
#   2. Replies to `initialize` with empty capabilities
#   3. On `textDocument/didOpen`, pushes one diagnostic for the file
#   4. Replies to `shutdown` and exits on `exit`
# Everything is framed with Content-Length headers, exactly like the spec.
_FAKE_SERVER_SCRIPT = textwrap.dedent(r"""
    import json, sys

    def read():
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            line = line.decode("ascii", errors="replace").rstrip("\r\n")
            if line == "":
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length") or 0)
        if not n:
            return None
        body = b""
        while len(body) < n:
            chunk = sys.stdin.buffer.read(n - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))

    def send(payload):
        body = json.dumps(payload).encode("utf-8")
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
        sys.stdout.buffer.flush()

    while True:
        msg = read()
        if msg is None:
            break
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})
        elif method == "textDocument/didOpen":
            uri = msg["params"]["textDocument"]["uri"]
            send({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [
                        {
                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
                            "severity": 1,
                            "source": "fake",
                            "code": "E001",
                            "message": "fabricated diagnostic for testing",
                        }
                    ],
                },
            })
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        elif method == "exit":
            break
""")


def _fake_server_command(tmp_dir):
    script_path = os.path.join(tmp_dir, "fake_lsp_server.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_SERVER_SCRIPT)
    return [sys.executable, script_path]


# ============================================================
# Framing tests (no subprocess)
# ============================================================

class FramingTests(unittest.TestCase):
    def test_frame_message_includes_content_length(self):
        framed = lsp_client._frame_message({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertIn(b"Content-Length: ", framed)
        self.assertIn(b"\r\n\r\n", framed)
        # Body should be exactly the declared length
        header, _, body = framed.partition(b"\r\n\r\n")
        declared = int(header.split(b": ")[1])
        self.assertEqual(len(body), declared)

    def test_read_message_round_trips(self):
        payload = {"jsonrpc": "2.0", "method": "x", "params": {"a": 1}}
        stream = io.BytesIO(lsp_client._frame_message(payload))
        result = lsp_client._read_message(stream)
        self.assertEqual(result, payload)

    def test_read_message_returns_none_on_eof(self):
        stream = io.BytesIO(b"")
        self.assertIsNone(lsp_client._read_message(stream))

    def test_path_to_uri_handles_unix_and_windows(self):
        if os.name == "nt":
            uri = lsp_client._path_to_uri(r"C:\Users\me\foo.py")
            self.assertTrue(uri.startswith("file:///C:/"))
            self.assertTrue(uri.endswith("foo.py"))
        else:
            uri = lsp_client._path_to_uri("/tmp/foo.py")
            self.assertEqual(uri, "file:///tmp/foo.py")


# ============================================================
# Client end-to-end with the fake server
# ============================================================

class LspClientEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        # File the fake server will report a diagnostic for.
        self.test_file = os.path.join(self.workspace, "demo.py")
        with open(self.test_file, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_initialize_and_get_diagnostics(self):
        cmd = _fake_server_command(self.workspace)
        client = lsp_client.LspClient(cmd, self.workspace)
        try:
            client.start()
            client.initialize()
            diagnostics = client.diagnostics_for(self.test_file, language_id="python", wait_seconds=4.0)
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0]["message"], "fabricated diagnostic for testing")
            self.assertEqual(diagnostics[0]["source"], "fake")
        finally:
            client.shutdown()

    def test_context_manager_starts_and_stops(self):
        cmd = _fake_server_command(self.workspace)
        with lsp_client.LspClient(cmd, self.workspace) as client:
            client.initialize()
            diags = client.diagnostics_for(self.test_file, language_id="python", wait_seconds=4.0)
            self.assertEqual(len(diags), 1)


# ============================================================
# Registry / detection tests
# ============================================================

class RegistryTests(unittest.TestCase):
    def test_language_for_path_python(self):
        self.assertEqual(lsp_registry.language_for_path("foo.py"), "python")
        self.assertEqual(lsp_registry.language_for_path("foo.PYI"), "python")

    def test_language_for_path_typescript_jsx(self):
        self.assertEqual(lsp_registry.language_for_path("comp.tsx"), "typescript")
        self.assertEqual(lsp_registry.language_for_path("comp.jsx"), "javascript")

    def test_language_for_path_unknown_returns_none(self):
        self.assertIsNone(lsp_registry.language_for_path("foo.exotic"))

    def test_language_for_empty_path_returns_none(self):
        self.assertIsNone(lsp_registry.language_for_path(""))
        self.assertIsNone(lsp_registry.language_for_path(None))

    def test_server_for_language_returns_none_when_nothing_installed(self):
        with patch("app.services.lsp.registry.shutil.which", return_value=None):
            self.assertIsNone(lsp_registry.server_for_language("python"))

    def test_server_for_language_picks_first_available(self):
        # Make only the second python candidate (pyright) "available"
        def fake_which(cmd):
            return "/fake/" + cmd if cmd == "pyright-langserver" else None

        with patch("app.services.lsp.registry.shutil.which", side_effect=fake_which):
            server = lsp_registry.server_for_language("python")
            self.assertIsNotNone(server)
            self.assertEqual(server.label, "pyright-langserver")

    def test_server_for_language_prefers_pylsp_when_available(self):
        def fake_which(cmd):
            return "/fake/" + cmd  # everything available

        with patch("app.services.lsp.registry.shutil.which", side_effect=fake_which):
            server = lsp_registry.server_for_language("python")
            self.assertEqual(server.label, "pylsp")  # community BSD is preferred

    def test_available_and_missing_servers_partition(self):
        def fake_which(cmd):
            return "/fake/" + cmd if cmd == "gopls" else None

        with patch("app.services.lsp.registry.shutil.which", side_effect=fake_which):
            avail = lsp_registry.available_servers()
            missing = lsp_registry.missing_servers()
            self.assertTrue(any(s.label == "gopls" for s in avail))
            self.assertFalse(any(s.label == "gopls" for s in missing))
            # And the union covers every known server
            self.assertEqual(
                {s.label for s in avail} | {s.label for s in missing},
                {s.label for s in lsp_registry.all_known_servers()},
            )

    def test_known_servers_are_all_free_no_strings(self):
        for server in lsp_registry.all_known_servers():
            self.assertNotIn("proprietary", server.license.lower())
            self.assertNotIn("commercial", server.license.lower())


# ============================================================
# Service-level tests with the fake server
# ============================================================

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.test_file = os.path.join(self.workspace, "demo.py")
        with open(self.test_file, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")

    def tearDown(self):
        lsp_service.shutdown_all()
        self._tmp.cleanup()

    def _patched_server_for_language(self, language):
        cmd = _fake_server_command(self.workspace)
        return lsp_registry.ServerInfo(
            language_id="python",
            label="fake-python",
            command=tuple(cmd),
            install_hint="(fake server, no install needed)",
            license="MIT (test)",
        )

    def test_get_diagnostics_returns_results_with_fake_server(self):
        with patch.object(lsp_service, "server_for_language", self._patched_server_for_language):
            result = lsp_service.get_diagnostics(self.test_file, workspace_root=self.workspace)
        self.assertTrue(result.ok)
        self.assertEqual(result.language, "python")
        self.assertEqual(len(result.diagnostics), 1)
        self.assertEqual(result.diagnostics[0]["source"], "fake")

    def test_get_diagnostics_unknown_language(self):
        odd = os.path.join(self.workspace, "data.exotic")
        with open(odd, "w", encoding="utf-8") as fh:
            fh.write("x")
        result = lsp_service.get_diagnostics(odd, workspace_root=self.workspace)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unknown_language")

    def test_get_diagnostics_no_server_installed(self):
        with patch.object(lsp_service, "server_for_language", return_value=None):
            result = lsp_service.get_diagnostics(self.test_file, workspace_root=self.workspace)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_server_installed")

    def test_get_diagnostics_path_not_found(self):
        result = lsp_service.get_diagnostics(os.path.join(self.workspace, "missing.py"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_not_found")

    def test_format_diagnostics_renders_each_finding(self):
        with patch.object(lsp_service, "server_for_language", self._patched_server_for_language):
            result = lsp_service.get_diagnostics(self.test_file, workspace_root=self.workspace)
        rendered = lsp_service.format_diagnostics(result)
        self.assertIn("fabricated diagnostic", rendered)
        self.assertIn("error", rendered)


# ============================================================
# Dispatcher integration
# ============================================================

class DispatcherLspTests(unittest.TestCase):
    def test_dispatch_lsp_status_when_nothing_installed(self):
        from app.services.command_dispatcher import dispatch_input

        with patch("app.services.command_dispatcher.lsp_available_servers", return_value=[]):
            event = dispatch_input("lsp", session_id="default")
        self.assertEqual(event["action"], "lsp")
        self.assertIn("no language servers installed", event["output"].lower())

    def test_dispatch_lsp_status_lists_installed(self):
        from app.services.command_dispatcher import dispatch_input

        fake = lsp_registry.ServerInfo(
            language_id="python", label="pylsp", command=("pylsp",),
            install_hint="pip install python-lsp-server", license="BSD-3 (community)",
        )
        with patch("app.services.command_dispatcher.lsp_available_servers", return_value=[fake]):
            event = dispatch_input("lsp", session_id="default")
        self.assertIn("pylsp", event["output"])
        self.assertIn("BSD-3", event["output"])

    def test_dispatch_lsp_diagnose_without_path_returns_usage(self):
        from app.services.command_dispatcher import dispatch_input

        event = dispatch_input("lsp diagnose", session_id="default")
        self.assertIn("Usage:", event["output"])

    def test_dispatch_lsp_diagnose_uses_workspace_relative_path(self):
        from app.services.command_dispatcher import dispatch_input

        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "main.py")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("x=1\n")

            captured = {}

            def fake_get_diag(path, workspace_root=None):
                captured["path"] = path
                captured["workspace_root"] = workspace_root
                return lsp_service.DiagnosticsResult(
                    ok=True, file_path=path, language="python",
                    server=None, diagnostics=[],
                )

            with patch(
                "app.services.command_dispatcher.local_memory.get_session_context",
                return_value={"workspace_path": tmp, "active_skills": [], "pending_approval": None},
            ), patch(
                "app.services.command_dispatcher.lsp_get_diagnostics",
                side_effect=fake_get_diag,
            ):
                event = dispatch_input("lsp diagnose main.py", session_id="default")

        self.assertEqual(event["action"], "lsp")
        self.assertEqual(captured["path"], os.path.join(tmp, "main.py"))
        self.assertEqual(captured["workspace_root"], tmp)


if __name__ == "__main__":
    unittest.main()
