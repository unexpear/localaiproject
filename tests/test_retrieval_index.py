"""Tests for the BM25 retrieval index (adopted from
`local_ai_workbench/retrieval/indexer.py`).

Covers:
* ``tokenize`` helper: stopwords dropped, lowercase, alphanumeric.
* ``BM25Index`` math: term frequency saturation, IDF favours rare
  terms, multi-term query is sum-of-IDF-weighted scores.
* Mutation: add / remove / re-add (replace), vocab cap, max_docs cap.
* Search: exact match wins, partial match still scores, irrelevant
  query → empty list.
* Workspace walker: indexes whitelisted files, skips excluded dirs,
  skips oversize / binary, stops at max_files.
* Per-session cache: get_or_build returns same instance, rebuild
  replaces, clear drops.
* Dispatcher: all four subcommands round-trip.
"""
import os
import tempfile
import unittest
import uuid

from app.memory import local_memory
from app.services import retrieval_index as ri
from app.services.command_dispatcher import dispatch_input


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ============================================================
# Tokenizer
# ============================================================

class TokenizeTests(unittest.TestCase):
    def test_drops_stopwords(self):
        self.assertEqual(
            ri.tokenize("the quick brown fox"),
            ["quick", "brown", "fox"],
        )

    def test_lowercases(self):
        self.assertEqual(
            ri.tokenize("PYTHON Code"),
            ["python", "code"],
        )

    def test_alphanumeric_only(self):
        self.assertEqual(
            ri.tokenize("hello, world! 123 test_case"),
            ["hello", "world", "test_case"],
        )

    def test_empty_input(self):
        self.assertEqual(ri.tokenize(""), [])
        self.assertEqual(ri.tokenize(None), [])


# ============================================================
# BM25 math
# ============================================================

class BM25MathTests(unittest.TestCase):
    def test_empty_index_returns_no_hits(self):
        idx = ri.BM25Index()
        self.assertEqual(idx.search("anything"), [])

    def test_empty_query_returns_no_hits(self):
        idx = ri.BM25Index()
        idx.add_document("a.txt", "hello world")
        self.assertEqual(idx.search(""), [])

    def test_exact_term_wins(self):
        idx = ri.BM25Index()
        idx.add_document("a.txt", "fox jumps over dog")
        idx.add_document("b.txt", "cat purrs softly")
        hits = idx.search("fox")
        self.assertEqual(hits[0].path, "a.txt")
        self.assertEqual(hits[0].matched_terms, ["fox"])

    def test_rare_term_scores_higher_than_common_term(self):
        # IDF favours rare terms — a query for a term that appears
        # in only one doc should score higher than one that appears
        # everywhere.
        idx = ri.BM25Index()
        idx.add_document("a.txt", "common common common rare")
        idx.add_document("b.txt", "common common common")
        idx.add_document("c.txt", "common common")
        hits_rare = idx.search("rare")
        hits_common = idx.search("common")
        # Rare query → only one hit; common → multiple but lower score per hit.
        self.assertEqual(len(hits_rare), 1)
        # The rare-term hit should outscore the best common-term hit
        # (because IDF for "common" is ~0).
        self.assertGreater(hits_rare[0].score, hits_common[0].score)

    def test_multi_term_score_sums(self):
        idx = ri.BM25Index()
        idx.add_document("a.txt", "fox dog")
        idx.add_document("b.txt", "fox only")
        # "fox dog" should score a.txt higher than b.txt — both
        # match "fox" but only a.txt matches "dog".
        hits = idx.search("fox dog")
        self.assertEqual(hits[0].path, "a.txt")
        self.assertEqual(set(hits[0].matched_terms), {"fox", "dog"})

    def test_term_frequency_saturates(self):
        # BM25 saturates: 10x the tf does NOT 10x the score.
        idx = ri.BM25Index()
        idx.add_document(
            "few.txt",
            "fox " + "filler word ".join("abcdefghij"),
        )
        idx.add_document(
            "many.txt",
            "fox " * 50 + "filler word ".join("klmnopqrst"),
        )
        hits = idx.search("fox")
        # many.txt scores higher (more occurrences) but not 50x.
        many = next(h for h in hits if h.path == "many.txt")
        few = next(h for h in hits if h.path == "few.txt")
        self.assertGreater(many.score, few.score)
        self.assertLess(many.score, few.score * 50)

    def test_query_with_only_stopwords_returns_no_hits(self):
        idx = ri.BM25Index()
        idx.add_document("a.txt", "the quick brown fox")
        self.assertEqual(idx.search("the and for"), [])

    def test_search_limit_respected(self):
        idx = ri.BM25Index()
        for i in range(20):
            idx.add_document(f"d{i}.txt", "matching")
        hits = idx.search("matching", limit=5)
        self.assertEqual(len(hits), 5)


# ============================================================
# Mutation
# ============================================================

class MutationTests(unittest.TestCase):
    def test_add_then_remove(self):
        idx = ri.BM25Index()
        idx.add_document("a.txt", "hello world")
        self.assertEqual(len(idx), 1)
        self.assertTrue(idx.remove_document("a.txt"))
        self.assertEqual(len(idx), 0)
        self.assertEqual(idx.search("hello"), [])

    def test_remove_nonexistent_returns_false(self):
        idx = ri.BM25Index()
        self.assertFalse(idx.remove_document("nope.txt"))

    def test_add_replaces_existing(self):
        idx = ri.BM25Index()
        idx.add_document("a.txt", "first version with hello")
        idx.add_document("a.txt", "second version with goodbye")
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx.search("hello"), [])  # gone
        hits = idx.search("goodbye")
        self.assertEqual(hits[0].path, "a.txt")

    def test_add_empty_text_rejected(self):
        idx = ri.BM25Index()
        self.assertFalse(idx.add_document("a.txt", ""))
        self.assertEqual(len(idx), 0)

    def test_add_only_stopwords_rejected(self):
        idx = ri.BM25Index()
        self.assertFalse(idx.add_document("a.txt", "the and for"))
        self.assertEqual(len(idx), 0)

    def test_max_docs_cap(self):
        idx = ri.BM25Index(max_docs=2)
        self.assertTrue(idx.add_document("a.txt", "first"))
        self.assertTrue(idx.add_document("b.txt", "second"))
        self.assertFalse(idx.add_document("c.txt", "third"))
        # Replacing an existing doc still works even at cap.
        self.assertTrue(idx.add_document("a.txt", "updated first"))
        self.assertEqual(len(idx), 2)


# ============================================================
# Workspace walker
# ============================================================

class IndexWorkspaceTests(unittest.TestCase):
    def test_indexes_whitelisted_extensions(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "code.py"), "w") as f:
                f.write("def hello(): return 'world'")
            with open(os.path.join(ws, "doc.md"), "w") as f:
                f.write("# Project documentation")
            index, diag = ri.index_workspace(ws)
            self.assertEqual(diag["indexed"], 2)
            self.assertEqual(len(index), 2)

    def test_skips_unknown_extensions(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "image.png"), "wb") as f:
                f.write(b"\x89PNGfake")
            with open(os.path.join(ws, "data.bin"), "wb") as f:
                f.write(b"\x00\x01\x02binarydata")
            index, diag = ri.index_workspace(ws)
            self.assertEqual(diag["indexed"], 0)
            self.assertGreaterEqual(diag["skipped_unknown_ext"], 2)

    def test_excludes_common_build_dirs(self):
        with tempfile.TemporaryDirectory() as ws:
            for excluded in ("node_modules", "__pycache__", ".git", "dist"):
                os.makedirs(os.path.join(ws, excluded))
                with open(os.path.join(ws, excluded, "junk.py"), "w") as f:
                    f.write("def fake(): pass")
            with open(os.path.join(ws, "real.py"), "w") as f:
                f.write("def real(): pass")
            index, _ = ri.index_workspace(ws)
            paths = [d.path for d in index._docs.values()]
            self.assertEqual(paths, ["real.py"])

    def test_excludes_dot_dirs(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, ".hidden"))
            with open(os.path.join(ws, ".hidden", "x.py"), "w") as f:
                f.write("def x(): pass")
            with open(os.path.join(ws, "y.py"), "w") as f:
                f.write("def y(): pass")
            index, _ = ri.index_workspace(ws)
            paths = [d.path for d in index._docs.values()]
            self.assertEqual(paths, ["y.py"])

    def test_skips_oversize_files(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "small.py"), "w") as f:
                f.write("def small(): pass")
            with open(os.path.join(ws, "huge.py"), "w") as f:
                f.write("# huge\n" + "x = 1\n" * 100_000)
            index, diag = ri.index_workspace(ws, max_file_bytes=1024)
            self.assertEqual(diag["indexed"], 1)
            self.assertEqual(diag["skipped_oversize"], 1)

    def test_skips_binary_files_by_null_byte(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "broken.py"), "wb") as f:
                # Has .py extension but null byte = binary.
                f.write(b"def fn(): pass\x00\x01\x02")
            index, diag = ri.index_workspace(ws)
            self.assertEqual(diag["indexed"], 0)
            self.assertEqual(diag["skipped_binary"], 1)

    def test_max_files_caps_walk(self):
        with tempfile.TemporaryDirectory() as ws:
            for i in range(10):
                with open(os.path.join(ws, f"f{i}.py"), "w") as f:
                    f.write(f"def fn{i}(): pass")
            index, diag = ri.index_workspace(ws, max_files=3)
            self.assertEqual(diag["indexed"], 3)
            self.assertTrue(diag["stopped_at_max_files"])

    def test_no_workspace_returns_empty_index(self):
        index, diag = ri.index_workspace("/path/does/not/exist")
        self.assertEqual(len(index), 0)
        self.assertEqual(diag["indexed"], 0)


# ============================================================
# Per-session cache
# ============================================================

class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        # Each test uses a unique session id so the module-level
        # cache from prior tests doesn't leak in.
        self.sid = _unique("retrieval_cache")
        ri.clear(self.sid)

    def tearDown(self):
        ri.clear(self.sid)

    def test_get_or_build_no_workspace_returns_none(self):
        idx, diag = ri.get_or_build(self.sid, workspace_path=None)
        self.assertIsNone(idx)
        self.assertFalse(diag["cached"])
        self.assertEqual(diag["reason"], "no_workspace")

    def test_get_or_build_then_cached(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "a.py"), "w") as f:
                f.write("def a(): pass")
            idx1, diag1 = ri.get_or_build(self.sid, workspace_path=ws)
            self.assertFalse(diag1["cached"])
            idx2, diag2 = ri.get_or_build(self.sid, workspace_path=ws)
            self.assertTrue(diag2["cached"])
            self.assertIs(idx1, idx2)

    def test_rebuild_replaces_cache(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "a.py"), "w") as f:
                f.write("def a(): pass")
            idx1, _ = ri.get_or_build(self.sid, workspace_path=ws)
            idx2, _ = ri.rebuild(self.sid, workspace_path=ws)
            self.assertIsNot(idx1, idx2)

    def test_clear_drops_cache(self):
        with tempfile.TemporaryDirectory() as ws:
            with open(os.path.join(ws, "a.py"), "w") as f:
                f.write("def a(): pass")
            ri.get_or_build(self.sid, workspace_path=ws)
            self.assertTrue(ri.clear(self.sid))
            self.assertFalse(ri.clear(self.sid))  # idempotent

    def test_status_for_uncached(self):
        info = ri.status(self.sid)
        self.assertFalse(info["cached"])
        self.assertEqual(info["doc_count"], 0)


# ============================================================
# Dispatcher
# ============================================================

class RetrieveDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        with open(os.path.join(self.workspace, "auth.py"), "w") as f:
            f.write("def login(user, password):\n    validate_token(password)")
        with open(os.path.join(self.workspace, "db.py"), "w") as f:
            f.write("def query(sql):\n    return execute_database_query(sql)")
        with open(os.path.join(self.workspace, "README.md"), "w") as f:
            f.write("# Auth Service\n\nHandles login + database queries")

    def tearDown(self):
        self.tmp.cleanup()

    def _set_workspace(self, sid):
        local_memory.set_workspace(session_id=sid, workspace_path=self.workspace)

    def test_status_no_workspace_returns_uncached(self):
        sid = _unique("disp_status_no_ws")
        result = dispatch_input("retrieve status", session_id=sid)
        self.assertEqual(result["action"], "retrieve")
        self.assertFalse(result["result"]["cached"])

    def test_query_without_workspace_returns_no_workspace(self):
        sid = _unique("disp_query_no_ws")
        result = dispatch_input("retrieve login", session_id=sid)
        self.assertEqual(result.get("reason"), "no_workspace")

    def test_query_lazy_builds_on_first_call(self):
        sid = _unique("disp_query_lazy")
        self._set_workspace(sid)
        try:
            result = dispatch_input("retrieve login", session_id=sid)
            self.assertEqual(result["action"], "retrieve")
            self.assertNotIn("reason", result)
            self.assertGreater(len(result["result"]["hits"]), 0)
            top = result["result"]["hits"][0]
            self.assertIn(top["path"], ("auth.py", "README.md"))
            self.assertIn("login", top["matched_terms"])
        finally:
            ri.clear(sid)

    def test_build_subcommand(self):
        sid = _unique("disp_build")
        self._set_workspace(sid)
        try:
            result = dispatch_input("retrieve build", session_id=sid)
            self.assertNotIn("reason", result)
            self.assertEqual(result["result"]["doc_count"], 3)
        finally:
            ri.clear(sid)

    def test_build_without_workspace_returns_no_workspace(self):
        sid = _unique("disp_build_no_ws")
        result = dispatch_input("retrieve build", session_id=sid)
        self.assertEqual(result.get("reason"), "no_workspace")

    def test_clear_subcommand(self):
        sid = _unique("disp_clear")
        self._set_workspace(sid)
        try:
            dispatch_input("retrieve build", session_id=sid)
            result = dispatch_input("retrieve clear", session_id=sid)
            self.assertNotIn("reason", result)
            self.assertIn("Cleared", result["output"])
            # Now uncached.
            self.assertFalse(ri.status(sid)["cached"])
        finally:
            ri.clear(sid)

    def test_no_args_shows_status(self):
        sid = _unique("disp_no_args")
        result = dispatch_input("retrieve", session_id=sid)
        self.assertEqual(result["action"], "retrieve")
        self.assertIn("not yet built", result["output"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
