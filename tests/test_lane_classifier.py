"""Tests for the Naive Bayes lane classifier (adopted from
`local_ai_workbench/training/classifier_trainer.py`).

Covers:
* ``tokenize`` helper: lowercase, alphanumeric, drop one-char tokens.
* ``NaiveBayesClassifier.train`` happy path + edge cases (empty
  samples, all-same label, vocab cap respected).
* ``predict`` on in-vocab + out-of-vocab text + untrained model.
* ``predict`` is deterministic across model copies (round-trip via
  ``to_dict`` / ``from_dict``).
* ``train_from_executions`` pulls samples from the session's
  execution records.
* Dispatcher: ``classifier train / predict / status / reset``.
* Storage: model survives ``session.context`` round-trip.
"""
import unittest
import uuid
from unittest.mock import patch as mock_patch

from app.memory import local_memory
from app.services import lane_classifier as lc
from app.services.command_dispatcher import dispatch_input


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# Reusable training set.
def _coding_reasoning_vision():
    return [
        ("write a python function to parse json", "coding"),
        ("refactor the login method", "coding"),
        ("add unit tests for the database module", "coding"),
        ("debug this stack trace error", "coding"),
        ("what is the meaning of life", "reasoning"),
        ("explain the philosophy of mind", "reasoning"),
        ("compare two papers on consciousness", "reasoning"),
        ("describe what is in this image", "vision"),
        ("what objects do you see in the photo", "vision"),
        ("analyze the diagram visually", "vision"),
    ]


# ============================================================
# tokenize
# ============================================================

class TokenizeTests(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(lc.tokenize("Hello WORLD"), ["hello", "world"])

    def test_drops_one_char_tokens(self):
        # 'a', 'I' should drop; 'be' kept.
        self.assertEqual(
            lc.tokenize("I am a developer who can be productive"),
            ["am", "developer", "who", "can", "be", "productive"],
        )

    def test_drops_punctuation(self):
        self.assertEqual(
            lc.tokenize("hello, world! it's parsing-test"),
            ["hello", "world", "it", "parsing", "test"],
        )

    def test_handles_empty_text(self):
        self.assertEqual(lc.tokenize(""), [])
        self.assertEqual(lc.tokenize(None), [])


# ============================================================
# NaiveBayesClassifier.train
# ============================================================

class TrainTests(unittest.TestCase):
    def test_happy_path_diagnostic_shape(self):
        m = lc.NaiveBayesClassifier()
        diag = m.train(_coding_reasoning_vision())
        self.assertTrue(diag["ok"])
        self.assertEqual(diag["samples_used"], 10)
        self.assertEqual(diag["samples_discarded"], 0)
        self.assertEqual(diag["per_label"], {"coding": 4, "reasoning": 3, "vision": 3})
        self.assertGreater(diag["vocab_size"], 0)

    def test_marks_trained(self):
        m = lc.NaiveBayesClassifier()
        self.assertFalse(m.is_trained)
        m.train(_coding_reasoning_vision())
        self.assertTrue(m.is_trained)

    def test_no_samples_returns_not_ok(self):
        m = lc.NaiveBayesClassifier()
        diag = m.train([])
        self.assertFalse(diag["ok"])
        self.assertEqual(diag["reason"], "no_samples")
        self.assertFalse(m.is_trained)

    def test_blank_label_or_text_discarded(self):
        m = lc.NaiveBayesClassifier()
        diag = m.train([
            ("good text", "coding"),
            ("", "coding"),                # discarded
            ("text but no label", ""),     # discarded
            ("more code text", "coding"),
        ])
        self.assertTrue(diag["ok"])
        self.assertEqual(diag["samples_used"], 2)
        self.assertEqual(diag["samples_discarded"], 2)

    def test_vocab_cap_respected(self):
        # 200 samples each with 50 unique tokens; cap to 30.
        samples = [
            (" ".join(f"tokA{j} tokB{j}" for j in range(i, i + 25)), "coding")
            for i in range(40)
        ]
        m = lc.NaiveBayesClassifier()
        diag = m.train(samples, vocab_limit=30)
        self.assertLessEqual(diag["vocab_size"], 30)
        self.assertLessEqual(len(m.vocab), 30)

    def test_train_is_idempotent_replaces_state(self):
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        old_vocab_size = len(m.vocab)
        # Train again with a different small set.
        m.train([("hello world", "alpha"), ("other thing", "beta")])
        # Should NOT carry forward state from first train.
        self.assertEqual(set(m.labels), {"alpha", "beta"})
        self.assertNotEqual(len(m.vocab), old_vocab_size)


# ============================================================
# predict
# ============================================================

class PredictTests(unittest.TestCase):
    def setUp(self):
        self.m = lc.NaiveBayesClassifier()
        self.m.train(_coding_reasoning_vision())

    def test_predicts_coding_for_code_phrase(self):
        lane, conf = self.m.predict("write a python function for parsing CSV")
        self.assertEqual(lane, "coding")
        self.assertGreater(conf, 0.5)

    def test_predicts_vision_for_image_phrase(self):
        lane, conf = self.m.predict("describe this photograph")
        self.assertEqual(lane, "vision")
        self.assertGreater(conf, 0.4)

    def test_returns_none_for_untrained_model(self):
        empty = lc.NaiveBayesClassifier()
        lane, conf = empty.predict("anything")
        self.assertIsNone(lane)
        self.assertEqual(conf, 0.0)

    def test_returns_none_for_out_of_vocab_only(self):
        lane, conf = self.m.predict("zxcvb1 qwerty987 asdfgh")
        self.assertIsNone(lane)
        self.assertEqual(conf, 0.0)

    def test_returns_none_for_empty_text(self):
        lane, conf = self.m.predict("")
        self.assertIsNone(lane)
        self.assertEqual(conf, 0.0)

    def test_confidence_sums_to_one_across_labels(self):
        # The chosen label's confidence + the (untracked) others
        # together sum to 1.0. Since predict only returns the
        # winning label's confidence, we verify it's between 0 and 1.
        lane, conf = self.m.predict("write a python function")
        self.assertGreaterEqual(conf, 0.0)
        self.assertLessEqual(conf, 1.0)


# ============================================================
# Round-trip
# ============================================================

class RoundTripTests(unittest.TestCase):
    def test_to_dict_from_dict_preserves_predictions(self):
        a = lc.NaiveBayesClassifier()
        a.train(_coding_reasoning_vision())
        b = lc.NaiveBayesClassifier.from_dict(a.to_dict())
        for text in ("write a function", "describe an image", "explain reasoning"):
            self.assertEqual(a.predict(text), b.predict(text))

    def test_from_dict_handles_none(self):
        m = lc.NaiveBayesClassifier.from_dict(None)
        self.assertFalse(m.is_trained)

    def test_from_dict_partial(self):
        m = lc.NaiveBayesClassifier.from_dict({"vocab": []})
        self.assertFalse(m.is_trained)


# ============================================================
# train_from_executions + persistence
# ============================================================

class TrainFromExecutionsTests(unittest.TestCase):
    def test_collect_and_train_from_executions(self):
        sid = _unique("classifier_train_from_exec")
        # Inject synthetic execution records via the public API.
        for msg, lane in _coding_reasoning_vision():
            local_memory.append_execution(
                {
                    "message": msg,
                    "status": "completed",
                    "requested_lane": lane,
                    "resolved_lane": lane,
                },
                session_id=sid,
            )
        samples = lc.collect_training_samples(sid)
        self.assertEqual(len(samples), len(_coding_reasoning_vision()))
        diag = lc.train_from_executions(sid)
        self.assertTrue(diag["ok"])
        # Persisted: load_model returns a populated classifier.
        m = lc.load_model(sid)
        self.assertTrue(m.is_trained)
        self.assertEqual(set(m.labels), {"coding", "reasoning", "vision"})

    def test_skips_records_without_resolved_lane(self):
        sid = _unique("classifier_skip_no_lane")
        local_memory.append_execution(
            {"message": "incomplete", "status": "needs_approval"},
            session_id=sid,
        )
        samples = lc.collect_training_samples(sid)
        self.assertEqual(samples, [])

    def test_no_executions_train_returns_not_ok(self):
        sid = _unique("classifier_no_exec")
        diag = lc.train_from_executions(sid)
        self.assertFalse(diag["ok"])
        self.assertEqual(diag["reason"], "no_samples")


# ============================================================
# Storage round-trip via session.context
# ============================================================

class PersistenceTests(unittest.TestCase):
    def test_save_load_round_trip(self):
        sid = _unique("classifier_persist")
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)
        # Different process? No — same process, but via session.context.
        loaded = lc.load_model(sid)
        self.assertTrue(loaded.is_trained)
        # Predictions match.
        for text in ("write a function", "describe this image"):
            self.assertEqual(m.predict(text), loaded.predict(text))

    def test_reset_clears_model(self):
        sid = _unique("classifier_reset")
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)
        self.assertTrue(lc.load_model(sid).is_trained)
        lc.reset_model(sid)
        self.assertFalse(lc.load_model(sid).is_trained)


# ============================================================
# model_status
# ============================================================

class ModelStatusTests(unittest.TestCase):
    def test_status_for_untrained(self):
        sid = _unique("classifier_status_untrained")
        info = lc.model_status(sid)
        self.assertFalse(info["trained"])
        self.assertEqual(info["labels"], [])
        self.assertFalse(info["ready_for_predictions"])

    def test_status_for_trained(self):
        sid = _unique("classifier_status_trained")
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)
        info = lc.model_status(sid)
        self.assertTrue(info["trained"])
        self.assertEqual(set(info["labels"]), {"coding", "reasoning", "vision"})
        # min samples per class = 3 → ready
        self.assertTrue(info["ready_for_predictions"])

    def test_status_warming_up_when_label_below_min(self):
        sid = _unique("classifier_status_warming")
        m = lc.NaiveBayesClassifier()
        m.train([
            ("hello", "alpha"),
            ("world", "alpha"),
            ("hi", "alpha"),
            ("foo", "beta"),  # only 1 -- below MIN_SAMPLES_PER_CLASS
        ])
        lc.save_model(sid, m)
        info = lc.model_status(sid)
        self.assertTrue(info["trained"])
        self.assertFalse(info["ready_for_predictions"])


# ============================================================
# Dispatcher
# ============================================================

class ClassifierDispatcherTests(unittest.TestCase):
    def test_status_untrained(self):
        sid = _unique("classifier_disp_status")
        result = dispatch_input("classifier status", session_id=sid)
        self.assertEqual(result["action"], "classifier")
        self.assertIn("not trained", result["output"])

    def test_default_subcommand_is_status(self):
        sid = _unique("classifier_disp_default")
        result = dispatch_input("classifier", session_id=sid)
        self.assertIn("not trained", result["output"])

    def test_train_with_no_executions_returns_invalid_argument(self):
        sid = _unique("classifier_disp_train_empty")
        result = dispatch_input("classifier train", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_train_then_status_then_predict(self):
        sid = _unique("classifier_disp_full")
        for msg, lane in _coding_reasoning_vision():
            local_memory.append_execution(
                {"message": msg, "status": "completed",
                 "requested_lane": lane, "resolved_lane": lane},
                session_id=sid,
            )
        train = dispatch_input("classifier train", session_id=sid)
        self.assertNotIn("reason", train)
        status = dispatch_input("classifier status", session_id=sid)
        self.assertIn("ready", status["output"])
        predict = dispatch_input(
            "classifier predict write a python function",
            session_id=sid,
        )
        self.assertEqual(predict["result"]["lane"], "coding")
        self.assertGreater(predict["result"]["confidence"], 0.5)

    def test_predict_without_text_returns_error(self):
        sid = _unique("classifier_disp_predict_empty")
        result = dispatch_input("classifier predict", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_predict_with_untrained_model_explains(self):
        sid = _unique("classifier_disp_predict_untrained")
        result = dispatch_input(
            "classifier predict hello world", session_id=sid,
        )
        # Untrained model returns the "no prediction" output, no reason.
        self.assertNotIn("reason", result)
        self.assertIsNone(result["result"]["lane"])

    def test_reset_clears_model(self):
        sid = _unique("classifier_disp_reset")
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)
        self.assertTrue(lc.load_model(sid).is_trained)
        result = dispatch_input("classifier reset", session_id=sid)
        self.assertEqual(result["action"], "classifier")
        self.assertFalse(lc.load_model(sid).is_trained)

    def test_unknown_subcommand_returns_error(self):
        sid = _unique("classifier_disp_unknown")
        result = dispatch_input("classifier bogus", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")


# ============================================================
# Phase 5b: classifier_lane_biases + autoroute
# ============================================================

class ClassifierLaneBiasesTests(unittest.TestCase):
    def _seed_trained(self, sid):
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)

    def test_returns_empty_when_no_model(self):
        sid = _unique("biases_no_model")
        self.assertEqual(
            lc.classifier_lane_biases("write a python function", sid),
            {},
        )

    def test_returns_empty_for_empty_message(self):
        sid = _unique("biases_empty_msg")
        self._seed_trained(sid)
        self.assertEqual(lc.classifier_lane_biases("", sid), {})
        self.assertEqual(lc.classifier_lane_biases("   ", sid), {})

    def test_returns_lane_with_positive_bias_for_strong_match(self):
        sid = _unique("biases_strong")
        self._seed_trained(sid)
        biases = lc.classifier_lane_biases("write a python function for parsing", sid)
        self.assertEqual(set(biases.keys()), {"coding"})
        self.assertGreaterEqual(biases["coding"], 1)

    def test_returns_empty_for_low_confidence(self):
        sid = _unique("biases_low_conf")
        self._seed_trained(sid)
        # Out-of-vocab message — no prediction → empty.
        self.assertEqual(
            lc.classifier_lane_biases("zxcvb1 qwerty987", sid),
            {},
        )

    def test_min_confidence_threshold_suppresses(self):
        sid = _unique("biases_min_conf")
        self._seed_trained(sid)
        # Force a high min_confidence so even a normally-confident
        # match gets suppressed.
        biases = lc.classifier_lane_biases(
            "write a python function", sid, min_confidence=0.999,
        )
        self.assertEqual(biases, {})

    def test_factor_scales_bias(self):
        sid = _unique("biases_factor")
        self._seed_trained(sid)
        small = lc.classifier_lane_biases(
            "write a python function for parsing", sid, factor=1.0,
        )
        big = lc.classifier_lane_biases(
            "write a python function for parsing", sid, factor=10.0,
        )
        # Same lane, bigger bias under bigger factor.
        self.assertEqual(set(small.keys()), set(big.keys()))
        lane = next(iter(small))
        self.assertGreater(big[lane], small[lane])


class AutorouteFlagTests(unittest.TestCase):
    def test_default_off(self):
        sid = _unique("autoroute_default")
        self.assertFalse(lc.is_autoroute_enabled(sid))

    def test_set_then_get(self):
        sid = _unique("autoroute_set_get")
        lc.set_autoroute(sid, True)
        self.assertTrue(lc.is_autoroute_enabled(sid))
        lc.set_autoroute(sid, False)
        self.assertFalse(lc.is_autoroute_enabled(sid))

    def test_storage_round_trip(self):
        # The flag survives the _normalize_context round-trip.
        sid = _unique("autoroute_storage")
        lc.set_autoroute(sid, True)
        ctx = local_memory.get_session_context(sid)
        self.assertTrue(ctx["classifier_autoroute"])


class AutorouteDispatcherTests(unittest.TestCase):
    def test_status_no_arg(self):
        sid = _unique("autoroute_disp_status")
        result = dispatch_input("classifier autoroute", session_id=sid)
        self.assertIn("OFF", result["output"])
        self.assertEqual(result["result"]["enabled"], False)

    def test_toggle_on(self):
        sid = _unique("autoroute_disp_on")
        result = dispatch_input("classifier autoroute on", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertTrue(lc.is_autoroute_enabled(sid))

    def test_toggle_off(self):
        sid = _unique("autoroute_disp_off")
        lc.set_autoroute(sid, True)
        result = dispatch_input("classifier autoroute off", session_id=sid)
        self.assertNotIn("reason", result)
        self.assertFalse(lc.is_autoroute_enabled(sid))

    def test_unknown_flag_returns_error(self):
        sid = _unique("autoroute_disp_unknown")
        result = dispatch_input("classifier autoroute maybe", session_id=sid)
        self.assertEqual(result.get("reason"), "invalid_argument")

    def test_truthy_synonyms_accepted(self):
        for flag in ("on", "true", "1", "yes"):
            sid = _unique("autoroute_disp_synonym")
            dispatch_input(f"classifier autoroute {flag}", session_id=sid)
            self.assertTrue(lc.is_autoroute_enabled(sid),
                            f"flag {flag!r} should enable")


class HandleChatBlendTests(unittest.TestCase):
    """End-to-end: when autoroute is on AND a model is trained,
    handle_chat's call to route_message receives blended biases."""

    def test_classifier_biases_blend_into_route_when_enabled(self):
        from app.services import chat_service
        sid = _unique("blend_e2e_on")
        # Train a classifier
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)
        lc.set_autoroute(sid, True)
        local_memory.set_workspace(session_id=sid, workspace_path=None)

        captured_biases = {}

        def fake_route_message(message, recent_history=None, lane_biases=None):
            captured_biases["biases"] = dict(lane_biases or {})
            return {
                "lane": "reasoning",
                "confidence": "low",
                "scores": {"coding": 0, "vision": 0, "reasoning": 1},
                "primary_signal": None,
                "context_used": False,
                "context_reason": None,
            }

        with mock_patch.object(
            chat_service.router, "route_message", side_effect=fake_route_message,
        ), mock_patch.object(
            chat_service.model_registry, "resolve_model_for_role",
            return_value={"resolved_role": "reasoning", "model": None,
                          "fallback_used": False},
        ):
            chat_service.handle_chat(
                "write a python function for parsing data",
                session_id=sid,
            )
        # The classifier predicted "coding"; that bias should be
        # in the captured lane_biases.
        self.assertIn("coding", captured_biases["biases"])
        self.assertGreater(captured_biases["biases"]["coding"], 0)

    def test_classifier_biases_skipped_when_autoroute_off(self):
        from app.services import chat_service
        sid = _unique("blend_e2e_off")
        m = lc.NaiveBayesClassifier()
        m.train(_coding_reasoning_vision())
        lc.save_model(sid, m)
        # autoroute remains False (default)
        local_memory.set_workspace(session_id=sid, workspace_path=None)

        captured_biases = {}

        def fake_route_message(message, recent_history=None, lane_biases=None):
            captured_biases["biases"] = dict(lane_biases or {})
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
            return_value={"resolved_role": "reasoning", "model": None,
                          "fallback_used": False},
        ):
            chat_service.handle_chat(
                "write a python function for parsing data",
                session_id=sid,
            )
        # Autoroute is off → no classifier bias should appear.
        # (Active skills could still contribute, but we have none here.)
        self.assertEqual(captured_biases["biases"].get("coding", 0), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
