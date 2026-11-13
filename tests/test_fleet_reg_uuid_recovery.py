import contextlib
import io
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet_reg
fleet_transcript = importlib.import_module("fleet_transcript")


class FleetRegUuidRecoveryTests(unittest.TestCase):
    def _entry(self, profile_home):
        return {
            "config_dir": str(profile_home),
            "cwd": "/repo",
            "profile": "work",
            "provider": "claude",
        }

    def test_ambiguous_project_transcripts_do_not_select_newest(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "projects" / fleet_reg.slug("/repo")
            project.mkdir(parents=True)
            older = project / "11111111-1111-4111-8111-111111111111.jsonl"
            newer = project / "22222222-2222-4222-8222-222222222222.jsonl"
            older.write_text("{}\n")
            newer.write_text("{}\n")
            os.utime(older, (1_000_000, 1_000_000))
            os.utime(newer, (2_000_000, 2_000_000))

            self.assertIsNone(fleet_reg._recover_uuid(self._entry(temp)))

    def test_live_process_uuid_wins_over_project_siblings(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "projects" / fleet_reg.slug("/repo")
            project.mkdir(parents=True)
            live = project / "11111111-1111-4111-8111-111111111111.jsonl"
            newest = project / "22222222-2222-4222-8222-222222222222.jsonl"
            live.write_text("{}\n")
            newest.write_text("{}\n")
            os.utime(live, (1_000_000, 1_000_000))
            os.utime(newest, (2_000_000, 2_000_000))

            with mock.patch.object(fleet_transcript, "resolve", return_value=(str(live), "process")):
                self.assertEqual(fleet_reg._recover_uuid(self._entry(temp)), live.stem)

    def test_register_refuses_ambiguous_transcripts_without_uuid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile"
            project = profile / "projects" / fleet_reg.slug("/repo")
            project.mkdir(parents=True)
            (project / "11111111-1111-4111-8111-111111111111.jsonl").write_text("{}\n")
            (project / "22222222-2222-4222-8222-222222222222.jsonl").write_text("{}\n")
            registry = root / "registry.json"
            registry.write_text('{"sessions": []}\n')
            argv = ["fleet_reg.py", "register", "cc-w-test", "--short", "test",
                    "--profile", "work", "--cwd", "/repo", "--registry", str(registry)]

            with mock.patch.object(fleet_reg, "CFG", {"work": str(profile)}), \
                 mock.patch.object(fleet_reg, "REG", str(registry)), \
                 mock.patch.object(fleet_transcript, "pane_uuid", return_value=None), \
                 mock.patch.object(sys, "argv", argv):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as refused:
                        fleet_reg.main()

            self.assertEqual(refused.exception.code, 1, output.getvalue())
            self.assertIn("REFUSED", output.getvalue())
            self.assertEqual(json.loads(registry.read_text()), {"sessions": []})

    def test_one_project_transcript_is_recovered(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "projects" / fleet_reg.slug("/repo")
            project.mkdir(parents=True)
            only = project / "11111111-1111-4111-8111-111111111111.jsonl"
            only.write_text("{}\n")

            self.assertEqual(fleet_reg._recover_uuid(self._entry(temp)), only.stem)


if __name__ == "__main__":
    unittest.main()
