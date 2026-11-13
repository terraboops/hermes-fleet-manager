"""Provider-aware registration and startup handling for fleet_reg.spawn."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet_reg


class FleetRegCodexSpawnTests(unittest.TestCase):
    def test_existing_list_command_needs_no_provider_flags(self):
        with mock.patch.object(fleet_reg, "load", return_value={"sessions": []}), \
             mock.patch.object(sys, "argv", ["fleet_reg.py", "list"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                fleet_reg.main()

    def _run_spawn(self, pane, rollout):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        profile_home = root / "profile"
        codex_home = root / "codex"
        cwd = root / "project"
        profile_home.mkdir()
        codex_home.mkdir()
        cwd.mkdir()
        saved = []
        calls = []
        launches = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if len(argv) > 1 and argv[1] == "capture-pane":
                return SimpleNamespace(stdout=pane, returncode=0)
            return SimpleNamespace(stdout="", returncode=0)

        def save(registry):
            saved.append(copy.deepcopy(registry))

        def shell_line(entry):
            launches.append(copy.deepcopy(entry))
            return "codex-test-command"

        argv = ["fleet_reg.py", "spawn", "cc-w-vibes-codex-copy",
                "--short", "vibescopy", "--profile", "work",
                "--cwd", str(cwd), "--provider", "codex",
                "--codex-home", str(codex_home)]
        with mock.patch.object(fleet_reg, "CFG", {"work": str(profile_home)}), \
             mock.patch.object(fleet_reg, "load", return_value={"sessions": []}), \
             mock.patch.object(fleet_reg, "save", side_effect=save), \
             mock.patch.object(fleet_reg, "transcript_path", return_value=str(codex_home / "rollout.jsonl")), \
             mock.patch.object(fleet_reg, "_discover_codex_rollout", return_value=rollout), \
             mock.patch.object(fleet_reg._harness, "shell_line", side_effect=shell_line), \
             mock.patch.object(fleet_reg.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(fleet_reg.time, "sleep"), \
             mock.patch.object(sys, "argv", argv):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                try:
                    fleet_reg.main()
                except SystemExit as exc:
                    return exc.code, output.getvalue(), saved, calls, launches
        return 0, output.getvalue(), saved, calls, launches

    def test_spawn_accepts_explicit_codex_provider_and_registers_native_home(self):
        uuid = "01a17a5a-100f-78f3-92f4-65802cee3c72"
        code, output, saved, calls, launches = self._run_spawn(
            "› Ask Codex to do anything\nGPT-6-Luna default", uuid)

        self.assertEqual(code, 0, output)
        self.assertEqual(len(saved), 1)
        entry = saved[0]["sessions"][0]
        self.assertEqual(entry["provider"], "codex")
        self.assertEqual(entry["command"], "codex")
        self.assertEqual(entry["resume_flag"], ["resume"])
        self.assertEqual(entry["codex_home"], launches[0]["codex_home"])
        self.assertTrue(entry["codex_auto_trust_project"])
        self.assertEqual(entry["uuid"], uuid)
        self.assertEqual(launches[0]["provider"], "codex")
        self.assertFalse(any("send-keys" in call for call in calls))

    def test_personal_codex_entry_does_not_enable_project_auto_trust(self):
        entry = fleet_reg._provider_entry({"profile": "personal"}, "codex", "/tmp/codex-home")
        self.assertFalse(entry["codex_auto_trust_project"])

    def test_register_accepts_exact_codex_uuid_and_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile_home = root / "profile"
            codex_home = root / "codex"
            cwd = root / "project"
            profile_home.mkdir()
            codex_home.mkdir()
            cwd.mkdir()
            saved = []
            uuid = "01a17a5a-100f-78f3-92f4-65802cee3c72"
            argv = ["fleet_reg.py", "register", "cc-w-example-codex",
                    "--short", "example-codex", "--profile", "work",
                    "--provider", "codex", "--codex-home", str(codex_home),
                    "--cwd", str(cwd), "--uuid", uuid]
            with mock.patch.object(fleet_reg, "CFG", {"work": str(profile_home)}), \
                 mock.patch.object(fleet_reg, "load", return_value={"sessions": []}), \
                 mock.patch.object(fleet_reg, "save", side_effect=lambda d: saved.append(copy.deepcopy(d))), \
                 mock.patch.object(fleet_reg, "transcript_path", return_value=str(codex_home / "rollout.jsonl")), \
                 mock.patch.object(sys, "argv", argv):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    fleet_reg.main()

            entry = saved[0]["sessions"][0]
            self.assertEqual(entry["provider"], "codex")
            self.assertEqual(entry["codex_home"], str(codex_home))
            self.assertEqual(entry["config_dir"], str(codex_home))
            self.assertEqual(entry["uuid"], uuid)

    def test_register_attaches_pending_provenance_to_scratch_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile_home = root / "profile"
            codex_home = root / "codex"
            cwd = root / "project"
            profile_home.mkdir()
            codex_home.mkdir()
            cwd.mkdir()
            source_transcript = root / "source.jsonl"
            handoff_context = root / "source-context.jsonl"
            source_transcript.write_text("source" + chr(10))
            handoff_context.write_text("excerpt" + chr(10))
            provenance = {"provider": "claude", "session_id": "source-uuid",
                          "transcript": str(source_transcript), "handoff_at": 123.0,
                          "context_excerpt": {"messages_included": 2},
                          "handoff_context": str(handoff_context)}
            pending = root / "pending.json"
            registry = root / "scratch-registry.json"
            registry.write_text(json.dumps({"sessions": []}))
            pending.write_text(json.dumps({"state": "NEEDS-INPUT",
                "target_name": "wrong-codex", "codex_home": str(codex_home),
                "cwd": str(cwd), "registry": str(registry), "source_provenance": provenance}))
            untouched_registry = root / "must-not-touch.json"
            uuid = "01a17a5a-100f-78f3-92f4-65802cee3c72"
            argv = ["fleet_reg.py", "register", "cc-w-example-codex",
                    "--short", "example-codex", "--profile", "work",
                    "--provider", "codex", "--codex-home", str(codex_home),
                    "--cwd", str(cwd), "--uuid", uuid,
                    "--registry", str(registry), "--source-provenance-file", str(pending)]
            with (
                mock.patch.object(fleet_reg, "CFG", {"work": str(profile_home)}),
                mock.patch.object(fleet_reg, "REG", str(untouched_registry)),
                mock.patch.object(fleet_reg, "transcript_path", return_value=str(codex_home / "rollout.jsonl")),
                mock.patch.object(sys, "argv", argv),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    argv.append("--handoff-resolved")
                    with self.assertRaises(SystemExit) as refused:
                        fleet_reg.main()
                    self.assertEqual(refused.exception.code, 2)
                    self.assertEqual(json.loads(registry.read_text()), {"sessions": []})
                    manifest = json.loads(pending.read_text())
                    manifest["target_name"] = "cc-w-example-codex"
                    pending.write_text(json.dumps(manifest))
                    argv.remove("--handoff-resolved")
                    with self.assertRaises(SystemExit) as refused:
                        fleet_reg.main()
                    self.assertEqual(refused.exception.code, 2)
                    self.assertEqual(json.loads(registry.read_text()), {"sessions": []})
                    argv.append("--handoff-resolved")
                    fleet_reg.main()

            saved = json.loads(registry.read_text())
            entry = saved["sessions"][0]
            self.assertEqual(entry["uuid"], uuid)
            self.assertEqual(entry["source_provenance"], provenance)
            self.assertFalse(untouched_registry.exists())

    def test_codex_unrecognized_no_rollout_is_unknown_and_left_live(self):
        code, output, saved, calls, _ = self._run_spawn(
            "Unexpected Codex setup screen\nChoose an option", None)

        self.assertEqual(code, 2, output)
        self.assertIn("UNKNOWN", output)
        self.assertNotIn("NEEDS-INPUT", output)
        self.assertEqual(saved, [])
        self.assertFalse(any("send-keys" in call for call in calls))
        self.assertFalse(any("kill-session" in call for call in calls))

    def test_codex_startup_choice_is_left_live_for_owner_without_auto_answer(self):
        code, output, saved, calls, _ = self._run_spawn(
            "Do you trust the contents of this directory?\nNo, exit", None)

        self.assertEqual(code, 2, output)
        self.assertIn("NEEDS-INPUT", output)
        self.assertEqual(saved, [])
        self.assertFalse(any("send-keys" in call for call in calls))
        self.assertFalse(any("kill-session" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
