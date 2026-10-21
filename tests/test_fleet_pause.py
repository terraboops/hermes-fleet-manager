"""An owner-paused session is skipped by the daemon; a bad pause file never silences the fleet."""
import json
import os
import tempfile
import time
import unittest

import fleet_watch


class PauseTests(unittest.TestCase):
    def _use(self, content):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "paused.json")
        if content is not None:
            open(f, "w").write(content)
        old = fleet_watch.PAUSE_FILE
        fleet_watch.PAUSE_FILE = f
        self.addCleanup(setattr, fleet_watch, "PAUSE_FILE", old)

    def test_paused_session_is_paused_and_others_are_not(self):
        self._use(json.dumps({"cc-x": {"since": 1, "until": None}}))
        self.assertTrue(fleet_watch.is_paused("cc-x"))
        self.assertFalse(fleet_watch.is_paused("cc-y"))

    def test_expired_pause_lapses(self):
        self._use(json.dumps({"cc-x": {"until": int(time.time()) - 5}}))
        self.assertFalse(fleet_watch.is_paused("cc-x"))

    def test_future_pause_holds(self):
        self._use(json.dumps({"cc-x": {"until": int(time.time()) + 600}}))
        self.assertTrue(fleet_watch.is_paused("cc-x"))

    def test_missing_or_broken_file_pauses_nobody(self):
        for content in (None, "{not json", "[1, 2]"):
            self._use(content)
            self.assertFalse(fleet_watch.is_paused("cc-x"), content)


if __name__ == "__main__":
    unittest.main()
