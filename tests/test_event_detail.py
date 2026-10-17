import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet_watch


class EventDetailTest(unittest.TestCase):
    def setUp(self):
        self._orig = fleet_watch._last_said
        fleet_watch._last_said = lambda sn, cap=700, now=None: "waiting on the headless run"

    def tearDown(self):
        fleet_watch._last_said = self._orig

    def test_stall_carries_what_the_session_said(self):
        out = fleet_watch.with_detail("cc-x", "STALL-CC-X: no new transcript output for 905s", {})
        self.assertIn("last said: waiting on the headless run", out)

    def test_miss_and_done_carry_detail(self):
        for m in ("SENTINEL-MISSED-CC-X: expected DONE-a by 1", "DONE-recheck-1"):
            self.assertIn("last said:", fleet_watch.with_detail("cc-x", m, {}))

    def test_event_with_an_ask_is_left_alone(self):
        m = "NEEDS-INPUT-t -- the ask"
        self.assertEqual(fleet_watch.with_detail("cc-x", m, {}), m)

    def test_new_lines_this_tick_win_over_a_transcript_read(self):
        out = fleet_watch.with_detail("cc-x", "DONE-a", {"cc-x": ["finished the thing"]})
        self.assertIn("finished the thing", out)
        self.assertNotIn("headless", out)

    def test_page_lines_allow_long_detail(self):
        line = "cc-x: " + "y" * 1000
        self.assertEqual(fleet_watch.format_page([{"session": "cc-x", "match": "y" * 1000}]), line)


if __name__ == "__main__":
    unittest.main()
