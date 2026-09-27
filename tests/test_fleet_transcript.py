"""Tests for transcript location.

The bug that motivated this module: a session whose cwd contains a dot
(~/Developer/terratauri.com) resolved to a project directory that does not
exist, because the name was rebuilt by replacing only slashes. Every dispatch to
that session was reported NOT-LANDED and re-sent, while the marker sat in the
transcript twice.

These tests pin the invariant that matters: location must not depend on
reconstructing the project-directory name from the cwd at all.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_transcript  # noqa: E402


def make_transcript(root, project_dir, uuid, age_seconds=0):
    d = os.path.join(root, "projects", project_dir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, uuid + ".jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(p, (t, t))
    return p


class TestLocateByUuid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fleet_transcript._CACHE.clear()

    def test_finds_a_cwd_with_a_dot(self):
        """The dotted-cwd case: project dir name is unrelated to what we compute."""
        uuid = "0feca4fc-7fc4-4761-a025-0153e68c6acb"
        real = make_transcript(self.tmp, "-Users-terra-Developer-terratauri-com", uuid)
        cfg = os.path.expanduser(self.tmp)
        entry = {"config_dir": cfg, "cwd": "/Users/terra/Developer/terratauri.com", "uuid": uuid}
        path, source = fleet_transcript.resolve(entry, tmux=None)
        self.assertEqual(path, real)
        self.assertEqual(source, "uuid")

    def test_finds_a_transcript_in_an_unexpectedly_named_dir(self):
        """Even a dir name we could never predict is found, because we never predict it."""
        uuid = "11111111-2222-3333-4444-555555555555"
        real = make_transcript(self.tmp, "totally-unexpected-name", uuid)
        entry = {"config_dir": self.tmp, "cwd": "/wherever/at/all", "uuid": uuid}
        path, source = fleet_transcript.resolve(entry, tmux=None)
        self.assertEqual(path, real)
        self.assertEqual(source, "uuid")

    def test_newest_wins_when_the_uuid_appears_twice(self):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        make_transcript(self.tmp, "old-dir", uuid, age_seconds=3600)
        new = make_transcript(self.tmp, "new-dir", uuid)
        entry = {"config_dir": self.tmp, "cwd": "/x", "uuid": uuid}
        path, _ = fleet_transcript.resolve(entry, tmux=None)
        self.assertEqual(path, new)

    def test_missing_uuid_file_falls_back_to_newest_not_to_none(self):
        newest = make_transcript(self.tmp, "-Users-terra-Developer-mine", "ffffffff-0000-0000-0000-000000000000", age_seconds=5)
        entry = {"config_dir": self.tmp, "cwd": "/Users/terra/Developer/mine", "uuid": "does-not-exist"}
        path, source = fleet_transcript.resolve(entry, tmux=None)
        self.assertEqual(path, newest)
        self.assertEqual(source, "newest")

    def test_nothing_found_reports_none_never_a_guess(self):
        entry = {"config_dir": self.tmp, "cwd": "/nothing/here", "uuid": "nope"}
        path, source = fleet_transcript.resolve(entry, tmux=None)
        self.assertIsNone(path)
        self.assertEqual(source, "none")

    def test_cache_does_not_survive_a_deleted_file(self):
        uuid = "99999999-9999-9999-9999-999999999999"
        p = make_transcript(self.tmp, "-d", uuid)
        entry = {"config_dir": self.tmp, "cwd": "/d", "uuid": uuid}
        self.assertEqual(fleet_transcript.resolve(entry, tmux=None)[0], p)
        os.remove(p)
        self.assertIsNone(fleet_transcript.resolve(entry, tmux=None)[0])


class TestLiveProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fleet_transcript._CACHE.clear()

    def test_uuid_is_read_from_a_resume_command_line(self):
        u = "12345678-1234-1234-1234-123456789abc"
        with mock.patch("fleet_transcript.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(stdout="4321\n"),           # tmux list-panes
                mock.Mock(stdout=""),                 # pgrep children
                mock.Mock(stdout="claude --remote-control --resume %s\n" % u),
            ]
            self.assertEqual(fleet_transcript.pane_uuid("cc-x"), u)

    def test_uuid_is_read_from_a_session_id_command_line(self):
        u = "87654321-4321-4321-4321-cba987654321"
        with mock.patch("fleet_transcript.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(stdout="99\n"),
                mock.Mock(stdout=""),
                mock.Mock(stdout="claude --session-id=%s\n" % u),
            ]
            self.assertEqual(fleet_transcript.pane_uuid("cc-x"), u)

    def test_no_ui_means_no_uuid_not_a_crash(self):
        with mock.patch("fleet_transcript.subprocess.run", side_effect=Exception("no tmux")):
            self.assertIsNone(fleet_transcript.pane_uuid("cc-x"))

    def test_process_uuid_beats_a_stale_registry_uuid(self):
        """A registry entry that predates a resume must not win over the live process."""
        live = "22222222-2222-2222-2222-222222222222"
        real = make_transcript(self.tmp, "-Users-terra-Developer-wolfgang", live)
        entry = {"config_dir": self.tmp, "cwd": "/Users/terra/Developer/wolfgang",
                 "uuid": "stale-stale-stale-stale-stale"}
        with mock.patch("fleet_transcript.pane_uuid", return_value=live):
            path, source = fleet_transcript.resolve(entry, tmux="cc-w-wolfgang-x")
        self.assertEqual(path, real)
        self.assertEqual(source, "process")

    def test_drift_reports_a_mismatch(self):
        entry = {"name": "cc-x", "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        with mock.patch("fleet_transcript.pane_uuid", return_value="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"):
            self.assertTrue(fleet_transcript.drift(entry))
        with mock.patch("fleet_transcript.pane_uuid", return_value=entry["uuid"]):
            self.assertFalse(fleet_transcript.drift(entry))


if __name__ == "__main__":
    unittest.main()
