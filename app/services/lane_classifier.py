"""Pure-Python Naive Bayes lane classifier (adopted from
``local_ai_workbench/training/classifier_trainer.py``).

The user's other project ships a small, dependency-free Multinomial
Naive Bayes implementation for classifying short text into a small
set of labels — used there for routing training-data records into
content classes. We adapt the same pattern to learn the lane
(``coding`` / ``reasoning`` / ``vision``) from the user's own past
chat turns + each turn's ``resolved_lane`` recorded by the chat
flow.

Phase 1 ships:
* ``NaiveBayesClassifier`` — train / predict / serialize. Vocab
  capped at 5000 tokens (mirrors the source pattern); Laplace
  smoothing with α=1.
* ``train_from_executions(session_id)`` — walks the session's
  execution records, builds (message, resolved_lane) samples,
  fits a fresh classifier.
* ``predict_lane(text, session_id)`` — convenience wrapper that
  loads the persisted model and returns
  ``(lane, confidence)`` or ``(None, 0.0)`` if no model is trained
  yet.
* Persistence in ``session.context["classifier"]`` so the model
  survives process restart.

Phase 2 (planned): blend the learned predictions with the
existing rule-based ``router.route_message`` scores in
``app/supervisor/router.py``. Phase 1 deliberately leaves the
existing router untouched — the classifier is a parallel, opt-in
preview surface (``classifier predict <text>``) so users can
evaluate it before we wire it into the live routing path.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


#: Maximum vocab size. Mirrors ``classifier_trainer.py``'s default —
#: large enough to cover the keywords that distinguish lanes, small
#: enough that the JSON-serialised model fits comfortably in
#: session.context.
DEFAULT_VOCAB_LIMIT = 5_000

#: Minimum samples per class before we consider the classifier
#: "trained enough" to surface predictions. Mirrors a sensible
#: floor; below this, predict_lane returns (None, 0.0).
MIN_SAMPLES_PER_CLASS = 3

#: Laplace smoothing constant (α). Adopted verbatim from the source.
ALPHA = 1.0

#: Token regex — split on word boundaries, keep alphanumerics +
#: underscore. Lowercase happens after match.
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")


def tokenize(text: str) -> List[str]:
    """Cheap tokenizer — lowercase, alphanumeric tokens only,
    drop one-character tokens (low signal)."""
    if not text:
        return []
    return [
        t.lower()
        for t in _TOKEN_RE.findall(str(text))
        if len(t) > 1
    ]


# ============================================================
# Model
# ============================================================

@dataclass
class NaiveBayesClassifier:
    """Multinomial Naive Bayes over a token-bag feature space.

    Stored fields (all JSON-safe):
    * ``vocab`` — sorted list of token strings; index = feature id.
    * ``log_priors`` — dict label → log P(label).
    * ``log_counts`` — dict label → list[float] (length == len(vocab));
      log P(token | label) under Laplace smoothing.
    * ``label_doc_counts`` — dict label → int (training sample count
      per label, used by ``status`` reporting).
    * ``vocab_limit`` — captured at training time.
    """

    vocab: List[str] = field(default_factory=list)
    log_priors: Dict[str, float] = field(default_factory=dict)
    log_counts: Dict[str, List[float]] = field(default_factory=dict)
    label_doc_counts: Dict[str, int] = field(default_factory=dict)
    vocab_limit: int = DEFAULT_VOCAB_LIMIT

    @property
    def is_trained(self) -> bool:
        return bool(self.vocab and self.log_priors)

    @property
    def labels(self) -> List[str]:
        return sorted(self.log_priors.keys())

    def train(
        self,
        samples: List[Tuple[str, str]],
        *,
        vocab_limit: int = DEFAULT_VOCAB_LIMIT,
    ) -> Dict[str, Any]:
        """Fit the model from ``(text, label)`` pairs. Returns a
        diagnostic dict with vocab size, per-label counts, and the
        number of samples discarded (empty / unlabeled).

        Idempotent: calling ``train`` again replaces the model.
        """
        # 1) Filter / count samples per label.
        per_label_tokens: Dict[str, List[List[str]]] = defaultdict(list)
        discarded = 0
        for text, label in samples or []:
            label = str(label or "").strip()
            if not label:
                discarded += 1
                continue
            tokens = tokenize(text)
            if not tokens:
                discarded += 1
                continue
            per_label_tokens[label].append(tokens)

        if not per_label_tokens:
            # Nothing usable — clear state and bail.
            self.vocab = []
            self.log_priors = {}
            self.log_counts = {}
            self.label_doc_counts = {}
            self.vocab_limit = vocab_limit
            return {
                "ok": False,
                "reason": "no_samples",
                "samples_used": 0,
                "samples_discarded": discarded,
            }

        # 2) Build vocab from the most-frequent tokens across the
        # whole corpus, capped at ``vocab_limit``. Mirror the source
        # project's "global frequency cap, not per-class" choice.
        global_counter: Counter = Counter()
        for token_lists in per_label_tokens.values():
            for tokens in token_lists:
                global_counter.update(tokens)
        ranked = [tok for tok, _ in global_counter.most_common(vocab_limit)]
        self.vocab = sorted(ranked)
        vocab_index = {tok: idx for idx, tok in enumerate(self.vocab)}

        # 3) Per-label counts + priors.
        total_docs = sum(len(tl) for tl in per_label_tokens.values())
        v = len(self.vocab)
        self.log_priors = {}
        self.log_counts = {}
        self.label_doc_counts = {}
        for label, token_lists in per_label_tokens.items():
            doc_count = len(token_lists)
            self.label_doc_counts[label] = doc_count
            self.log_priors[label] = math.log(doc_count / total_docs)

            # Token counts within this label, restricted to vocab.
            tok_count = Counter()
            for tokens in token_lists:
                for tok in tokens:
                    idx = vocab_index.get(tok)
                    if idx is not None:
                        tok_count[idx] += 1
            total_tokens_for_label = sum(tok_count.values())
            denom = total_tokens_for_label + ALPHA * v
            self.log_counts[label] = [
                math.log((tok_count.get(idx, 0) + ALPHA) / denom)
                for idx in range(v)
            ]

        self.vocab_limit = vocab_limit
        return {
            "ok": True,
            "vocab_size": v,
            "samples_used": total_docs,
            "samples_discarded": discarded,
            "per_label": dict(self.label_doc_counts),
        }

    def predict(self, text: str) -> Tuple[Optional[str], float]:
        """Return ``(label, confidence)`` for ``text`` where
        confidence is the softmax-normalised probability of the
        winning label. Returns ``(None, 0.0)`` if the model isn't
        trained or the text yields no in-vocab tokens (cold-start
        case)."""
        if not self.is_trained:
            return None, 0.0
        tokens = tokenize(text)
        if not tokens:
            return None, 0.0
        vocab_index = {tok: idx for idx, tok in enumerate(self.vocab)}
        # Bag-of-tokens feature vector (counts).
        feature_indices: List[int] = []
        for tok in tokens:
            idx = vocab_index.get(tok)
            if idx is not None:
                feature_indices.append(idx)
        if not feature_indices:
            return None, 0.0

        log_scores: Dict[str, float] = {}
        for label, log_prior in self.log_priors.items():
            score = log_prior
            counts = self.log_counts.get(label) or []
            for idx in feature_indices:
                if idx < len(counts):
                    score += counts[idx]
            log_scores[label] = score

        # Softmax over the log-scores -> confidence.
        max_log = max(log_scores.values())
        exp_scores = {l: math.exp(s - max_log) for l, s in log_scores.items()}
        denom = sum(exp_scores.values()) or 1.0
        winner = max(exp_scores, key=exp_scores.get)
        confidence = exp_scores[winner] / denom
        return winner, confidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vocab": list(self.vocab),
            "log_priors": dict(self.log_priors),
            "log_counts": {
                label: list(counts) for label, counts in self.log_counts.items()
            },
            "label_doc_counts": dict(self.label_doc_counts),
            "vocab_limit": int(self.vocab_limit),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "NaiveBayesClassifier":
        data = data or {}
        return cls(
            vocab=list(data.get("vocab") or []),
            log_priors=dict(data.get("log_priors") or {}),
            log_counts={
                str(k): list(v)
                for k, v in (data.get("log_counts") or {}).items()
            },
            label_doc_counts=dict(data.get("label_doc_counts") or {}),
            vocab_limit=int(data.get("vocab_limit") or DEFAULT_VOCAB_LIMIT),
        )


# ============================================================
# Persistence + session integration
# ============================================================

def load_model(session_id: str = "default") -> NaiveBayesClassifier:
    """Load the persisted classifier from
    ``session.context["classifier"]`` (or return an empty one if
    none has been trained yet)."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id) or {}
    return NaiveBayesClassifier.from_dict(context.get("classifier"))


def save_model(session_id: str, model: NaiveBayesClassifier) -> None:
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id)
    context["classifier"] = model.to_dict()
    local_memory.update_session_context(session_id=session_id, context=context)


def reset_model(session_id: str = "default") -> None:
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id)
    context["classifier"] = {}
    local_memory.update_session_context(session_id=session_id, context=context)


def collect_training_samples(
    session_id: str = "default", *, limit: int = 200,
) -> List[Tuple[str, str]]:
    """Pull ``(message, resolved_lane)`` pairs from the session's
    execution records. Skips entries without a ``resolved_lane``
    (failed-before-routing executions).
    """
    from app.memory import local_memory
    executions = local_memory.get_executions(session_id=session_id, limit=limit) or []
    samples: List[Tuple[str, str]] = []
    for record in executions:
        if not isinstance(record, dict):
            continue
        msg = (record.get("message") or "").strip()
        lane = (record.get("resolved_lane") or "").strip()
        if not msg or not lane:
            continue
        samples.append((msg, lane))
    return samples


def train_from_executions(
    session_id: str = "default",
    *,
    limit: int = 200,
    vocab_limit: int = DEFAULT_VOCAB_LIMIT,
) -> Dict[str, Any]:
    """Pull training samples from the session's executions, fit a
    fresh classifier, persist it. Returns the diagnostic dict from
    ``train`` plus a top-level ``samples`` count."""
    samples = collect_training_samples(session_id, limit=limit)
    model = NaiveBayesClassifier()
    diag = model.train(samples, vocab_limit=vocab_limit)
    diag["samples"] = len(samples)
    save_model(session_id, model)
    return diag


def predict_lane(
    text: str, session_id: str = "default",
) -> Tuple[Optional[str], float]:
    """Convenience wrapper: load the persisted model, return
    ``(lane, confidence)`` for ``text``. Returns ``(None, 0.0)``
    when no model is trained or the text has no in-vocab tokens.
    """
    model = load_model(session_id)
    return model.predict(text)


def model_status(session_id: str = "default") -> Dict[str, Any]:
    """Inspect the persisted model — for ``classifier status``."""
    model = load_model(session_id)
    return {
        "trained": model.is_trained,
        "vocab_size": len(model.vocab),
        "labels": model.labels,
        "label_doc_counts": dict(model.label_doc_counts),
        "vocab_limit": model.vocab_limit,
        "ready_for_predictions": (
            model.is_trained
            and all(
                count >= MIN_SAMPLES_PER_CLASS
                for count in model.label_doc_counts.values()
            )
        ),
    }


# ============================================================
# Phase 5b: blend into the rule-based router
# ============================================================

#: Default amplification factor when converting the classifier's
#: confidence (0..1 float) into the rule-router's integer bias
#: scale (~0..10). With factor=4, a 0.85-confident vote contributes
#: a +3 bias — meaningful but non-overwhelming next to the
#: rule-router's keyword signals (which can reach ~10).
DEFAULT_BIAS_FACTOR = 4.0

#: Below this confidence, the classifier's vote is suppressed
#: entirely. Mirrors the source pattern's threshold for "is the
#: prediction worth acting on". Prevents a 35%-vs-33%-vs-32%
#: three-way split from dragging the router around.
DEFAULT_MIN_CONFIDENCE = 0.55


def classifier_lane_biases(
    message: str,
    session_id: str = "default",
    *,
    factor: float = DEFAULT_BIAS_FACTOR,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> Dict[str, int]:
    """Convert the classifier's prediction into a ``lane_biases``
    dict the rule-router can consume directly.

    Returns ``{}`` when:

    * No model is trained for this session.
    * The classifier returns no prediction (out-of-vocab message).
    * The prediction's confidence is below ``min_confidence``.

    Otherwise returns ``{predicted_lane: int(round(confidence * factor))}``.
    The bias is at least 1 when the confidence > min_confidence so a
    low-but-acceptable vote still nudges the router.
    """
    if not message or not message.strip():
        return {}
    lane, confidence = predict_lane(message, session_id=session_id)
    if lane is None:
        return {}
    if confidence < float(min_confidence or 0):
        return {}
    bias = max(1, int(round(confidence * float(factor or 0))))
    return {lane: bias}


def is_autoroute_enabled(session_id: str = "default") -> bool:
    """Whether the classifier's predictions get folded into the
    live route. Off by default — the user opts in via
    ``classifier autoroute on``."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id) or {}
    return bool(context.get("classifier_autoroute"))


def set_autoroute(session_id: str = "default", enabled: bool = False) -> bool:
    """Persist the autoroute flag for ``session_id``. Returns the
    new value."""
    from app.memory import local_memory
    context = local_memory.get_session_context(session_id)
    context["classifier_autoroute"] = bool(enabled)
    local_memory.update_session_context(session_id=session_id, context=context)
    return bool(enabled)
