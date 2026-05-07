"""Tests for the project_context_service file readers (Phase 3 of
``docs/design/prompt-assembly.md``).

Covers:
* missing workspace_path → None
* missing files → None
* binary file detection (null bytes early in stream)
* over-cap files truncated with marker
* case-variant filenames (PERSONA.md / Persona.md / persona.md)
* malformed UTF-8 → graceful fallback
"""
import os
import tempfile
import unittest

from app.services import project_context_service as pcs


class FindFirstExistingTests(unittest.TestCase):
    def test_none_when_workspace_path_is_falsy(self):
        self.assertIsNone(pcs._find_first_existing(None, ("PERSONA.md",)))
        self.assertIsNone(pcs._find_first_existing("", ("PERSONA.md",)))

    def test_none_when_workspace_dir_does_not_exist(self):
        self.assertIsNone(pcs._find_first_existing(
            os.path.join(tempfile.gettempdir(), "definitely-does-not-exist-xyz"),
            ("PERSONA.md",),
        ))

    def test_returns_first_match_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create the lowercase variant only
            path = os.path.join(tmp, "persona.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("hello")
            found = pcs._find_first_existing(tmp, pcs.PERSONA_FILENAMES)
            # Some platforms (Windows) treat all three names as the
            # same file; either way we should find *something*.
            self.assertIsNotNone(found)
            self.assertTrue(os.path.isfile(found))


class ReadPersonaTests(unittest.TestCase):
    def test_returns_none_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pcs.read_persona(tmp))

    def test_returns_none_when_workspace_path_is_none(self):
        self.assertIsNone(pcs.read_persona(None))
        self.assertIsNone(pcs.read_persona(""))

    def test_returns_content_when_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "PERSONA.md"), "w", encoding="utf-8") as fh:
                fh.write("# Persona\n\nBe concise.")
            content = pcs.read_persona(tmp)
            self.assertIsNotNone(content)
            self.assertIn("Be concise", content)

    def test_returns_none_for_binary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "PERSONA.md")
            with open(path, "wb") as fh:
                # Put a null byte early; that's our binary signal.
                fh.write(b"hello\x00world\n")
            self.assertIsNone(pcs.read_persona(tmp))

    def test_truncates_over_cap_with_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "PERSONA.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x" * (pcs.MAX_PROJECT_FILE_BYTES + 1000))
            content = pcs.read_persona(tmp)
            self.assertIsNotNone(content)
            self.assertIn("file truncated", content)
            self.assertLess(len(content), pcs.MAX_PROJECT_FILE_BYTES + 200)

    def test_handles_invalid_utf8_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "PERSONA.md")
            with open(path, "wb") as fh:
                fh.write(b"valid prefix \xff\xfe broken")
            content = pcs.read_persona(tmp)
            self.assertIsNotNone(content)
            self.assertIn("valid prefix", content)


class ReadProjectAgentsTests(unittest.TestCase):
    def test_returns_content_when_AGENTS_md_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as fh:
                fh.write("Project: foo\nConventions: bar")
            content = pcs.read_project_agents(tmp)
            self.assertIsNotNone(content)
            self.assertIn("Project: foo", content)
            self.assertIn("Conventions: bar", content)

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pcs.read_project_agents(tmp))

    def test_persona_md_does_not_satisfy_agents_lookup(self):
        # Two distinct files, two distinct producers.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "PERSONA.md"), "w", encoding="utf-8") as fh:
                fh.write("persona content")
            self.assertIsNotNone(pcs.read_persona(tmp))
            self.assertIsNone(pcs.read_project_agents(tmp))

    def test_both_files_read_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "PERSONA.md"), "w", encoding="utf-8") as fh:
                fh.write("persona content")
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as fh:
                fh.write("project content")
            self.assertIn("persona content", pcs.read_persona(tmp))
            self.assertIn("project content", pcs.read_project_agents(tmp))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
