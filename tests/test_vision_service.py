"""Tests for the vision-input @image: attachment plumbing
(Phase 1 — see `docs/design/vision-input.md`).

Covers:
* ``extract_image_mentions`` parser:
  - Single + multiple mentions
  - Trailing punctuation stripped
  - Lookbehind avoids false matches in emails / URLs
  - Empty input, no mentions, dedup

* ``attach_image_mentions`` end-to-end:
  - Successful attachment (small PNG round-trips through base64)
  - No workspace pinned → all attachments flagged with reason
  - Path traversal (``../escape.png``) → invalid_path
  - Missing file → path_not_found
  - Wrong extension → unsupported_format
  - Symlink → symlink_unsupported
  - Oversize → size_exceeded with metadata
  - Count cap enforced (matches beyond cap silently dropped)

* ``apply_to_messages``:
  - Adds ``images`` field to last user message
  - No-op when no successful attachments
  - No-op when last message isn't user role
  - Skips empty message list

* ``format_attachment_summary`` rendering.

* End-to-end via ``chat_service.handle_chat`` with mocked
  dispatch_chat — confirms the runtime_messages payload's
  trailing user message gets the ``images`` field populated.
"""
import os
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch as mock_patch

from app.services import vision_service as vs


# Tiny valid PNG (1x1 transparent). 67 bytes; small enough for
# tests to base64 round-trip cheaply.
_TINY_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41,
    0x54, 0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
    0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
    0x42, 0x60, 0x82,
])


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _write_tiny_png(workspace, name="tiny.png"):
    full = os.path.join(workspace, name)
    os.makedirs(os.path.dirname(full) or workspace, exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(_TINY_PNG)
    return full


# ============================================================
# Parser
# ============================================================

class ExtractImageMentionsTests(unittest.TestCase):
    def test_single_mention(self):
        self.assertEqual(
            vs.extract_image_mentions("@image:foo.png describe this"),
            ["foo.png"],
        )

    def test_multiple_mentions_in_order(self):
        self.assertEqual(
            vs.extract_image_mentions("compare @image:a.png and @image:b.jpg"),
            ["a.png", "b.jpg"],
        )

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(
            vs.extract_image_mentions("see @image:foo.png."),
            ["foo.png"],
        )
        self.assertEqual(
            vs.extract_image_mentions("see @image:foo.png, please"),
            ["foo.png"],
        )

    def test_dedupes_repeats(self):
        self.assertEqual(
            vs.extract_image_mentions("@image:a.png and @image:a.png again"),
            ["a.png"],
        )

    def test_lookbehind_skips_word_preceded(self):
        # ``user@image:foo`` shouldn't match — ``@`` preceded by a word char.
        self.assertEqual(
            vs.extract_image_mentions("hello user@image:foo.png world"),
            [],
        )

    def test_no_mentions(self):
        self.assertEqual(vs.extract_image_mentions("no images here"), [])

    def test_empty_input(self):
        self.assertEqual(vs.extract_image_mentions(""), [])
        self.assertEqual(vs.extract_image_mentions(None), [])


# ============================================================
# attach_image_mentions
# ============================================================

class AttachImageMentionsTests(unittest.TestCase):
    def test_successful_attachment(self):
        with tempfile.TemporaryDirectory() as ws:
            _write_tiny_png(ws)
            result = vs.attach_image_mentions("@image:tiny.png", ws)
        self.assertTrue(result["expanded"])
        self.assertTrue(result["had_workspace"])
        self.assertEqual(len(result["attachments"]), 1)
        att = result["attachments"][0]
        self.assertTrue(att["ok"])
        self.assertEqual(att["mime_type"], "image/png")
        self.assertEqual(att["size_bytes"], len(_TINY_PNG))
        # Base64 carries the encoded payload.
        self.assertTrue(att["base64_data"])

    def test_stripped_text_replaces_token_with_placeholder(self):
        with tempfile.TemporaryDirectory() as ws:
            _write_tiny_png(ws)
            result = vs.attach_image_mentions(
                "see @image:tiny.png and tell me", ws,
            )
        self.assertEqual(
            result["stripped"],
            "see [image:tiny.png] and tell me",
        )

    def test_no_workspace_flags_all_attachments(self):
        result = vs.attach_image_mentions("@image:foo.png", None)
        self.assertFalse(result["expanded"])
        self.assertFalse(result["had_workspace"])
        self.assertEqual(len(result["attachments"]), 1)
        self.assertEqual(result["attachments"][0]["error"], "no_workspace")

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            result = vs.attach_image_mentions("@image:../escape.png", ws)
        self.assertEqual(result["attachments"][0]["error"], "invalid_path")

    def test_missing_file_returns_path_not_found(self):
        with tempfile.TemporaryDirectory() as ws:
            result = vs.attach_image_mentions("@image:nope.png", ws)
        self.assertEqual(result["attachments"][0]["error"], "path_not_found")

    def test_wrong_extension_returns_unsupported_format(self):
        with tempfile.TemporaryDirectory() as ws:
            txt = os.path.join(ws, "doc.txt")
            with open(txt, "w") as fh:
                fh.write("not an image")
            result = vs.attach_image_mentions("@image:doc.txt", ws)
        self.assertEqual(result["attachments"][0]["error"], "unsupported_format")

    def test_oversize_image_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            _write_tiny_png(ws)
            # Set a tiny per-image cap to force the failure path.
            result = vs.attach_image_mentions(
                "@image:tiny.png", ws, per_image_max_bytes=10,
            )
        self.assertEqual(result["attachments"][0]["error"], "size_exceeded")
        # Metadata still includes mime/size for debugging.
        self.assertEqual(result["attachments"][0]["mime_type"], "image/png")
        self.assertEqual(result["attachments"][0]["size_bytes"], len(_TINY_PNG))

    def test_count_cap_silently_drops_excess(self):
        with tempfile.TemporaryDirectory() as ws:
            for n in range(6):
                _write_tiny_png(ws, f"img{n}.png")
            text = " ".join(f"@image:img{n}.png" for n in range(6))
            result = vs.attach_image_mentions(text, ws, max_images=2)
        # Only first 2 are even attempted.
        self.assertEqual(len(result["attachments"]), 2)

    @unittest.skipIf(sys.platform == "win32", "symlink test requires admin on Windows")
    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as ws:
            real = _write_tiny_png(ws, "real.png")
            link = os.path.join(ws, "linked.png")
            os.symlink(real, link)
            result = vs.attach_image_mentions("@image:linked.png", ws)
        self.assertEqual(result["attachments"][0]["error"], "symlink_unsupported")


# ============================================================
# apply_to_messages
# ============================================================

class ApplyToMessagesTests(unittest.TestCase):
    def _b64_attachment(self):
        with tempfile.TemporaryDirectory() as ws:
            _write_tiny_png(ws)
            result = vs.attach_image_mentions("@image:tiny.png", ws)
        return result["attachments"]

    def test_adds_images_to_last_user_message(self):
        attachments = self._b64_attachment()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "describe this"},
        ]
        vs.apply_to_messages(messages, attachments)
        self.assertIn("images", messages[-1])
        self.assertEqual(len(messages[-1]["images"]), 1)

    def test_no_op_when_no_successful_attachments(self):
        # All failed attachments
        failed = [
            {"path": "x.png", "ok": False, "error": "path_not_found"},
        ]
        messages = [{"role": "user", "content": "x"}]
        vs.apply_to_messages(messages, failed)
        self.assertNotIn("images", messages[0])

    def test_no_op_for_empty_messages(self):
        attachments = self._b64_attachment()
        messages = []
        vs.apply_to_messages(messages, attachments)  # no crash
        self.assertEqual(messages, [])

    def test_no_op_when_last_is_not_user_role(self):
        attachments = self._b64_attachment()
        messages = [{"role": "assistant", "content": "x"}]
        vs.apply_to_messages(messages, attachments)
        self.assertNotIn("images", messages[0])

    def test_returns_messages_for_chaining(self):
        attachments = self._b64_attachment()
        messages = [{"role": "user", "content": "x"}]
        returned = vs.apply_to_messages(messages, attachments)
        self.assertIs(returned, messages)


# ============================================================
# Summary formatter
# ============================================================

class FormatAttachmentSummaryTests(unittest.TestCase):
    def test_successful_attachments_show_mime_and_size(self):
        attachments = [
            {"path": "a.png", "ok": True, "mime_type": "image/png",
             "size_bytes": 12345},
        ]
        s = vs.format_attachment_summary(attachments)
        self.assertIn("a.png", s)
        self.assertIn("image/png", s)
        self.assertIn("KB", s)

    def test_failed_attachment_shows_reason(self):
        attachments = [
            {"path": "a.png", "ok": False, "error": "size_exceeded"},
        ]
        s = vs.format_attachment_summary(attachments)
        self.assertIn("FAIL", s)
        self.assertIn("size_exceeded", s)

    def test_empty_list_returns_empty_string(self):
        self.assertEqual(vs.format_attachment_summary([]), "")
        self.assertEqual(vs.format_attachment_summary(None), "")


# ============================================================
# End-to-end via handle_chat (mocked dispatch)
# ============================================================

class HandleChatVisionAttachmentTests(unittest.TestCase):
    def test_image_attachment_reaches_runtime_messages(self):
        from app.memory import local_memory
        from app.services import chat_service

        sid = _unique("vision_e2e")

        with tempfile.TemporaryDirectory() as ws:
            _write_tiny_png(ws)
            local_memory.set_workspace(session_id=sid, workspace_path=ws)

            captured = {}

            def fake_dispatch_chat(provider_id, model, messages, options=None):
                captured["messages"] = messages
                return {"ok": True, "content": "looks fine",
                        "error": None, "raw": {}}

            with mock_patch.object(
                chat_service.chat_providers, "dispatch_chat",
                side_effect=fake_dispatch_chat,
            ), mock_patch.object(
                chat_service.model_registry, "resolve_model_for_role",
                return_value={"resolved_role": "vision",
                              "model": "llama3.2-vision",
                              "fallback_used": False},
            ):
                result = chat_service.handle_chat(
                    "@image:tiny.png describe this", session_id=sid,
                )

        self.assertTrue(result["ok"])
        # Captured messages: trailing user message has `images` field.
        runtime_messages = captured["messages"]
        self.assertEqual(runtime_messages[-1]["role"], "user")
        self.assertIn("images", runtime_messages[-1])
        self.assertEqual(len(runtime_messages[-1]["images"]), 1)
        # Stripped content (no @image: token, replaced by placeholder).
        self.assertNotIn("@image:", runtime_messages[-1]["content"])
        self.assertIn("[image:tiny.png]", runtime_messages[-1]["content"])

    def test_no_image_attachment_when_no_mentions(self):
        from app.memory import local_memory
        from app.services import chat_service

        sid = _unique("vision_e2e_none")
        with tempfile.TemporaryDirectory() as ws:
            local_memory.set_workspace(session_id=sid, workspace_path=ws)

            captured = {}

            def fake_dispatch_chat(provider_id, model, messages, options=None):
                captured["messages"] = messages
                return {"ok": True, "content": "ok", "error": None, "raw": {}}

            with mock_patch.object(
                chat_service.chat_providers, "dispatch_chat",
                side_effect=fake_dispatch_chat,
            ), mock_patch.object(
                chat_service.model_registry, "resolve_model_for_role",
                return_value={"resolved_role": "reasoning",
                              "model": "llama3.1:8b",
                              "fallback_used": False},
            ):
                chat_service.handle_chat("hello world", session_id=sid)

        runtime_messages = captured["messages"]
        self.assertNotIn("images", runtime_messages[-1])


# ============================================================
# Phase 3: model-capability gate
# ============================================================

class IsVisionCapableTests(unittest.TestCase):
    def test_explicit_registry_flag(self):
        from app.models import model_registry
        # qwen3-vl is registered with vision_capable=True.
        self.assertTrue(model_registry.is_vision_capable("qwen3-vl"))

    def test_non_vision_registered_models_return_false(self):
        from app.models import model_registry
        for m in ("llama3.1:8b", "qwen2.5-coder:7b", "dolphin3:8b"):
            self.assertFalse(
                model_registry.is_vision_capable(m),
                f"{m} should not be vision-capable",
            )

    def test_heuristic_catches_vision_keyword(self):
        from app.models import model_registry
        for m in (
            "llama3.2-vision",
            "llama3.2-vision:11b",
            "llama3.3-vision-instruct",
        ):
            self.assertTrue(
                model_registry.is_vision_capable(m),
                f"{m} should match `vision` heuristic",
            )

    def test_heuristic_catches_vl_suffix(self):
        from app.models import model_registry
        for m in ("qwen3-vl", "qwen2.5-vl:7b", "qwen2-vl:72b"):
            self.assertTrue(
                model_registry.is_vision_capable(m),
                f"{m} should match `-vl` heuristic",
            )

    def test_heuristic_catches_llava_family(self):
        from app.models import model_registry
        for m in ("llava", "llava:34b", "llava-llama3:8b"):
            self.assertTrue(
                model_registry.is_vision_capable(m),
                f"{m} should match `llava` heuristic",
            )

    def test_heuristic_catches_other_vision_families(self):
        from app.models import model_registry
        for m in ("moondream", "moondream2", "bakllava", "minicpm-v:8b", "pixtral:12b"):
            self.assertTrue(
                model_registry.is_vision_capable(m),
                f"{m} should match family heuristic",
            )

    def test_empty_or_none_returns_false(self):
        from app.models import model_registry
        self.assertFalse(model_registry.is_vision_capable(""))
        self.assertFalse(model_registry.is_vision_capable(None))
        self.assertFalse(model_registry.is_vision_capable(123))


class CapabilityGateInHandleChatTests(unittest.TestCase):
    """Phase 3 wires the capability gate into ``handle_chat`` so
    images don't get sent to non-vision models."""

    def _stage(self, sid, ws):
        from app.memory import local_memory
        local_memory.set_workspace(session_id=sid, workspace_path=ws)
        full = os.path.join(ws, "tiny.png")
        with open(full, "wb") as fh:
            fh.write(_TINY_PNG)

    def test_vision_capable_model_receives_images(self):
        from app.memory import local_memory
        from app.services import chat_service

        sid = _unique("vision_phase3_capable")
        with tempfile.TemporaryDirectory() as ws:
            self._stage(sid, ws)
            captured = {}

            def fake_dispatch(provider_id, model, messages, options=None):
                captured["messages"] = messages
                return {"ok": True, "content": "ok", "error": None, "raw": {}}

            with mock_patch.object(
                chat_service.chat_providers, "dispatch_chat",
                side_effect=fake_dispatch,
            ), mock_patch.object(
                chat_service.model_registry, "resolve_model_for_role",
                return_value={
                    "resolved_role": "vision",
                    "model": "llama3.2-vision",
                    "fallback_used": False,
                },
            ):
                chat_service.handle_chat(
                    "@image:tiny.png describe", session_id=sid,
                )

            # Vision-capable model → images attached.
            self.assertIn("images", captured["messages"][-1])
            self.assertEqual(len(captured["messages"][-1]["images"]), 1)

    def test_non_vision_model_strips_images(self):
        from app.memory import local_memory
        from app.services import chat_service

        sid = _unique("vision_phase3_strips")
        with tempfile.TemporaryDirectory() as ws:
            self._stage(sid, ws)
            captured = {}

            def fake_dispatch(provider_id, model, messages, options=None):
                captured["messages"] = messages
                return {"ok": True, "content": "ok", "error": None, "raw": {}}

            with mock_patch.object(
                chat_service.chat_providers, "dispatch_chat",
                side_effect=fake_dispatch,
            ), mock_patch.object(
                chat_service.model_registry, "resolve_model_for_role",
                return_value={
                    "resolved_role": "reasoning",
                    "model": "llama3.1:8b",
                    "fallback_used": True,
                },
            ):
                chat_service.handle_chat(
                    "@image:tiny.png describe", session_id=sid,
                )

            # Non-vision model → images NOT attached.
            self.assertNotIn("images", captured["messages"][-1])

    def test_non_vision_model_records_failure_in_metadata(self):
        from app.memory import local_memory
        from app.services import chat_service

        sid = _unique("vision_phase3_metadata")
        with tempfile.TemporaryDirectory() as ws:
            self._stage(sid, ws)

            def fake_dispatch(provider_id, model, messages, options=None):
                return {"ok": True, "content": "ok", "error": None, "raw": {}}

            with mock_patch.object(
                chat_service.chat_providers, "dispatch_chat",
                side_effect=fake_dispatch,
            ), mock_patch.object(
                chat_service.model_registry, "resolve_model_for_role",
                return_value={
                    "resolved_role": "reasoning",
                    "model": "llama3.1:8b",
                    "fallback_used": True,
                },
            ):
                chat_service.handle_chat(
                    "@image:tiny.png describe", session_id=sid,
                )

            history = local_memory.get_history(session_id=sid, limit=5)
            user_turns = [h for h in history if h.get("role") == "user"]
            self.assertTrue(user_turns)
            meta = user_turns[-1].get("metadata") or {}
            attachments = meta.get("image_attachments") or []
            self.assertEqual(len(attachments), 1)
            self.assertFalse(attachments[0]["ok"])
            self.assertEqual(attachments[0]["error"], "non_vision_model")


# ============================================================
# Phase 3b: vision lane bias from @image: tokens
# ============================================================

class VisionLaneBiasTests(unittest.TestCase):
    def test_no_images_returns_zero(self):
        self.assertEqual(vs.vision_lane_bias("describe this layout"), 0)

    def test_empty_or_none_returns_zero(self):
        self.assertEqual(vs.vision_lane_bias(""), 0)
        self.assertEqual(vs.vision_lane_bias(None), 0)

    def test_one_image_returns_per_image_bias(self):
        self.assertEqual(
            vs.vision_lane_bias("@image:foo.png describe this"),
            vs.PER_IMAGE_VISION_BIAS,
        )

    def test_two_images_returns_double_bias(self):
        self.assertEqual(
            vs.vision_lane_bias("@image:a.png @image:b.jpg"),
            min(2 * vs.PER_IMAGE_VISION_BIAS, vs.MAX_VISION_BIAS),
        )

    def test_three_or_more_images_capped_at_max(self):
        # 3 × 5 = 15, but cap is 10.
        self.assertEqual(
            vs.vision_lane_bias("@image:a.png @image:b.png @image:c.png"),
            vs.MAX_VISION_BIAS,
        )
        # Even at 10 images, still capped.
        text = " ".join(f"@image:f{i}.png" for i in range(10))
        self.assertEqual(vs.vision_lane_bias(text), vs.MAX_VISION_BIAS)

    def test_lookbehind_excludes_email_like_tokens(self):
        # The parser's lookbehind on `@` ignores word-preceded
        # contexts; bias should NOT trigger.
        self.assertEqual(
            vs.vision_lane_bias("contact me at user@image:foo.png"),
            0,
        )

    def test_dedupes_repeats(self):
        # extract_image_mentions dedupes, so bias counts unique
        # paths.
        self.assertEqual(
            vs.vision_lane_bias("@image:a.png and @image:a.png again"),
            vs.PER_IMAGE_VISION_BIAS,
        )


class VisionBiasInHandleChatTests(unittest.TestCase):
    """End-to-end: ``handle_chat`` adds the vision bias to
    ``lane_biases`` before the router picks. Verified by mocking
    ``route_message`` and inspecting the captured biases."""

    def test_no_image_no_vision_bias(self):
        from app.memory import local_memory
        from app.services import chat_service

        sid = _unique("vision_bias_no_image")
        local_memory.set_workspace(session_id=sid, workspace_path=None)

        captured = {}

        def fake_route_message(message, recent_history=None, lane_biases=None):
            captured["biases"] = dict(lane_biases or {})
            return {
                "lane": "reasoning", "confidence": "low",
                "scores": {"coding": 0, "vision": 0, "reasoning": 1},
                "primary_signal": None,
                "context_used": False, "context_reason": None,
            }

        with mock_patch.object(
            chat_service.router, "route_message", side_effect=fake_route_message,
        ), mock_patch.object(
            chat_service.model_registry, "resolve_model_for_role",
            return_value={"resolved_role": "reasoning",
                          "model": None, "fallback_used": False},
        ):
            chat_service.handle_chat("hello world", session_id=sid)

        # No image tokens → no vision bias contribution.
        self.assertEqual(captured["biases"].get("vision", 0), 0)

    def test_one_image_token_adds_vision_bias(self):
        from app.memory import local_memory
        from app.services import chat_service, vision_service

        sid = _unique("vision_bias_one_image")
        local_memory.set_workspace(session_id=sid, workspace_path=None)

        captured = {}

        def fake_route_message(message, recent_history=None, lane_biases=None):
            captured["biases"] = dict(lane_biases or {})
            return {
                "lane": "reasoning", "confidence": "low",
                "scores": {"coding": 0, "vision": 0, "reasoning": 1},
                "primary_signal": None,
                "context_used": False, "context_reason": None,
            }

        with mock_patch.object(
            chat_service.router, "route_message", side_effect=fake_route_message,
        ), mock_patch.object(
            chat_service.model_registry, "resolve_model_for_role",
            return_value={"resolved_role": "reasoning",
                          "model": None, "fallback_used": False},
        ):
            chat_service.handle_chat(
                "@image:foo.png describe this", session_id=sid,
            )

        self.assertEqual(
            captured["biases"].get("vision"),
            vision_service.PER_IMAGE_VISION_BIAS,
        )

    def test_multi_image_capped(self):
        from app.memory import local_memory
        from app.services import chat_service, vision_service

        sid = _unique("vision_bias_multi")
        local_memory.set_workspace(session_id=sid, workspace_path=None)

        captured = {}

        def fake_route_message(message, recent_history=None, lane_biases=None):
            captured["biases"] = dict(lane_biases or {})
            return {
                "lane": "vision", "confidence": "high",
                "scores": {"coding": 0, "vision": 10, "reasoning": 1},
                "primary_signal": None,
                "context_used": False, "context_reason": None,
            }

        with mock_patch.object(
            chat_service.router, "route_message", side_effect=fake_route_message,
        ), mock_patch.object(
            chat_service.model_registry, "resolve_model_for_role",
            return_value={"resolved_role": "vision",
                          "model": None, "fallback_used": False},
        ):
            chat_service.handle_chat(
                "compare @image:a.png @image:b.jpg @image:c.gif please",
                session_id=sid,
            )

        # 3 images × 5 = 15, capped at 10.
        self.assertEqual(
            captured["biases"].get("vision"),
            vision_service.MAX_VISION_BIAS,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
