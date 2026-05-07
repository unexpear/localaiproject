import json
import unittest
from unittest.mock import MagicMock, patch

from app.models import runtime_client


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line


class _FakeJsonResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class PullModelStreamingTests(unittest.TestCase):
    def test_pull_without_callback_uses_non_streaming_path(self):
        with patch.object(runtime_client, "_request", return_value=_FakeJsonResponse({"status": "success"})) as req:
            success, output = runtime_client.pull_model("llama3.1:8b")

        self.assertTrue(success)
        self.assertEqual(output, "success")
        _, kwargs = req.call_args
        self.assertEqual(kwargs["json"]["stream"], False)

    def test_pull_with_callback_streams_events(self):
        lines = [
            json.dumps({"status": "pulling manifest"}),
            "",  # empty line is skipped
            json.dumps({"status": "downloading", "completed": 50, "total": 100}),
            json.dumps({"status": "success"}),
        ]
        events = []
        with patch.object(
            runtime_client,
            "_request",
            return_value=_FakeStreamResponse(lines),
        ) as req:
            success, output = runtime_client.pull_model("llama3.1:8b", progress_callback=events.append)

        self.assertTrue(success)
        self.assertEqual(output, "success")
        _, kwargs = req.call_args
        self.assertEqual(kwargs["json"]["stream"], True)
        self.assertTrue(kwargs["stream"])
        statuses = [e.get("status") for e in events]
        self.assertEqual(statuses, ["pulling manifest", "downloading", "success"])
        self.assertEqual(events[1]["completed"], 50)
        self.assertEqual(events[1]["model"], "llama3.1:8b")

    def test_pull_callback_exception_does_not_abort_stream(self):
        lines = [json.dumps({"status": "a"}), json.dumps({"status": "success"})]

        def flaky(event):
            if event["status"] == "a":
                raise RuntimeError("ui blew up")

        with patch.object(runtime_client, "_request", return_value=_FakeStreamResponse(lines)):
            success, output = runtime_client.pull_model("m", progress_callback=flaky)

        self.assertTrue(success)
        self.assertEqual(output, "success")

    def test_pull_error_event_returns_failure(self):
        lines = [json.dumps({"status": "pulling"}), json.dumps({"error": "network timeout"})]
        with patch.object(runtime_client, "_request", return_value=_FakeStreamResponse(lines)):
            success, output = runtime_client.pull_model("m", progress_callback=lambda e: None)

        self.assertFalse(success)
        self.assertEqual(output, "network timeout")

    def test_pull_malformed_line_becomes_status_event(self):
        lines = ["not-json-at-all", json.dumps({"status": "success"})]
        events = []
        with patch.object(runtime_client, "_request", return_value=_FakeStreamResponse(lines)):
            success, _ = runtime_client.pull_model("m", progress_callback=events.append)

        self.assertTrue(success)
        self.assertEqual(events[0]["status"], "not-json-at-all")


class PullModelCancellationTests(unittest.TestCase):
    def test_cancel_event_aborts_stream_with_cancelled_output(self):
        import threading as _t

        cancel = _t.Event()
        lines = [
            json.dumps({"status": "pulling manifest"}),
            json.dumps({"status": "downloading", "completed": 1, "total": 10}),
            json.dumps({"status": "downloading", "completed": 2, "total": 10}),
            json.dumps({"status": "success"}),
        ]

        events = []

        def callback(event):
            events.append(event)
            # Cancel after the first progress event.
            if event.get("status") == "pulling manifest":
                cancel.set()

        with patch.object(runtime_client, "_request", return_value=_FakeStreamResponse(lines)):
            success, output = runtime_client.pull_model(
                "m", progress_callback=callback, cancel_event=cancel
            )

        self.assertFalse(success)
        self.assertEqual(output, "cancelled")
        # We should not have seen "success" because we cancelled before reaching it.
        self.assertNotIn("success", [e.get("status") for e in events])


class DeleteModelTests(unittest.TestCase):
    def test_delete_success_returns_true(self):
        with patch.object(runtime_client, "_request", return_value=_FakeJsonResponse({}, status_code=200)) as req:
            success, output = runtime_client.delete_model("qwen3-vl")

        self.assertTrue(success)
        self.assertEqual(output, "deleted")
        args, kwargs = req.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertEqual(kwargs["json"]["name"], "qwen3-vl")

    def test_delete_missing_returns_not_found(self):
        with patch.object(runtime_client, "_request", return_value=_FakeJsonResponse({}, status_code=404)):
            success, output = runtime_client.delete_model("nonsense")

        self.assertFalse(success)
        self.assertIn("not found", output)

    def test_delete_network_error_surfaces_exception_message(self):
        with patch.object(runtime_client, "_request", side_effect=RuntimeError("boom")):
            success, output = runtime_client.delete_model("x")

        self.assertFalse(success)
        self.assertIn("boom", output)


if __name__ == "__main__":
    unittest.main()
