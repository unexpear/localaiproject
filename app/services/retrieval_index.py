"""Pure-Python BM25 retrieval index over workspace documents
(adopted from ``local_ai_workbench/retrieval/indexer.py``).

Phase 2a of ``docs/design/recall-memory.md`` — the user's `ttttt`
project ships an OOM-safe BM25 indexer plus a grounded response
composer; we adapt the math here without the dependencies.

Phase 2a scope (this ship):
* ``BM25Index`` — pure data structure with ``add_document`` /
  ``search`` / ``stats``. Standard BM25 with k1=1.5, b=0.75
  defaults. Vocabulary cap so the per-session memory footprint
  stays bounded.
* ``index_workspace(workspace_path)`` — walks the workspace,
  picks text files by extension, applies a size cap, excludes
  the usual build / VCS dirs, returns a populated ``BM25Index``.
* Per-session in-memory cache so the second ``retrieve`` call
  doesn't re-walk the disk.
* Dispatcher: ``retrieve <query>`` (lazy-builds on first call) /
  ``retrieve build`` / ``retrieve status`` / ``retrieve clear``.

Phase 2b (planned): inject the top hits as a new prompt slot
("retrieved_docs") through the prompt-assembly system, with a
citation marker so the model knows where text came from.
"""
from __future__ import annotations

import math
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# ============================================================
# Tokenizer + stopwords
# ============================================================

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")

#: Tiny English stopword list. Not exhaustive — just enough to
#: remove the most common high-IDF noise terms. Mirrors a
#: typical IR baseline.
STOPWORDS: Set[str] = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "has", "had", "have", "her", "him", "his", "she", "they", "this",
    "that", "with", "from", "was", "were", "will", "would", "should",
    "could", "their", "there", "what", "when", "where", "which", "who",
    "why", "how", "into", "out", "any", "some", "more", "most", "than",
    "then", "such", "very", "much", "just", "only", "your", "our",
    "its", "it",
}


def tokenize(text: str) -> List[str]:
    """Lowercase + alphanumeric tokens, drop stopwords + 1-char
    tokens. Identical preprocessing for indexing AND query."""
    if not text:
        return []
    return [
        t.lower()
        for t in _TOKEN_RE.findall(str(text))
        if len(t) > 1 and t.lower() not in STOPWORDS
    ]


# ============================================================
# Index dataclass
# ============================================================

@dataclass
class IndexedDocument:
    """One document in the index. ``length`` is token count (after
    tokenisation); ``term_frequency`` is the bag of (token, count)."""

    path: str
    length: int
    term_frequency: Dict[str, int]
    excerpt: str = ""              # first ~200 chars for display
    mtime: float = 0.0             # for staleness checks


@dataclass
class BM25Hit:
    """One search result. ``score`` is the BM25 score; ``snippet``
    is the matching excerpt (first sentence containing a query
    term, falls back to the document's ``excerpt`` field)."""

    path: str
    score: float
    snippet: str
    matched_terms: List[str] = field(default_factory=list)


# ============================================================
# BM25 parameters (Robertson/Zaragoza defaults)
# ============================================================

DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

#: Cap on the inverted-index vocabulary. Tokens beyond this count
#: are dropped at index time — keeps memory bounded for big
#: workspaces.
DEFAULT_VOCAB_LIMIT = 50_000

#: Cap on stored documents. Beyond this, the indexer stops adding
#: more (this is hit only on extreme workspaces; the workspace
#: walker also caps by file count).
DEFAULT_MAX_DOCS = 5_000


class BM25Index:
    """In-memory BM25 inverted index. Not thread-safe by itself —
    callers serialize via ``_INDEX_LOCK`` (per-session) when going
    through ``get_or_build``."""

    def __init__(
        self,
        *,
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
        vocab_limit: int = DEFAULT_VOCAB_LIMIT,
        max_docs: int = DEFAULT_MAX_DOCS,
    ) -> None:
        self.k1 = float(k1)
        self.b = float(b)
        self.vocab_limit = int(vocab_limit)
        self.max_docs = int(max_docs)
        self._docs: Dict[str, IndexedDocument] = {}
        # postings[term] -> dict[path -> tf]; lazily filled.
        self._postings: Dict[str, Dict[str, int]] = {}
        # idf cache; invalidated on any add/remove.
        self._idf_cache: Optional[Dict[str, float]] = None

    # -------------------- lifecycle --------------------

    def __len__(self) -> int:
        return len(self._docs)

    @property
    def vocab_size(self) -> int:
        return len(self._postings)

    @property
    def is_empty(self) -> bool:
        return not self._docs

    @property
    def avg_doc_length(self) -> float:
        if not self._docs:
            return 0.0
        return sum(d.length for d in self._docs.values()) / len(self._docs)

    def stats(self) -> Dict[str, Any]:
        return {
            "doc_count": len(self._docs),
            "vocab_size": self.vocab_size,
            "avg_doc_length": self.avg_doc_length,
            "k1": self.k1,
            "b": self.b,
            "vocab_limit": self.vocab_limit,
            "max_docs": self.max_docs,
        }

    def clear(self) -> None:
        self._docs.clear()
        self._postings.clear()
        self._idf_cache = None

    # -------------------- mutation --------------------

    def add_document(
        self, path: str, text: str, *, mtime: float = 0.0,
    ) -> bool:
        """Add (or replace) one document. Returns True if added,
        False if the index is at ``max_docs`` capacity. Replacing
        an existing path is always permitted."""
        if path in self._docs:
            self.remove_document(path)
        elif len(self._docs) >= self.max_docs:
            return False

        tokens = tokenize(text)
        if not tokens:
            return False

        tf: Dict[str, int] = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1

        # Vocab cap: if adding this doc's distinct terms would push
        # us over, drop the least-common postings until we fit.
        # This is rare in practice — only triggered on extreme
        # corpora.
        if len(self._postings) + len(tf) > self.vocab_limit:
            self._evict_postings(
                target=self.vocab_limit - len(tf)
            )

        excerpt = (text or "").strip()[:240].replace("\n", " ")
        self._docs[path] = IndexedDocument(
            path=path,
            length=len(tokens),
            term_frequency=tf,
            excerpt=excerpt,
            mtime=float(mtime or 0.0),
        )
        for term, count in tf.items():
            self._postings.setdefault(term, {})[path] = count
        self._idf_cache = None
        return True

    def remove_document(self, path: str) -> bool:
        """Drop a document by path. Returns True if it was present."""
        doc = self._docs.pop(path, None)
        if doc is None:
            return False
        for term in doc.term_frequency:
            posting = self._postings.get(term)
            if not posting:
                continue
            posting.pop(path, None)
            if not posting:
                self._postings.pop(term, None)
        self._idf_cache = None
        return True

    def _evict_postings(self, target: int) -> None:
        """Drop the least-common postings until ``len(self._postings)
        <= target``. Last-resort vocab management — only fires on
        extreme corpora."""
        if target < 0:
            target = 0
        if len(self._postings) <= target:
            return
        # Sort terms by ascending posting count (= least common first).
        terms_by_freq = sorted(
            self._postings.items(),
            key=lambda kv: len(kv[1]),
        )
        for term, posting in terms_by_freq:
            if len(self._postings) <= target:
                return
            # Remove from each doc's tf too so doc counts stay consistent.
            for path in list(posting):
                doc = self._docs.get(path)
                if doc is not None:
                    doc.term_frequency.pop(term, None)
            self._postings.pop(term, None)

    # -------------------- search --------------------

    def _idf(self, term: str) -> float:
        # Robertson IDF: log((N - df + 0.5) / (df + 0.5) + 1).
        # The +1 guards the log against zero/negative when df > N
        # (impossible here but defensive).
        n = len(self._docs)
        df = len(self._postings.get(term, {}))
        if df == 0 or n == 0:
            return 0.0
        return math.log(((n - df + 0.5) / (df + 0.5)) + 1.0)

    def _ensure_idf_cache(self) -> Dict[str, float]:
        if self._idf_cache is None:
            self._idf_cache = {
                term: self._idf(term) for term in self._postings
            }
        return self._idf_cache

    def search(self, query: str, *, limit: int = 10) -> List[BM25Hit]:
        """Return up to ``limit`` ``BM25Hit`` records ordered by
        descending score. Empty result for empty index, empty query,
        or query with no in-vocab terms."""
        if not query or self.is_empty:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        idf = self._ensure_idf_cache()
        avg_len = self.avg_doc_length or 1.0

        # Score per candidate doc.
        scores: Dict[str, float] = {}
        matched: Dict[str, List[str]] = {}
        for term in query_tokens:
            posting = self._postings.get(term)
            if not posting:
                continue
            term_idf = idf.get(term, 0.0)
            if term_idf <= 0.0:
                continue
            for path, tf in posting.items():
                doc = self._docs.get(path)
                if doc is None:
                    continue
                # Standard BM25 saturation.
                norm = (
                    tf * (self.k1 + 1)
                    / (tf + self.k1 * (1 - self.b + self.b * doc.length / avg_len))
                )
                scores[path] = scores.get(path, 0.0) + term_idf * norm
                matched.setdefault(path, [])
                if term not in matched[path]:
                    matched[path].append(term)

        if not scores:
            return []

        # Top-k by score, then path for stable order.
        ordered = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[: max(1, int(limit or 10))]

        hits: List[BM25Hit] = []
        for path, score in ordered:
            doc = self._docs[path]
            hits.append(BM25Hit(
                path=path,
                score=score,
                snippet=doc.excerpt,
                matched_terms=list(matched.get(path, [])),
            ))
        return hits


# ============================================================
# Workspace walker
# ============================================================

#: File extensions worth indexing. Avoid binary-by-default
#: (.png, .jpg, .pdf, .zip, ...) and giant data files (.csv).
DEFAULT_EXTENSIONS: Set[str] = {
    ".py", ".md", ".txt", ".rst", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".scss", ".sh", ".bat", ".ps1",
    ".java", ".kt", ".go", ".rs", ".rb", ".php", ".lua",
    ".c", ".h", ".cpp", ".hpp", ".cs",
    ".sql", ".gql", ".graphql",
}

#: Directories to skip during the walk. Common build/VCS noise.
DEFAULT_EXCLUDE_DIRS: Set[str] = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", "env", ".env", "dist", "build", "target",
    ".pytest_cache", ".mypy_cache", ".tox", ".idea", ".vscode",
    "site-packages", ".gradle", ".next", ".nuxt", ".cache",
}

#: Hard cap per file. Larger files are skipped (likely generated
#: or asset-shaped). Mirrors the source pattern's chunking gate.
DEFAULT_MAX_FILE_BYTES = 256 * 1024

#: Hard cap on total files indexed. Big monorepos won't push past
#: this; the user can still raise it via ``index_workspace`` kwargs.
DEFAULT_MAX_FILES = 2_000


def index_workspace(
    workspace_path: str,
    *,
    extensions: Optional[Set[str]] = None,
    exclude_dirs: Optional[Set[str]] = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    vocab_limit: int = DEFAULT_VOCAB_LIMIT,
    max_docs: int = DEFAULT_MAX_DOCS,
) -> Tuple[BM25Index, Dict[str, Any]]:
    """Walk ``workspace_path``, build a fresh ``BM25Index``.
    Returns ``(index, stats)`` where ``stats`` is a diagnostic
    dict with file-walk counts."""
    extensions = extensions if extensions is not None else DEFAULT_EXTENSIONS
    exclude = exclude_dirs if exclude_dirs is not None else DEFAULT_EXCLUDE_DIRS
    index = BM25Index(vocab_limit=vocab_limit, max_docs=max_docs)
    diag = {
        "indexed": 0,
        "skipped_binary": 0,
        "skipped_oversize": 0,
        "skipped_unreadable": 0,
        "skipped_unknown_ext": 0,
        "stopped_at_max_files": False,
    }
    if not workspace_path or not os.path.isdir(workspace_path):
        return index, diag

    for root, dirs, files in os.walk(workspace_path):
        # Mutate `dirs` in place to prune excluded subdirectories.
        dirs[:] = [d for d in dirs if d not in exclude and not d.startswith(".")]
        for fname in files:
            if diag["indexed"] >= max_files:
                diag["stopped_at_max_files"] = True
                return index, diag
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                diag["skipped_unknown_ext"] += 1
                continue
            full_path = os.path.join(root, fname)
            try:
                size = os.path.getsize(full_path)
            except OSError:
                diag["skipped_unreadable"] += 1
                continue
            if size > max_file_bytes:
                diag["skipped_oversize"] += 1
                continue
            text = _read_text_safely(full_path)
            if text is None:
                diag["skipped_binary"] += 1
                continue
            rel = os.path.relpath(full_path, workspace_path).replace("\\", "/")
            try:
                mtime = os.path.getmtime(full_path)
            except OSError:
                mtime = 0.0
            if index.add_document(rel, text, mtime=mtime):
                diag["indexed"] += 1
    return index, diag


def _read_text_safely(path: str) -> Optional[str]:
    """Return the file's decoded UTF-8 content or None if it
    looks binary (null byte in the first 2 KB) / unreadable."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(2048)
            if b"\x00" in head:
                return None
            rest = fh.read()
        raw = head + rest
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


# ============================================================
# Per-session cache
# ============================================================

#: Module-level cache. Each session has at most one BM25Index in
#: memory at a time. Cleared with ``clear`` or on workspace pin
#: change (caller's responsibility — we don't subscribe to context
#: events to keep this layer simple).
_INDEXES: Dict[str, BM25Index] = {}
_INDEX_LOCK = threading.Lock()


def get_or_build(
    session_id: str = "default",
    workspace_path: Optional[str] = None,
    **index_kwargs: Any,
) -> Tuple[Optional[BM25Index], Dict[str, Any]]:
    """Return the cached index for ``session_id``, or build a fresh
    one if not yet cached. Returns ``(index, stats)`` — stats is
    empty when the index was already cached.
    """
    with _INDEX_LOCK:
        cached = _INDEXES.get(session_id)
    if cached is not None:
        return cached, {"cached": True, **cached.stats()}

    if not workspace_path:
        return None, {"cached": False, "reason": "no_workspace"}

    index, diag = index_workspace(workspace_path, **index_kwargs)
    with _INDEX_LOCK:
        _INDEXES[session_id] = index
    diag["cached"] = False
    diag.update(index.stats())
    return index, diag


def rebuild(
    session_id: str = "default",
    workspace_path: Optional[str] = None,
    **index_kwargs: Any,
) -> Tuple[Optional[BM25Index], Dict[str, Any]]:
    """Force a fresh index build, replacing any cached index."""
    if not workspace_path:
        return None, {"cached": False, "reason": "no_workspace"}
    index, diag = index_workspace(workspace_path, **index_kwargs)
    with _INDEX_LOCK:
        _INDEXES[session_id] = index
    diag["cached"] = False
    diag.update(index.stats())
    return index, diag


def clear(session_id: str = "default") -> bool:
    """Drop the cached index for ``session_id``. Returns True iff
    one was present."""
    with _INDEX_LOCK:
        return _INDEXES.pop(session_id, None) is not None


def status(session_id: str = "default") -> Dict[str, Any]:
    """Inspect the cached index without building anything new."""
    with _INDEX_LOCK:
        cached = _INDEXES.get(session_id)
    if cached is None:
        return {"cached": False, "doc_count": 0, "vocab_size": 0}
    return {"cached": True, **cached.stats()}


def search(
    session_id: str = "default",
    query: str = "",
    *,
    limit: int = 10,
    workspace_path: Optional[str] = None,
) -> Tuple[List[BM25Hit], Dict[str, Any]]:
    """Lazy-build (if needed) + search. Convenience wrapper for
    the dispatcher."""
    index, diag = get_or_build(session_id, workspace_path=workspace_path)
    if index is None:
        return [], diag
    hits = index.search(query, limit=limit)
    return hits, diag


def format_hits(hits: List[BM25Hit]) -> str:
    """Render a list of ``BM25Hit`` as a readable text block for
    the dispatcher's output."""
    if not hits:
        return "(no matches)"
    lines: List[str] = [f"Top {len(hits)} hit(s):"]
    for i, hit in enumerate(hits, 1):
        terms = ",".join(hit.matched_terms[:5])
        lines.append(
            f"  {i}. {hit.path}  (score: {hit.score:.3f}, terms: {terms})"
        )
        if hit.snippet:
            preview = hit.snippet[:120]
            if len(hit.snippet) > 120:
                preview += "..."
            lines.append(f"     {preview}")
    return "\n".join(lines)
