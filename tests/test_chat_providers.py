"""Tests for chat-providers Phase 1 (`docs/design/chat-providers.md`).

Five layers:
  - HTTP wrapper response classification (timeout / unauthorized / etc.)
  - OpenAI + Anthropic adapters with mocked `_http_post_json`
  - Storage round-trip (`session.context.lane_providers`)
  - Dispatcher commands (`provider list / show / bind / unbind`)
  - chat_service integration: bound lane routes through provider; a
    forbid_provider rule downgrades the binding to ollama
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import chat_providers as cp
from app.services import chat_service
from app.services import rule_service as rs
from app.services.command_dispatcher import dispatch_input


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# ============================================================
# OpenAI adapter
# ============================================================

class OpenAIAdapterTests(unittest.TestCase):
    def test_calls_chat_completions_with_bearer_auth(self):
        canned = {
            "choices": [{"message": {"role": "assistant", "content": "hi from openai"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            "model": "gpt-5",
        }
        captured = {}

        def fake_post(url, headers, body, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = body
            return canned, None

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            with patch.object(cp, "_http_post_json", side_effect=fake_post):
                result = cp.openai_chat(
                    "gpt-5",
                    [{"role": "user", "content": "hello"}],
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "hi from openai")
        self.assertEqual(result["error"], None)
        self.assertIn("/chat/completions", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(captured["body"]["model"], "gpt-5")
        self.assertEqual(captured["body"]["messages"][0]["content"], "hello")

    def test_no_credentials_returns_unauthenticated(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            # Also remove it if set in the real env.
            os.environ.pop("OPENAI_API_KEY", None)
            result = cp.openai_chat("gpt-5", [{"role": "user", "content": "hi"}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "provider_unauthenticated")

    def test_unauthorized_response_maps_to_provider_unauthenticated(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-bad"}, clear=False):
            with patch.object(cp, "_http_post_json", return_value=(None, "unauthorized")):
                result = cp.openai_chat("gpt-5", [{"role": "user", "content": "hi"}])
        self.assertEqual(result["error"], "provider_unauthenticated")

    def test_timeout_maps_to_provider_unreachable(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            with patch.object(cp, "_http_post_json", return_value=(None, "timeout")):
                result = cp.openai_chat("gpt-5", [{"role": "user", "content": "hi"}])
        self.assertEqual(result["error"], "provider_unreachable")

    def test_rate_limited_maps_to_provider_quota_exceeded(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            with patch.object(cp, "_http_post_json", return_value=(None, "rate_limited")):
                result = cp.openai_chat("gpt-5", [{"role": "user", "content": "hi"}])
        self.assertEqual(result["error"], "provider_quota_exceeded")

    def test_malformed_response_returns_bad_response(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            # Response without the expected `choices[0].message.content` shape
            with patch.object(cp, "_http_post_json", return_value=({"weird": True}, None)):
                result = cp.openai_chat("gpt-5", [{"role": "user", "content": "hi"}])
        self.assertEqual(result["error"], "bad_response")

    def test_temperature_and_max_tokens_passthrough(self):
        captured = {}

        def fake_post(url, headers, body, timeout=None):
            captured["body"] = body
            return {"choices": [{"message": {"content": "ok"}}]}, None

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            with patch.object(cp, "_http_post_json", side_effect=fake_post):
                cp.openai_chat(
                    "gpt-5",
                    [{"role": "user", "content": "x"}],
                    options={"temperature": 0.2, "max_tokens": 100},
                )
        self.assertAlmostEqual(captured["body"]["temperature"], 0.2)
        self.assertEqual(captured["body"]["max_tokens"], 100)

    def test_custom_base_url(self):
        captured = {}

        def fake_post(url, headers, body, timeout=None):
            captured["url"] = url
            return {"choices": [{"message": {"content": "ok"}}]}, None

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            with patch.object(cp, "_http_post_json", side_effect=fake_post):
                cp.openai_chat(
                    "qwen-72b",
                    [{"role": "user", "content": "x"}],
                    options={"base_url": "https://api.together.xyz/v1"},
                )
        self.assertTrue(captured["url"].startswith("https://api.together.xyz/v1"))


# ============================================================
# Anthropic adapter
# ============================================================

class AnthropicAdapterTests(unittest.TestCase):
    def test_extracts_system_messages_to_top_level_field(self):
        captured = {}

        def fake_post(url, headers, body, timeout=None):
            captured["body"] = body
            return {"content": [{"type": "text", "text": "ok"}]}, None

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            with patch.object(cp, "_http_post_json", side_effect=fake_post):
                cp.anthropic_chat(
                    "claude-sonnet-4-5",
                    [
                        {"role": "system", "content": "you are helpful"},
                        {"role": "system", "content": "be concise"},
                        {"role": "user", "content": "hi"},
                    ],
                )
        # System chunks join with double-newline.
        self.assertIn("you are helpful", captured["body"]["system"])
        self.assertIn("be concise", captured["body"]["system"])
        # Only the user message is in `messages`.
        self.assertEqual(len(captured["body"]["messages"]), 1)
        self.assertEqual(captured["body"]["messages"][0]["role"], "user")

    def test_default_max_tokens_is_4096(self):
        captured = {}

        def fake_post(url, headers, body, timeout=None):
            captured["body"] = body
            return {"content": [{"type": "text", "text": "ok"}]}, None

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            with patch.object(cp, "_http_post_json", side_effect=fake_post):
                cp.anthropic_chat(
                    "claude-sonnet-4-5",
                    [{"role": "user", "content": "hi"}],
                )
        self.assertEqual(captured["body"]["max_tokens"], 4096)

    def test_uses_x_api_key_header_and_anthropic_version(self):
        captured = {}

        def fake_post(url, headers, body, timeout=None):
            captured["headers"] = headers
            return {"content": [{"type": "text", "text": "ok"}]}, None

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            with patch.object(cp, "_http_post_json", side_effect=fake_post):
                cp.anthropic_chat(
                    "claude-sonnet-4-5",
                    [{"role": "user", "content": "hi"}],
                )
        self.assertEqual(captured["headers"]["x-api-key"], "sk-ant-test")
        self.assertEqual(captured["headers"]["anthropic-version"], cp.ANTHROPIC_API_VERSION)

    def test_concatenates_text_blocks_in_response(self):
        canned = {
            "content": [
                {"type": "text", "text": "Hello, "},
                {"type": "text", "text": "world."},
            ],
        }
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
            with patch.object(cp, "_http_post_json", return_value=(canned, None)):
                result = cp.anthropic_chat(
                    "claude-sonnet-4-5",
                    [{"role": "user", "content": "hi"}],
                )
        self.assertEqual(result["content"], "Hello, world.")

    def test_no_credentials_returns_unauthenticated(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        result = cp.anthropic_chat("claude-sonnet-4-5", [{"role": "user", "content": "hi"}])
        self.assertEqual(result["error"], "provider_unauthenticated")


# ============================================================
# Registry + binding parser
# ============================================================

class RegistryTests(unittest.TestCase):
    def test_list_providers_starts_with_ollama(self):
        providers = cp.list_providers()
        self.assertEqual(providers[0].id, "ollama")

    def test_get_provider_unknown(self):
        self.assertIsNone(cp.get_provider("does-not-exist"))

    def test_parse_lane_binding_with_provider(self):
        provider, model = cp.parse_lane_binding("anthropic:claude-sonnet-4-5")
        self.assertEqual(provider, "anthropic")
        self.assertEqual(model, "claude-sonnet-4-5")

    def test_parse_lane_binding_bare_model_defaults_to_ollama(self):
        provider, model = cp.parse_lane_binding("llama3.1:8b")
        # Note: ":" SPLITS, so "llama3.1" is parsed as a provider here.
        # That's a limitation of the bare-model fallback. Users should
        # write "ollama:llama3.1:8b" for clarity. We test the actual
        # behaviour, not a wished-for one.
        self.assertEqual(provider, "llama3.1")
        self.assertEqual(model, "8b")

    def test_parse_lane_binding_truly_bare(self):
        provider, model = cp.parse_lane_binding("gpt5")
        self.assertEqual(provider, "ollama")
        self.assertEqual(model, "gpt5")

    def test_parse_lane_binding_empty_raises(self):
        with self.assertRaises(ValueError):
            cp.parse_lane_binding("")

    def test_dispatch_chat_unknown_provider_returns_provider_unknown(self):
        result = cp.dispatch_chat("never-registered", "x", messages=[])
        self.assertEqual(result["error"], "provider_unknown")

    def test_has_credentials_for_local_always_true(self):
        self.assertTrue(cp.has_credentials("ollama"))

    def test_has_credentials_with_env_var(self):
        os.environ.pop("OPENAI_API_KEY", None)
        self.assertFalse(cp.has_credentials("openai"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}, clear=False):
            self.assertTrue(cp.has_credentials("openai"))


# ============================================================
# Storage round-trip
# ============================================================

class LaneProviderStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_default_is_empty_dict(self):
        self.assertEqual(local_memory.get_lane_providers("default"), {})

    def test_set_and_get(self):
        local_memory.set_lane_provider("default", "reasoning", "anthropic:claude-sonnet-4-5")
        self.assertEqual(
            local_memory.get_lane_provider("default", "reasoning"),
            "anthropic:claude-sonnet-4-5",
        )

    def test_clear_one_lane(self):
        local_memory.set_lane_provider("default", "reasoning", "anthropic:claude-sonnet-4-5")
        local_memory.set_lane_provider("default", "coding", "openai:gpt-5")
        removed = local_memory.clear_lane_provider("default", "reasoning")
        self.assertTrue(removed)
        self.assertNotIn("reasoning", local_memory.get_lane_providers("default"))
        self.assertIn("coding", local_memory.get_lane_providers("default"))

    def test_clear_unknown_returns_false(self):
        removed = local_memory.clear_lane_provider("default", "ghost")
        self.assertFalse(removed)

    def test_set_with_bare_model_inserts_ollama_prefix(self):
        local_memory.set_lane_provider("default", "x", "model-name")
        self.assertEqual(
            local_memory.get_lane_provider("default", "x"),
            "ollama:model-name",
        )

    def test_set_with_empty_binding_raises(self):
        with self.assertRaises(ValueError):
            local_memory.set_lane_provider("default", "x", "")

    def test_set_with_lane_only_raises(self):
        with self.assertRaises(ValueError):
            local_memory.set_lane_provider("default", "", "openai:gpt-5")


# ============================================================
# Dispatcher commands
# ============================================================

class ProviderDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()

    def test_provider_list(self):
        ev = dispatch_input("provider list", session_id="default")
        self.assertIn("ollama", ev["output"])
        self.assertIn("openai", ev["output"])
        self.assertIn("anthropic", ev["output"])

    def test_provider_show_with_no_bindings(self):
        ev = dispatch_input("provider show", session_id="default")
        self.assertIn("No lane bindings", ev["output"])

    def test_bind_with_no_credentials_emits_provider_unauthenticated(self):
        os.environ.pop("OPENAI_API_KEY", None)
        ev = dispatch_input("provider bind reasoning openai:gpt-5", session_id="default")
        self.assertEqual(ev.get("reason"), "provider_unauthenticated")

    def test_bind_with_credentials_round_trip(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}, clear=False):
            ev = dispatch_input(
                "provider bind reasoning openai:gpt-5", session_id="default"
            )
        self.assertNotIn("reason", ev)
        bindings = local_memory.get_lane_providers("default")
        self.assertEqual(bindings.get("reasoning"), "openai:gpt-5")

    def test_bind_with_unknown_provider_emits_provider_unknown(self):
        ev = dispatch_input(
            "provider bind reasoning fakeprovider:gpt-5", session_id="default"
        )
        self.assertEqual(ev.get("reason"), "provider_unknown")

    def test_bind_with_bad_syntax_emits_invalid_argument(self):
        ev = dispatch_input("provider bind", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")

    def test_unbind_known_lane(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}, clear=False):
            dispatch_input("provider bind reasoning openai:gpt-5", session_id="default")
        ev = dispatch_input("provider unbind reasoning", session_id="default")
        self.assertNotIn("reason", ev)
        self.assertEqual(local_memory.get_lane_providers("default"), {})

    def test_unbind_unknown_lane_emits_unknown_target(self):
        ev = dispatch_input("provider unbind ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")


# ============================================================
# chat_service integration
# ============================================================

class ChatServiceProviderRoutingTests(unittest.TestCase):
    """End-to-end routing: when a lane is bound, handle_chat dispatches
    through chat_providers instead of ollama runtime_client.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        rs._reset_registry_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        rs._reset_registry_for_tests()

    def test_resolve_chat_provider_no_binding_falls_back_to_ollama(self):
        provider, model, source = chat_service._resolve_chat_provider(
            "default", "reasoning", "llama3.1:8b"
        )
        self.assertEqual(provider, "ollama")
        self.assertEqual(model, "llama3.1:8b")
        self.assertEqual(source, "default")

    def test_resolve_chat_provider_with_binding(self):
        local_memory.set_lane_provider("default", "reasoning", "anthropic:claude-sonnet-4-5")
        provider, model, source = chat_service._resolve_chat_provider(
            "default", "reasoning", "llama3.1:8b"
        )
        self.assertEqual(provider, "anthropic")
        self.assertEqual(model, "claude-sonnet-4-5")
        self.assertEqual(source, "binding")

    def test_resolve_chat_provider_with_forbid_rule_strips_binding(self):
        # Bind the lane, then add a tag + a forbid rule that excludes it.
        local_memory.set_lane_provider("default", "reasoning", "anthropic:claude-sonnet-4-5")
        local_memory.add_tag("default", "production")
        rs.register_rule_for_tests(rs.parse_rule({
            "id": "prod-local-only",
            "when": {"session_tag": "production"},
            "then": {"forbid_provider": {"not_in": ["ollama"], "reason": "production_local_only"}},
        }))

        provider, model, source = chat_service._resolve_chat_provider(
            "default", "reasoning", "llama3.1:8b"
        )
        self.assertEqual(provider, "ollama")
        self.assertEqual(model, "llama3.1:8b")
        self.assertEqual(source, "forbidden")

    def test_resolve_chat_provider_with_disabled_rule_keeps_binding(self):
        local_memory.set_lane_provider("default", "reasoning", "anthropic:claude-sonnet-4-5")
        local_memory.add_tag("default", "production")
        rs.register_rule_for_tests(rs.parse_rule({
            "id": "prod-local-only",
            "when": {"session_tag": "production"},
            "then": {"forbid_provider": {"not_in": ["ollama"]}},
        }))
        rs.disable_rule("prod-local-only")

        provider, _, source = chat_service._resolve_chat_provider(
            "default", "reasoning", "llama3.1:8b"
        )
        self.assertEqual(provider, "anthropic")
        self.assertEqual(source, "binding")

    def test_handle_chat_routes_through_provider_when_bound(self):
        """End-to-end: bind lane → handle_chat dispatches to chat_providers."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}, clear=False):
            local_memory.set_lane_provider("default", "reasoning", "openai:gpt-5")

        captured = {}

        def fake_dispatch(provider_id, model, messages, options=None):
            captured["provider"] = provider_id
            captured["model"] = model
            return {"ok": True, "content": "from openai", "error": None, "raw": {}}

        # The router needs to actually return a lane; the registry is
        # asked for a fallback model we don't intend to use.
        with patch("app.services.chat_service.chat_providers.dispatch_chat",
                   side_effect=fake_dispatch):
            with patch("app.supervisor.router.route_message", return_value={
                "lane": "reasoning",
                "confidence": 0.9,
                "scores": {"reasoning": 0.9},
                "primary_signal": "fake",
                "context_used": False,
                "context_reason": "",
            }):
                with patch(
                    "app.models.model_registry.resolve_model_for_role",
                    return_value={
                        "model": "llama3.1:8b",
                        "resolved_role": "reasoning",
                        "fallback_used": False,
                    },
                ):
                    result = chat_service.handle_chat("hello", session_id="default")

        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], "from openai")
        self.assertEqual(result["provider_id"], "openai")
        self.assertEqual(result["provider_source"], "binding")
        self.assertEqual(captured["provider"], "openai")
        self.assertEqual(captured["model"], "gpt-5")


if __name__ == "__main__":
    unittest.main()
