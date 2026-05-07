"""Tests for workflow templates Phase 1 (`docs/design/workflow-templates.md`).

Three layers:
  - Parser (TOML + JSON → Template, with shape validation)
  - Parameter substitution (`{{name}}` + `{user.*}` / `{date}` / `{session_id}`)
  - Run flow (each step dispatches; pause-on-approval stops the run)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.memory import local_memory
from app.memory.json_storage import JsonStorage
from app.services import template_service as ts
from app.services.command_dispatcher import dispatch_input


def _isolated_memory(tmp_dir):
    storage = JsonStorage(path=os.path.join(tmp_dir, "memory.json"))
    local_memory._reset_storage_for_tests(storage)
    local_memory._reset_undo_stacks_for_tests()
    return storage


# ============================================================
# Parser
# ============================================================

class TemplateParserTests(unittest.TestCase):
    def test_parse_minimal_template(self):
        data = {
            "id": "minimal",
            "description": "tiny",
            "steps": [
                {"kind": "command", "cmd": "doctor"},
            ],
        }
        tmpl = ts.parse_template(data)
        self.assertEqual(tmpl.id, "minimal")
        self.assertEqual(tmpl.description, "tiny")
        self.assertEqual(len(tmpl.steps), 1)
        self.assertEqual(tmpl.steps[0].cmd, "doctor")

    def test_parse_template_with_params(self):
        data = {
            "id": "with-params",
            "params": [
                {"name": "version", "required": True, "description": "semver"},
                {"name": "author", "default": "anonymous"},
            ],
            "steps": [{"kind": "command", "cmd": "echo {{version}} by {{author}}"}],
        }
        tmpl = ts.parse_template(data)
        self.assertEqual(len(tmpl.params), 2)
        self.assertTrue(tmpl.params[0].required)
        self.assertEqual(tmpl.params[1].default, "anonymous")

    def test_invalid_id_raises(self):
        with self.assertRaises(ts.TemplateError) as ctx:
            ts.parse_template({"id": "Has Spaces", "steps": [{"kind": "command", "cmd": "x"}]})
        self.assertEqual(ctx.exception.reason, "invalid_argument")

    def test_empty_steps_raises(self):
        with self.assertRaises(ts.TemplateError):
            ts.parse_template({"id": "empty", "steps": []})

    def test_unknown_step_kind_raises_template_unknown_step(self):
        with self.assertRaises(ts.TemplateError) as ctx:
            ts.parse_template(
                {"id": "bad", "steps": [{"kind": "wait", "cmd": "ignored"}]}
            )
        self.assertEqual(ctx.exception.reason, "template_unknown_step")

    def test_command_step_without_cmd_raises(self):
        with self.assertRaises(ts.TemplateError):
            ts.parse_template(
                {"id": "no-cmd", "steps": [{"kind": "command"}]}
            )

    def test_duplicate_param_names_raise(self):
        with self.assertRaises(ts.TemplateError):
            ts.parse_template(
                {
                    "id": "dups",
                    "params": [{"name": "x"}, {"name": "x"}],
                    "steps": [{"kind": "command", "cmd": "x"}],
                }
            )

    def test_param_name_must_be_identifier(self):
        with self.assertRaises(ts.TemplateError):
            ts.parse_template(
                {
                    "id": "badname",
                    "params": [{"name": "9has-leading-digit"}],
                    "steps": [{"kind": "command", "cmd": "x"}],
                }
            )

    def test_too_many_steps_raises(self):
        steps = [
            {"kind": "command", "cmd": f"step {i}"}
            for i in range(ts.MAX_STEPS + 1)
        ]
        with self.assertRaises(ts.TemplateError):
            ts.parse_template({"id": "huge", "steps": steps})


class TemplateFileLoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        ts._reset_registry_for_tests()

    def tearDown(self):
        self._tmp.cleanup()
        ts._reset_registry_for_tests()

    def test_load_toml_file(self):
        path = Path(self._tmp.name) / "x.toml"
        path.write_text(
            'id = "from-toml"\n'
            'description = "hi"\n'
            '[[steps]]\n'
            'kind = "command"\n'
            'cmd = "doctor"\n',
            encoding="utf-8",
        )
        tmpl = ts.load_template_file(path)
        self.assertEqual(tmpl.id, "from-toml")
        self.assertEqual(tmpl.steps[0].cmd, "doctor")

    def test_load_json_file(self):
        path = Path(self._tmp.name) / "x.json"
        path.write_text(
            json.dumps(
                {
                    "id": "from-json",
                    "steps": [{"kind": "command", "cmd": "doctor"}],
                }
            ),
            encoding="utf-8",
        )
        tmpl = ts.load_template_file(path)
        self.assertEqual(tmpl.id, "from-json")

    def test_unsupported_extension_raises(self):
        path = Path(self._tmp.name) / "x.yaml"
        path.write_text("id: nope", encoding="utf-8")
        with self.assertRaises(ts.TemplateError) as ctx:
            ts.load_template_file(path)
        self.assertEqual(ctx.exception.reason, "invalid_argument")

    def test_malformed_toml_raises(self):
        path = Path(self._tmp.name) / "x.toml"
        path.write_text("=== this is not toml ===", encoding="utf-8")
        with self.assertRaises(ts.TemplateError) as ctx:
            ts.load_template_file(path)
        self.assertEqual(ctx.exception.reason, "invalid_argument")

    def test_reload_skips_bad_files_keeps_good_ones(self):
        good = Path(self._tmp.name) / "good.toml"
        good.write_text(
            'id = "good"\n[[steps]]\nkind = "command"\ncmd = "doctor"\n',
            encoding="utf-8",
        )
        bad = Path(self._tmp.name) / "bad.toml"
        bad.write_text("not = 'a template'\nid = ''\n", encoding="utf-8")
        registry = ts.reload_templates(templates_dir=Path(self._tmp.name))
        self.assertIn("good", registry)
        self.assertNotIn("", registry)

    def test_reload_with_no_directory(self):
        # Point at a directory that doesn't exist — should return empty,
        # not raise.
        registry = ts.reload_templates(
            templates_dir=Path(self._tmp.name) / "does-not-exist"
        )
        self.assertEqual(registry, {})


# ============================================================
# Parameter substitution
# ============================================================

class TemplateSubstitutionTests(unittest.TestCase):
    def test_substitute_param_reference(self):
        result = ts.substitute("hello {{name}}", {"name": "world"})
        self.assertEqual(result, "hello world")

    def test_substitute_missing_param_raises(self):
        with self.assertRaises(ts.TemplateError) as ctx:
            ts.substitute("hello {{name}}", {})
        self.assertEqual(ctx.exception.reason, "template_param_missing")

    def test_substitute_session_id_builtin(self):
        result = ts.substitute("on session {session_id}", {}, session_id="my-session")
        self.assertEqual(result, "on session my-session")

    def test_substitute_date_builtin(self):
        result = ts.substitute("today: {date}", {})
        self.assertEqual(result, f"today: {_dt.date.today().isoformat()}")

    def test_substitute_mixed_param_and_builtin(self):
        result = ts.substitute(
            "{{tag}} on {date} for {session_id}",
            {"tag": "release-0.5"},
            session_id="default",
        )
        self.assertIn("release-0.5", result)
        self.assertIn(_dt.date.today().isoformat(), result)
        self.assertIn("default", result)

    def test_substitute_multiple_param_refs(self):
        result = ts.substitute(
            "{{a}} and {{b}} and {{a}}",
            {"a": "X", "b": "Y"},
        )
        self.assertEqual(result, "X and Y and X")

    def test_resolve_params_required_missing_raises(self):
        tmpl = ts.parse_template(
            {
                "id": "needs-version",
                "params": [{"name": "version", "required": True}],
                "steps": [{"kind": "command", "cmd": "echo {{version}}"}],
            }
        )
        with self.assertRaises(ts.TemplateError) as ctx:
            ts.resolve_params(tmpl, {})
        self.assertEqual(ctx.exception.reason, "template_param_missing")

    def test_resolve_params_uses_default_when_missing(self):
        tmpl = ts.parse_template(
            {
                "id": "with-default",
                "params": [{"name": "author", "default": "anon"}],
                "steps": [{"kind": "command", "cmd": "echo {{author}}"}],
            }
        )
        merged = ts.resolve_params(tmpl, {})
        self.assertEqual(merged["author"], "anon")

    def test_resolve_params_explicit_overrides_default(self):
        tmpl = ts.parse_template(
            {
                "id": "ov",
                "params": [{"name": "x", "default": "d"}],
                "steps": [{"kind": "command", "cmd": "x"}],
            }
        )
        merged = ts.resolve_params(tmpl, {"x": "actual"})
        self.assertEqual(merged["x"], "actual")


# ============================================================
# run_template
# ============================================================

class TemplateRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        ts._reset_registry_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        ts._reset_registry_for_tests()

    def test_run_dispatches_each_step(self):
        tmpl = ts.parse_template(
            {
                "id": "two-step",
                "steps": [
                    {"kind": "command", "cmd": "doctor"},
                    {"kind": "command", "cmd": "tasks"},
                ],
            }
        )

        captured_calls = []

        def fake_dispatch(text, session_id):
            captured_calls.append((text, session_id))
            return {"action": "fake", "session_id": session_id, "output": f"ran {text}"}

        summary = ts.run_template(tmpl, {}, "default", dispatch=fake_dispatch)

        self.assertEqual(len(captured_calls), 2)
        self.assertEqual(captured_calls[0], ("doctor", "default"))
        self.assertEqual(captured_calls[1], ("tasks", "default"))
        self.assertEqual(summary["stopped_reason"], "completed")
        self.assertTrue(summary["completed"])

    def test_run_substitutes_params_before_dispatch(self):
        tmpl = ts.parse_template(
            {
                "id": "with-param",
                "params": [{"name": "version", "required": True}],
                "steps": [{"kind": "command", "cmd": "echo v={{version}}"}],
            }
        )
        captured = []

        def fake_dispatch(text, session_id):
            captured.append(text)
            return {"action": "fake", "session_id": session_id, "output": "ok"}

        ts.run_template(tmpl, {"version": "0.5.2"}, "s", dispatch=fake_dispatch)
        self.assertEqual(captured, ["echo v=0.5.2"])

    def test_run_stops_on_approval_required(self):
        tmpl = ts.parse_template(
            {
                "id": "pauses",
                "steps": [
                    {"kind": "command", "cmd": "step-one"},
                    {"kind": "command", "cmd": "step-two-pauses"},
                    {"kind": "command", "cmd": "step-three-never-runs"},
                ],
            }
        )

        def fake_dispatch(text, session_id):
            if "pauses" in text:
                return {
                    "action": "fake",
                    "session_id": session_id,
                    "output": "needs approve",
                    "reason": "approval_required",
                }
            return {"action": "fake", "session_id": session_id, "output": "ok"}

        summary = ts.run_template(tmpl, {}, "s", dispatch=fake_dispatch)
        self.assertEqual(summary["stopped_reason"], "paused")
        self.assertFalse(summary["completed"])
        self.assertEqual(len(summary["steps"]), 2)  # third step skipped

    def test_run_stops_on_required_step_failure(self):
        tmpl = ts.parse_template(
            {
                "id": "must-pass",
                "steps": [
                    {"kind": "command", "cmd": "step1", "require_success": True},
                    {"kind": "command", "cmd": "step2-never-runs"},
                ],
            }
        )

        def fake_dispatch(text, session_id):
            return {
                "action": "fake",
                "session_id": session_id,
                "output": "boom",
                "reason": "internal_error",
            }

        summary = ts.run_template(tmpl, {}, "s", dispatch=fake_dispatch)
        self.assertEqual(summary["stopped_reason"], "step_failed")
        self.assertEqual(len(summary["steps"]), 1)

    def test_run_continues_past_non_required_failure(self):
        tmpl = ts.parse_template(
            {
                "id": "tolerant",
                "steps": [
                    {"kind": "command", "cmd": "step1"},  # no require_success
                    {"kind": "command", "cmd": "step2"},
                ],
            }
        )

        call_count = {"n": 0}

        def fake_dispatch(text, session_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"action": "fake", "output": "fail", "reason": "internal_error"}
            return {"action": "fake", "output": "ok"}

        summary = ts.run_template(tmpl, {}, "s", dispatch=fake_dispatch)
        self.assertEqual(summary["stopped_reason"], "completed")
        self.assertEqual(len(summary["steps"]), 2)


# ============================================================
# Dispatcher integration
# ============================================================

class TemplateDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _isolated_memory(self._tmp.name)
        ts._reset_registry_for_tests()

    def tearDown(self):
        local_memory._reset_storage_for_tests(None)
        self._tmp.cleanup()
        ts._reset_registry_for_tests()

    def test_template_list_with_no_templates(self):
        ev = dispatch_input("template list", session_id="default")
        self.assertIn("No templates", ev["output"])

    def test_template_show_unknown_emits_unknown_target(self):
        ts.register_template_for_tests(
            ts.parse_template(
                {"id": "exists", "steps": [{"kind": "command", "cmd": "x"}]}
            )
        )
        ev = dispatch_input("template show ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_template_run_unknown_emits_unknown_target(self):
        ev = dispatch_input("template run ghost", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_template_run_with_required_param_missing(self):
        ts.register_template_for_tests(
            ts.parse_template(
                {
                    "id": "needs-x",
                    "params": [{"name": "x", "required": True}],
                    "steps": [{"kind": "command", "cmd": "echo {{x}}"}],
                }
            )
        )
        ev = dispatch_input("template run needs-x", session_id="default")
        self.assertEqual(ev.get("reason"), "template_param_missing")

    def test_template_run_full_loop(self):
        ts.register_template_for_tests(
            ts.parse_template(
                {
                    "id": "demo",
                    "params": [{"name": "msg", "default": "hi"}],
                    "steps": [
                        # `notes` is a real handler, no mocking needed.
                        {"kind": "command", "cmd": "notes append {{msg}} from template"},
                    ],
                }
            )
        )
        ev = dispatch_input("template run demo msg=hello", session_id="default")
        self.assertNotIn("reason", ev)
        # The note got appended.
        notes = local_memory.get_notes(session_id="default")
        self.assertIn("hello from template", notes)

    def test_template_show_unknown_id_emits_unknown_target(self):
        ev = dispatch_input("template show does_not_exist", session_id="default")
        self.assertEqual(ev.get("reason"), "unknown_target")

    def test_template_run_bad_param_syntax(self):
        ts.register_template_for_tests(
            ts.parse_template(
                {
                    "id": "x",
                    "steps": [{"kind": "command", "cmd": "doctor"}],
                }
            )
        )
        ev = dispatch_input("template run x not-key-equals-value", session_id="default")
        self.assertEqual(ev.get("reason"), "invalid_argument")


if __name__ == "__main__":
    unittest.main()
