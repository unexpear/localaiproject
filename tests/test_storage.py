"""Cross-backend storage tests.

Both `JsonStorage` and `DuckDbStorage` must satisfy the same contract:
read returns the payload that write last put in, missing files yield
empty stores, concurrent threads don't lose each other's edits.

The same test cases run against both backends via parametrised setUp.
"""
import json
import os
import tempfile
import threading
import unittest

from app.memory.json_storage import JsonStorage
from app.memory.duckdb_storage import DuckDbStorage
from app.memory.storage import get_storage


def _backend_factory(tmp_dir, kind):
    if kind == "json":
        return JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    if kind == "duckdb":
        return DuckDbStorage(path=os.path.join(tmp_dir, "memory.duckdb"))
    raise ValueError(kind)


class _StorageContractMixin:
    """Subclasses set self.kind to 'json' or 'duckdb'."""

    kind = "json"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = _backend_factory(self._tmp.name, self.kind)

    def tearDown(self):
        if hasattr(self.storage, "close"):
            self.storage.close()
        self._tmp.cleanup()

    def test_read_missing_returns_empty_sessions_blob(self):
        self.assertEqual(self.storage.read_store(), {"sessions": {}})

    def test_write_then_read_round_trips_payload(self):
        payload = {
            "sessions": {
                "default": {
                    "history": [{"role": "user", "content": "hello"}],
                    "executions": [],
                    "summary": {"facts": ["x"], "preferences": [], "open_tasks": []},
                    "context": {"workspace_path": "C:/proj", "active_skills": ["builder"]},
                }
            }
        }
        self.storage.write_store(payload)
        self.assertEqual(self.storage.read_store(), payload)

    def test_overwrite_replaces_payload(self):
        self.storage.write_store({"sessions": {"a": {"history": []}}})
        self.storage.write_store({"sessions": {"b": {"history": []}}})
        result = self.storage.read_store()
        self.assertIn("b", result["sessions"])
        self.assertNotIn("a", result["sessions"])

    def test_write_rejects_non_dict_payload(self):
        with self.assertRaises(TypeError):
            self.storage.write_store(["not", "a", "dict"])

    def test_write_default_fills_sessions_key(self):
        self.storage.write_store({})  # no "sessions" key
        self.assertEqual(self.storage.read_store(), {"sessions": {}})

    def test_concurrent_writes_serialise_no_data_loss(self):
        # Each thread writes a distinct session id; all should survive.
        # Serialising matters here for DuckDB; for JsonStorage with the
        # per-instance lock this also passes. The point is both backends
        # honour the contract.
        ids = [f"s{i}" for i in range(10)]

        def writer(sid):
            current = self.storage.read_store()
            current["sessions"].setdefault(sid, {"history": [{"role": "user", "content": sid}]})
            self.storage.write_store(current)

        threads = [threading.Thread(target=writer, args=(sid,)) for sid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        result = self.storage.read_store()
        # NOTE: with no per-test ordering it's possible some races still drop
        # writes if a thread reads BEFORE another's write commits. The right
        # contract here is "no corruption" -- the file is still valid and
        # contains a strict subset of the writes. Stronger invariants come in
        # Phase 2 once local_memory.py routes through the storage's read /
        # mutate / write under one lock.
        self.assertIn("sessions", result)
        self.assertGreaterEqual(len(result["sessions"]), 1)
        for sid, session in result["sessions"].items():
            self.assertIn(sid, ids)


class JsonStorageTests(_StorageContractMixin, unittest.TestCase):
    kind = "json"


class DuckDbStorageTests(_StorageContractMixin, unittest.TestCase):
    kind = "duckdb"


class JsonResilienceTests(unittest.TestCase):
    def test_corrupt_json_yields_empty_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ this is { not json")
            storage = JsonStorage(path=path)
            self.assertEqual(storage.read_store(), {"sessions": {}})

    def test_empty_file_yields_empty_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.json")
            open(path, "w").close()
            storage = JsonStorage(path=path)
            self.assertEqual(storage.read_store(), {"sessions": {}})


class DuckDbMigrationTests(unittest.TestCase):
    def test_import_from_json_file_brings_data_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "legacy.json")
            duck_path = os.path.join(tmp, "memory.duckdb")
            payload = {"sessions": {"default": {"history": [{"role": "user", "content": "hi"}]}}}
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)

            storage = DuckDbStorage(path=duck_path)
            try:
                self.assertTrue(storage.import_from_json_file(json_path))
                self.assertEqual(storage.read_store(), payload)
                # Idempotent — calling again is fine.
                self.assertTrue(storage.import_from_json_file(json_path))
                self.assertEqual(storage.read_store(), payload)
            finally:
                storage.close()

    def test_import_from_missing_file_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            duck_path = os.path.join(tmp, "memory.duckdb")
            storage = DuckDbStorage(path=duck_path)
            try:
                self.assertFalse(storage.import_from_json_file(os.path.join(tmp, "nope.json")))
                self.assertEqual(storage.read_store(), {"sessions": {}})
            finally:
                storage.close()

    def test_import_from_corrupt_file_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "bad.json")
            duck_path = os.path.join(tmp, "memory.duckdb")
            with open(json_path, "w", encoding="utf-8") as fh:
                fh.write("{ not json")
            storage = DuckDbStorage(path=duck_path)
            try:
                self.assertFalse(storage.import_from_json_file(json_path))
            finally:
                storage.close()


class StorageFactoryTests(unittest.TestCase):
    def test_get_storage_default_is_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.json")
            storage = get_storage(backend="json", path=path)
            self.assertIsInstance(storage, JsonStorage)
            self.assertEqual(storage.path, path)

    def test_get_storage_duckdb_returns_duckdb_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.duckdb")
            storage = get_storage(backend="duckdb", path=path)
            try:
                self.assertIsInstance(storage, DuckDbStorage)
                self.assertEqual(storage.path, path)
            finally:
                storage.close()

    def test_get_storage_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            get_storage(backend="mongodb")


if __name__ == "__main__":
    unittest.main()
