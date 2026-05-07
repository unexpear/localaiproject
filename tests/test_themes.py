import json
import os
import tempfile
import unittest

from app.ui import themes


class ThemeStateRoundTripTests(unittest.TestCase):
    def test_load_returns_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "themes.json")
            state = themes.load_state(path)
        self.assertEqual(state["active"], themes.DEFAULT_ACTIVE)
        for name in themes.DEFAULT_PRESETS:
            self.assertIn(name, state["presets"])

    def test_save_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "themes.json")
            base = themes.load_state(path)
            updated = themes.upsert_preset(
                base,
                "MyDark",
                {"kind": "qdarkstyle", "options": {"palette": "dark"}},
            )
            updated = themes.set_active(updated, "MyDark")
            themes.save_state(updated, path)

            reloaded = themes.load_state(path)
            self.assertEqual(reloaded["active"], "MyDark")
            self.assertEqual(reloaded["presets"]["MyDark"]["kind"], "qdarkstyle")
            self.assertEqual(reloaded["presets"]["MyDark"]["options"]["palette"], "dark")

    def test_corrupt_json_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "themes.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            state = themes.load_state(path)
        self.assertEqual(state["active"], themes.DEFAULT_ACTIVE)


class ThemeMutationTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "active": themes.DEFAULT_ACTIVE,
            "presets": dict(themes.DEFAULT_PRESETS),
        }

    def test_upsert_preset_normalizes_options(self):
        updated = themes.upsert_preset(
            self.state,
            "Custom",
            {"kind": "qss", "options": {"qss": "QWidget { color: red; }", "junk": "ignored"}},
        )
        self.assertIn("Custom", updated["presets"])
        self.assertEqual(updated["presets"]["Custom"]["options"], {"qss": "QWidget { color: red; }"})

    def test_upsert_rejects_invalid_kind(self):
        with self.assertRaises(ValueError):
            themes.upsert_preset(self.state, "Bad", {"kind": "magic", "options": {}})

    def test_upsert_rejects_qt_material_without_theme(self):
        with self.assertRaises(ValueError):
            themes.upsert_preset(self.state, "Bad", {"kind": "qt_material", "options": {}})

    def test_upsert_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            themes.upsert_preset(self.state, "  ", {"kind": "default", "options": {}})

    def test_delete_preset_removes_user_preset(self):
        seeded = themes.upsert_preset(
            self.state,
            "ToRemove",
            {"kind": "default", "options": {}},
        )
        cleaned = themes.delete_preset(seeded, "ToRemove")
        self.assertNotIn("ToRemove", cleaned["presets"])

    def test_delete_builtin_preset_raises(self):
        with self.assertRaises(ValueError):
            themes.delete_preset(self.state, themes.DEFAULT_ACTIVE)

    def test_delete_active_preset_resets_active_to_default(self):
        seeded = themes.upsert_preset(
            self.state,
            "Temp",
            {"kind": "default", "options": {}},
        )
        seeded = themes.set_active(seeded, "Temp")
        cleaned = themes.delete_preset(seeded, "Temp")
        self.assertEqual(cleaned["active"], themes.DEFAULT_ACTIVE)

    def test_set_active_unknown_preset_raises(self):
        with self.assertRaises(ValueError):
            themes.set_active(self.state, "Nonexistent")

    def test_get_active_preset_returns_name_and_payload(self):
        name, preset = themes.get_active_preset(self.state)
        self.assertEqual(name, themes.DEFAULT_ACTIVE)
        self.assertEqual(preset["kind"], "default")

    def test_get_active_falls_back_when_active_missing(self):
        broken = {"active": "Ghost", "presets": dict(themes.DEFAULT_PRESETS)}
        name, _ = themes.get_active_preset(broken)
        self.assertEqual(name, themes.DEFAULT_ACTIVE)


class QssFileKindTests(unittest.TestCase):
    def test_qss_file_normalizes_relative_path(self):
        state = {"active": "Default", "presets": dict(themes.DEFAULT_PRESETS)}
        updated = themes.upsert_preset(
            state,
            "Foundry Custom",
            {"kind": "qss_file", "options": {"path": "foundry.qss"}},
        )
        self.assertEqual(updated["presets"]["Foundry Custom"]["kind"], "qss_file")
        self.assertEqual(updated["presets"]["Foundry Custom"]["options"]["path"], "foundry.qss")

    def test_qss_file_rejects_path_traversal(self):
        state = {"active": "Default", "presets": dict(themes.DEFAULT_PRESETS)}
        with self.assertRaises(ValueError):
            themes.upsert_preset(
                state,
                "Bad",
                {"kind": "qss_file", "options": {"path": "../../etc/passwd.qss"}},
            )

    def test_qss_file_requires_qss_extension(self):
        state = {"active": "Default", "presets": dict(themes.DEFAULT_PRESETS)}
        with self.assertRaises(ValueError):
            themes.upsert_preset(
                state,
                "Bad",
                {"kind": "qss_file", "options": {"path": "secret.txt"}},
            )

    def test_qss_file_requires_non_empty_path(self):
        state = {"active": "Default", "presets": dict(themes.DEFAULT_PRESETS)}
        with self.assertRaises(ValueError):
            themes.upsert_preset(
                state,
                "Bad",
                {"kind": "qss_file", "options": {"path": ""}},
            )

    def test_foundry_is_a_default_preset(self):
        self.assertIn("Foundry", themes.DEFAULT_PRESETS)
        self.assertEqual(themes.DEFAULT_PRESETS["Foundry"]["kind"], "qss_file")
        # And the file actually exists in the bundled QSS dir
        full = os.path.join(themes.QSS_DIR, themes.DEFAULT_PRESETS["Foundry"]["options"]["path"])
        self.assertTrue(os.path.exists(full), f"missing bundled QSS file: {full}")


class RenamePresetTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "active": themes.DEFAULT_ACTIVE,
            "presets": dict(themes.DEFAULT_PRESETS),
        }
        # Seed a personal preset
        self.state = themes.upsert_preset(
            self.state, "MyDark", {"kind": "qdarkstyle", "options": {"palette": "dark"}}
        )

    def test_rename_personal_preset_succeeds(self):
        renamed = themes.rename_preset(self.state, "MyDark", "Midnight")
        self.assertNotIn("MyDark", renamed["presets"])
        self.assertIn("Midnight", renamed["presets"])
        self.assertEqual(renamed["presets"]["Midnight"]["kind"], "qdarkstyle")

    def test_rename_builtin_raises(self):
        with self.assertRaises(ValueError):
            themes.rename_preset(self.state, "Default", "MyDefault")

    def test_rename_to_builtin_collision_raises(self):
        with self.assertRaises(ValueError):
            themes.rename_preset(self.state, "MyDark", "Default")

    def test_rename_to_existing_name_raises(self):
        self.state = themes.upsert_preset(
            self.state, "MyOther", {"kind": "default", "options": {}}
        )
        with self.assertRaises(ValueError):
            themes.rename_preset(self.state, "MyDark", "MyOther")

    def test_rename_empty_name_raises(self):
        with self.assertRaises(ValueError):
            themes.rename_preset(self.state, "MyDark", "   ")

    def test_rename_unknown_preset_raises(self):
        with self.assertRaises(ValueError):
            themes.rename_preset(self.state, "Ghost", "Apparition")

    def test_rename_active_follows(self):
        active_state = themes.set_active(self.state, "MyDark")
        renamed = themes.rename_preset(active_state, "MyDark", "Midnight")
        self.assertEqual(renamed["active"], "Midnight")

    def test_rename_to_same_name_is_noop(self):
        renamed = themes.rename_preset(self.state, "MyDark", "MyDark")
        self.assertIn("MyDark", renamed["presets"])


class ReadQssTests(unittest.TestCase):
    def test_read_qss_returns_string_for_qss_kind(self):
        preset = {"kind": "qss", "options": {"qss": "QWidget { color: red; }"}}
        self.assertEqual(themes.read_qss(preset), "QWidget { color: red; }")

    def test_read_qss_returns_file_contents_for_qss_file_kind(self):
        preset = themes.DEFAULT_PRESETS["Foundry"]
        contents = themes.read_qss(preset)
        self.assertIsNotNone(contents)
        self.assertIn("FOUNDRY", contents)
        self.assertIn("#1A1C1F", contents)

    def test_read_qss_returns_none_for_default_kind(self):
        self.assertIsNone(themes.read_qss({"kind": "default", "options": {}}))

    def test_read_qss_returns_none_for_qt_material_kind(self):
        self.assertIsNone(
            themes.read_qss({"kind": "qt_material", "options": {"theme": "dark_teal.xml"}})
        )

    def test_read_qss_missing_file_returns_none(self):
        self.assertIsNone(
            themes.read_qss({"kind": "qss_file", "options": {"path": "definitely-not-real.qss"}})
        )


class ImportQssFromFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qss_dir_backup = themes.QSS_DIR
        # Sandbox QSS_DIR to a temp dir for these tests so we don't pollute the bundled themes.
        themes.QSS_DIR = os.path.join(self.tmp.name, "themes_qss")
        os.makedirs(themes.QSS_DIR, exist_ok=True)
        self.state = {
            "active": themes.DEFAULT_ACTIVE,
            "presets": dict(themes.DEFAULT_PRESETS),
        }
        # Seed a source .qss in another temp dir to import from.
        self.source = os.path.join(self.tmp.name, "external.qss")
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write("QWidget { background: #112233; }")

    def tearDown(self):
        themes.QSS_DIR = self.qss_dir_backup
        self.tmp.cleanup()

    def test_imports_file_and_creates_preset(self):
        updated = themes.import_qss_from_file(self.state, self.source, "Imported Theme")
        self.assertIn("Imported Theme", updated["presets"])
        self.assertEqual(updated["presets"]["Imported Theme"]["kind"], "qss_file")
        copied = os.path.join(themes.QSS_DIR, updated["presets"]["Imported Theme"]["options"]["path"])
        self.assertTrue(os.path.exists(copied))
        with open(copied, "r", encoding="utf-8") as fh:
            self.assertIn("#112233", fh.read())

    def test_rejects_non_qss_extension(self):
        bad = os.path.join(self.tmp.name, "data.txt")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("not stylesheet")
        with self.assertRaises(ValueError):
            themes.import_qss_from_file(self.state, bad, "X")

    def test_rejects_missing_file(self):
        with self.assertRaises(ValueError):
            themes.import_qss_from_file(self.state, "/no/such/file.qss", "X")

    def test_rejects_builtin_name_collision(self):
        with self.assertRaises(ValueError):
            themes.import_qss_from_file(self.state, self.source, "Foundry")

    def test_reimport_same_name_same_content_reuses_file(self):
        first = themes.import_qss_from_file(self.state, self.source, "First")
        # Re-import with the same name and identical content — should reuse the existing file.
        again = themes.import_qss_from_file(first, self.source, "First")
        p1 = first["presets"]["First"]["options"]["path"]
        p2 = again["presets"]["First"]["options"]["path"]
        self.assertEqual(p1, p2)

    def test_reimport_same_name_different_content_creates_new_file(self):
        first = themes.import_qss_from_file(self.state, self.source, "First")
        # Modify source contents and re-import under the same preset name.
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write("QWidget { background: #ffeecc; }")
        second = themes.import_qss_from_file(first, self.source, "First")
        # New file path should be distinct so the old preset (if anyone referenced it) still resolves.
        p1 = first["presets"]["First"]["options"]["path"]
        p2 = second["presets"]["First"]["options"]["path"]
        self.assertNotEqual(p1, p2)
        # And the new file actually got written
        self.assertTrue(os.path.exists(os.path.join(themes.QSS_DIR, p2)))


class TypographyTests(unittest.TestCase):
    def test_default_typography_is_native(self):
        state = themes._empty_state()
        typo = themes.get_typography(state)
        self.assertEqual(typo["ui_family"], "")
        self.assertEqual(typo["base_size_pt"], 0)

    def test_set_typography_persists_through_save_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "themes.json")
            base = themes.load_state(path)
            updated = themes.set_typography(base, ui_family="Inter", base_size_pt=12)
            themes.save_state(updated, path)

            reloaded = themes.load_state(path)
            typo = themes.get_typography(reloaded)
            self.assertEqual(typo["ui_family"], "Inter")
            self.assertEqual(typo["base_size_pt"], 12)

    def test_set_typography_clamps_size_to_range(self):
        state = themes._empty_state()
        lo, hi = themes.BASE_SIZE_RANGE
        too_small = themes.set_typography(state, base_size_pt=2)
        self.assertEqual(too_small["typography"]["base_size_pt"], lo)
        too_big = themes.set_typography(state, base_size_pt=72)
        self.assertEqual(too_big["typography"]["base_size_pt"], hi)

    def test_set_typography_zero_or_negative_means_no_override(self):
        state = themes._empty_state()
        cleared = themes.set_typography(state, ui_family="", base_size_pt=0)
        self.assertEqual(cleared["typography"]["ui_family"], "")
        self.assertEqual(cleared["typography"]["base_size_pt"], 0)
        cleared2 = themes.set_typography(state, ui_family="Inter", base_size_pt=-1)
        self.assertEqual(cleared2["typography"]["base_size_pt"], 0)

    def test_typography_qss_returns_empty_string_when_no_overrides(self):
        self.assertEqual(themes._typography_qss(None), "")
        self.assertEqual(themes._typography_qss({}), "")
        self.assertEqual(themes._typography_qss({"ui_family": "", "base_size_pt": 0}), "")

    def test_typography_qss_includes_family_and_size(self):
        snippet = themes._typography_qss({"ui_family": "Inter", "base_size_pt": 12})
        self.assertIn('"Inter"', snippet)
        self.assertIn("12pt", snippet)
        self.assertIn("QWidget", snippet)


class ThemeSanitizationTests(unittest.TestCase):
    def test_loaded_state_drops_invalid_user_presets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "themes.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "active": "Default",
                        "presets": {
                            "Bad1": {"kind": "magic", "options": {}},
                            "Bad2": {"kind": "qt_material", "options": {}},  # no theme
                            "Good": {"kind": "qss", "options": {"qss": "QWidget {}"}},
                        },
                    },
                    fh,
                )
            state = themes.load_state(path)
        self.assertNotIn("Bad1", state["presets"])
        self.assertNotIn("Bad2", state["presets"])
        self.assertIn("Good", state["presets"])
        # Built-ins still merged in.
        self.assertIn(themes.DEFAULT_ACTIVE, state["presets"])


if __name__ == "__main__":
    unittest.main()
