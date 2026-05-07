"""End-to-end tests for `local_memory` over both storage backends.

These exercise the public functions (`append_message`, `set_workspace`,
`append_execution`, etc.) under both the JSON and DuckDB backends and
prove the data-loss fix: 50 threads concurrently appending messages
all survive, where the legacy JSON read-modify-write would have lost
some.
"""
import os
import tempfile
import threading
import unittest

from app.memory import local_memory
from app.memory.duckdb_storage import DuckDbStorage
from app.memory.json_storage import JsonStorage


def _backend(kind, tmp_dir):
    if kind == "json":
        return JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    if kind == "duckdb":
        return DuckDbStorage(path=os.path.join(tmp_dir, "memory.duckdb"))
    raise ValueError(kind)


class _LocalMemoryContractMixin:
    kind = "json"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = _backend(self.kind, self._tmp.name)
        local_memory._reset_storage_for_tests(self._storage)

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        if hasattr(self._storage, "close"):
            self._storage.close()
        self._tmp.cleanup()

    def test_append_message_persists_through_storage(self):
        local_memory.append_message("user", "hello world", session_id="default")
        history = local_memory.get_history(session_id="default")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "hello world")

    def test_set_workspace_round_trips(self):
        local_memory.set_workspace(session_id="default", workspace_path="C:/proj")
        ctx = local_memory.get_session_context(session_id="default")
        self.assertEqual(ctx["workspace_path"], "C:/proj")

    def test_concurrent_appends_all_survive(self):
        # The legacy load-modify-save pattern lost writes here. With the
        # storage.mutate() lock, every append must survive on both backends.
        N = 50

        def writer(i):
            local_memory.append_message("user", f"msg-{i}", session_id="default")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        history = local_memory.get_history(session_id="default")
        contents = sorted(m["content"] for m in history)
        self.assertEqual(len(contents), N, "concurrent appends lost data")
        self.assertEqual(contents, sorted(f"msg-{i}" for i in range(N)))

    def test_concurrent_context_updates_dont_clobber_history(self):
        # Spin up two writers: one appends to history, one repeatedly updates
        # context. The history must keep all messages even though context is
        # being rewritten under it.
        N = 30

        def msg_writer():
            for i in range(N):
                local_memory.append_message("user", f"hist-{i}", session_id="default")

        def ctx_writer():
            for i in range(N):
                local_memory.set_active_skills(session_id="default", skills=[f"skill{i}"])

        t1 = threading.Thread(target=msg_writer)
        t2 = threading.Thread(target=ctx_writer)
        t1.start(); t2.start(); t1.join(); t2.join()

        history = local_memory.get_history(session_id="default")
        self.assertEqual(len(history), N)
        ctx = local_memory.get_session_context(session_id="default")
        self.assertEqual(len(ctx["active_skills"]), 1)  # last write wins, fine
        self.assertTrue(ctx["active_skills"][0].startswith("skill"))


class JsonBackedLocalMemoryTests(_LocalMemoryContractMixin, unittest.TestCase):
    kind = "json"


class DuckDbBackedLocalMemoryTests(_LocalMemoryContractMixin, unittest.TestCase):
    kind = "duckdb"


class DuckDbAutoMigrationTests(unittest.TestCase):
    """The Phase-2 migration: setting MEMORY_BACKEND=duckdb on a system that
    already has a local_memory.json should auto-import that data the first
    time the DB is opened."""

    def test_get_storage_duckdb_imports_legacy_json_when_db_empty(self):
        from app.memory import json_storage as js, storage as st
        with tempfile.TemporaryDirectory() as tmp:
            # Drop a legacy JSON file at the conventional path.
            legacy_path = os.path.join(tmp, "local_memory.json")
            with open(legacy_path, "w", encoding="utf-8") as fh:
                fh.write('{"sessions": {"default": {"history": [{"role":"user","content":"legacy hi"}]}}}')

            # Point the migration at our temp legacy path by patching the
            # module-level _DEFAULT_PATH used inside get_storage().
            saved = js._DEFAULT_PATH
            js._DEFAULT_PATH = legacy_path
            try:
                duck_path = os.path.join(tmp, "fresh.duckdb")
                storage = st.get_storage(backend="duckdb", path=duck_path)
                try:
                    data = storage.read_store()
                    self.assertIn("default", data["sessions"])
                    self.assertEqual(
                        data["sessions"]["default"]["history"][0]["content"],
                        "legacy hi",
                    )
                finally:
                    storage.close()
            finally:
                js._DEFAULT_PATH = saved


if __name__ == "__main__":
    unittest.main()
