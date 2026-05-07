"""Tests for the streaming chat primitive (Phase 1 — see
`docs/design/streaming.md`).

Covers:
* ``runtime_client.infer_chat_stream``:
  - Happy path yields chunk events + a terminal ``done`` event
  - HTTP error → terminal ``error`` event with partial content
  - Malformed JSON lines silently skipped (transport hiccups)
  - Missing message+messages → error event before any HTTP call
  - The terminal event is always exactly one (done OR error)

* ``chat_providers.dispatch_chat_stream``:
  - Unknown provider → error event
  - Ollama path streams chunks via runtime_client
  - Cloud providers (openai/anthropic) fall back to batch +
    synthesize a single ``done`` event
  - Cloud failure → ``error`` event with content preserved
"""
import json
import unittest
from unittest.mock import MagicMock, patch as mock_patch

from app.models import runtime_client
from app.services import chat_providers as cp


def _mock_resp(lines):
    """Build a fake `requests.Response` whose iter_lines yields the
    supplied list of lines. raise_for_status is a no-op."""
    resp = MagicMock()
    resp.iter_lines.return_value = iter(lines)
    resp.raise_for_status = MagicMock()
    return resp


# ============================================================
# infer_chat_stream
# ============================================================

class InferChatStreamTests(unittest.TestCase):
    def test_happy_path_yields_chunks_then_done(self):
        lines = [
            json.dumps({"message": {"content": "Hello "}}),
            json.dumps({"message": {"content": "world"}}),
            json.dumps({"message": {"content": ""}, "done": True}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "hi"}],
            ))
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["chunk", "chunk", "done"])
        self.assertEqual(events[0]["content"], "Hello ")
        self.assertEqual(events[1]["content"], "world")
        # done event accumulates the full text.
        self.assertEqual(events[-1]["content"], "Hello world")

    def test_http_error_yields_terminal_error_event(self):
        with mock_patch.object(
            runtime_client, "_request",
            side_effect=RuntimeError("connection refused"),
        ):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "hi"}],
            ))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "error")
        self.assertIn("connection refused", events[0]["error"])

    def test_partial_content_preserved_on_mid_stream_error(self):
        # Iter yields one chunk, then a closure that raises.
        good_line = json.dumps({"message": {"content": "partial "}})

        class FlakyIter:
            def __init__(self):
                self.calls = 0

            def __iter__(self):
                return self

            def __next__(self):
                self.calls += 1
                if self.calls == 1:
                    return good_line
                raise RuntimeError("connection reset mid-stream")

        resp = MagicMock()
        resp.iter_lines.return_value = FlakyIter()
        resp.raise_for_status = MagicMock()

        with mock_patch.object(runtime_client, "_request", return_value=resp):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "hi"}],
            ))
        # First event is the chunk, second (terminal) is error with
        # the partial content carried.
        self.assertEqual(events[0]["kind"], "chunk")
        self.assertEqual(events[0]["content"], "partial ")
        self.assertEqual(events[-1]["kind"], "error")
        self.assertEqual(events[-1]["content"], "partial ")

    def test_malformed_json_lines_silently_skipped(self):
        lines = [
            "not even close to json",
            json.dumps({"message": {"content": "hi"}}),
            "{partial json",
            json.dumps({"done": True, "message": {"content": ""}}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "x"}],
            ))
        kinds = [e["kind"] for e in events]
        # One real chunk + done; malformed lines didn't crash or emit.
        self.assertEqual(kinds, ["chunk", "done"])
        self.assertEqual(events[0]["content"], "hi")

    def test_blank_lines_ignored(self):
        lines = [
            "",
            json.dumps({"message": {"content": "hi"}}),
            "",
            json.dumps({"done": True, "message": {"content": ""}}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "x"}],
            ))
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["chunk", "done"])

    def test_missing_message_arg_yields_error(self):
        events = list(runtime_client.infer_chat_stream("llama3"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "error")
        self.assertIn("requires", events[0]["error"])

    def test_terminal_event_exactly_once(self):
        lines = [
            json.dumps({"message": {"content": "a"}}),
            json.dumps({"message": {"content": "b"}}),
            json.dumps({"done": True, "message": {"content": ""}}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "x"}],
            ))
        terminals = [e for e in events if e["kind"] in ("done", "error")]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["kind"], "done")

    def test_alternative_response_field_supported(self):
        # Older Ollama variants emit "response" instead of
        # "message"."content"; tolerate both.
        lines = [
            json.dumps({"response": "alpha"}),
            json.dumps({"response": "beta"}),
            json.dumps({"done": True}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "x"}],
            ))
        chunks = [e["content"] for e in events if e["kind"] == "chunk"]
        self.assertEqual(chunks, ["alpha", "beta"])

    def test_empty_chunks_are_dropped(self):
        # Empty content chunks (Ollama sometimes emits them) don't
        # produce empty `chunk` events.
        lines = [
            json.dumps({"message": {"content": ""}}),
            json.dumps({"message": {"content": "real"}}),
            json.dumps({"done": True, "message": {"content": ""}}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(runtime_client.infer_chat_stream(
                "llama3", messages=[{"role": "user", "content": "x"}],
            ))
        chunks = [e for e in events if e["kind"] == "chunk"]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["content"], "real")


# ============================================================
# dispatch_chat_stream
# ============================================================

class DispatchChatStreamTests(unittest.TestCase):
    def test_unknown_provider_yields_error(self):
        events = list(cp.dispatch_chat_stream("nonexistent", "model", messages=[]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "error")
        self.assertEqual(events[0]["error"], "provider_unknown")

    def test_ollama_path_routes_to_runtime_client_stream(self):
        lines = [
            json.dumps({"message": {"content": "hello"}}),
            json.dumps({"done": True, "message": {"content": ""}}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(cp.dispatch_chat_stream(
                "ollama", "llama3", messages=[{"role": "user", "content": "x"}],
            ))
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["chunk", "done"])
        self.assertEqual(events[0]["content"], "hello")
        self.assertEqual(events[-1]["content"], "hello")

    def test_cloud_provider_falls_back_to_batch_done(self):
        # A registered non-ollama provider should produce a single
        # `done` event by calling the batch chat_fn.
        original = cp.REGISTRY.get("openai")
        fake_chat_fn = MagicMock(return_value={
            "ok": True, "content": "hi from cloud",
            "error": None, "raw": {"id": "abc"},
        })
        cp.REGISTRY["openai"] = cp.ProviderInfo(
            id="openai", label="Test", auth_env="X",
            chat_fn=fake_chat_fn, is_local=False, description="t",
        )
        try:
            events = list(cp.dispatch_chat_stream(
                "openai", "gpt-4", messages=[],
            ))
        finally:
            if original is not None:
                cp.REGISTRY["openai"] = original
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "done")
        self.assertEqual(events[0]["content"], "hi from cloud")
        self.assertEqual(events[0]["raw"], {"id": "abc"})
        self.assertEqual(fake_chat_fn.call_count, 1)

    def test_cloud_provider_failure_yields_error_event(self):
        original = cp.REGISTRY.get("anthropic")
        fake_chat_fn = MagicMock(return_value={
            "ok": False, "content": "",
            "error": "provider_unauthenticated", "raw": None,
        })
        cp.REGISTRY["anthropic"] = cp.ProviderInfo(
            id="anthropic", label="Test", auth_env="X",
            chat_fn=fake_chat_fn, is_local=False, description="t",
        )
        try:
            events = list(cp.dispatch_chat_stream(
                "anthropic", "claude", messages=[],
            ))
        finally:
            if original is not None:
                cp.REGISTRY["anthropic"] = original
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "error")
        self.assertEqual(events[0]["error"], "provider_unauthenticated")

    def test_terminal_event_always_exactly_one(self):
        # Across both happy and error paths, exactly one terminal.
        lines = [
            json.dumps({"message": {"content": "x"}}),
            json.dumps({"done": True, "message": {"content": ""}}),
        ]
        with mock_patch.object(runtime_client, "_request", return_value=_mock_resp(lines)):
            events = list(cp.dispatch_chat_stream(
                "ollama", "llama3", messages=[],
            ))
        terminals = [e for e in events if e["kind"] in ("done", "error")]
        self.assertEqual(len(terminals), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
