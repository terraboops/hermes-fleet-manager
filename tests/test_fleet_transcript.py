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


class TestInboundInstructions(unittest.TestCase):
    """The instructions FROM the operator, which the daemon must never be blind to.

    The failure this pins: a plan was parked as "not-yet-authorised" 24 minutes
    after the operator asked for it, because the state fingerprint carried only
    pane content and a daemon log line. An agent cannot judge authorisation
    without reading what she actually asked for.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.jsonl")

    def _write(self, records):
        with open(self.path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    @staticmethod
    def _user(text, ts="2026-09-28T19:00:00.000Z"):
        return {"type": "user", "timestamp": ts,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    @staticmethod
    def _queued(text, op="enqueue", ts="2026-09-28T19:05:00.000Z"):
        return {"type": "queue-operation", "operation": op, "timestamp": ts,
                "content": text}

    def test_a_typed_message_is_an_instruction(self):
        self._write([self._user("build the marketplace plugin plz")])
        epoch, text = fleet_transcript.newest_inbound(self.path)
        self.assertEqual(text, "build the marketplace plugin plz")
        self.assertIsNotNone(epoch)

    def test_a_pasted_dispatch_is_an_instruction_even_though_it_is_not_a_user_turn(self):
        # Every fleet dispatch lands this way when the session is busy: a
        # queue-operation with the paste in `content`, never a user turn.
        self._write([self._queued('<pasted_content id="04b1"> TERRA - DIG INTO CI. Do it.')])
        _, text = fleet_transcript.newest_inbound(self.path)
        self.assertEqual(text, "TERRA - DIG INTO CI. Do it.")

    def test_an_enqueue_and_its_remove_are_one_instruction_not_two(self):
        # Claude Code writes the paste twice (enqueue, then remove/absorbed).
        body = '<pasted_content id="8"> the same instruction'
        self._write([self._queued(body),
                     self._queued(body, op="remove", ts="2026-09-28T19:05:01.000Z")])
        hits = fleet_transcript.recent_inbound(self.path, n=3)
        self.assertEqual(len(hits), 1)

    def test_machine_frames_are_not_instructions(self):
        self._write([
            self._user("<system-reminder>you are a helpful agent</system-reminder>"),
            self._user("[Subagent hand-back] the text below is the final report"),
            self._user("This session is being continued from a previous conversation. Summary:"),
            self._queued("<task-notification><task-id>x</task-id>"),
            self._user("a real one"),
        ])
        hits = fleet_transcript.recent_inbound(self.path, n=5)
        self.assertEqual([t for _, t in hits], ["a real one"])

    def test_an_image_companion_is_not_an_instruction(self):
        # Claude Code records an isMeta turn companion for an image a turn
        # carries (annotation only, no image data). It is not something the
        # operator said, and counting it as the newest inbound hides their real
        # last instruction behind a phantom.
        self._write([
            self._user("fix the windmill", ts="2026-09-28T19:00:00.000Z"),
            {"type": "user", "isMeta": True, "turnCompanion": True,
             "timestamp": "2026-09-28T19:05:00.000Z",
             "message": {"role": "user", "content": [
                 {"type": "text",
                  "text": "[Image: original 2820x600, displayed at 2000x426. "
                          "Multiply coordinates by 1.41 to map to original image.]"}]}},
        ])
        self.assertEqual(fleet_transcript.newest_inbound(self.path)[1], "fix the windmill")

    def test_newest_wins_and_recent_returns_them_oldest_first(self):
        self._write([self._user("first", ts="2026-09-28T19:00:00.000Z"),
                     self._user("second", ts="2026-09-28T19:10:00.000Z"),
                     self._user("third", ts="2026-09-28T19:20:00.000Z")])
        self.assertEqual(fleet_transcript.newest_inbound(self.path)[1], "third")
        self.assertEqual([t for _, t in fleet_transcript.recent_inbound(self.path, n=2)],
                         ["second", "third"])

    def test_the_digest_changes_when_she_says_something_new(self):
        # The fingerprint's change signal must fire on a new instruction, even
        # when the pane is untouched.
        self._write([self._user("do the thing")])
        first = fleet_transcript.newest_inbound(self.path)[1]
        self._write([self._user("do the thing"), self._user("now do the other thing")])
        second = fleet_transcript.newest_inbound(self.path)[1]
        self.assertNotEqual(first, second)

    def test_no_instructions_reports_none_never_a_guess(self):
        self._write([{"type": "assistant", "message": {"role": "assistant", "content": []}}])
        self.assertEqual(fleet_transcript.newest_inbound(self.path), (None, None))

    def test_a_missing_file_is_not_a_crash(self):
        self.assertEqual(fleet_transcript.newest_inbound("/nope/none.jsonl"), (None, None))


if __name__ == "__main__":
    unittest.main()
