"""A session parked on the owner is redirected to unblocked work, not nudged with generic text."""
import os
import tempfile
import unittest

import ladder


class OwnerWaitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        ladder.FOCUS_DIR = self.dir
        with open(os.path.join(self.dir, "s1_focus.md"), "w") as f:
            f.write("GOAL: make the game better.\nNEXT: build the top unblocked backlog item.\n")

    def test_detects_waiting_on_the_owner(self):
        for said in ("These five are still waiting on you", "Nothing has changed since the check",
                     "waiting on Terra for the keys", "needs your decision", "I am still blocked"):
            self.assertTrue(ladder.waiting_on_owner(said), said)

    def test_ordinary_text_is_not_an_owner_wait(self):
        for said in ("", None, "Shipped the quest, commit abc123, verified live.", "Running the tests now"):
            self.assertFalse(ladder.waiting_on_owner(said), said)

    def test_owner_wait_gets_the_redirect_on_every_rung(self):
        for rung in (1, 2, 3):
            t, grounded = ladder.rung_text("s1", rung, 15, None, last_words="still waiting on you")
            self.assertIn("not a reason to stop", t)
            self.assertIn("do not poll", t)
            self.assertIn("build the top unblocked backlog item", t)
            self.assertTrue(grounded)

    def test_no_owner_wait_keeps_the_old_text(self):
        t, _ = ladder.rung_text("s1", 1, 15, None, last_words="working on it")
        self.assertIn("you have been quiet 15 min", t)
        t, _ = ladder.rung_text("s1", 1, 15, None)
        self.assertIn("you have been quiet 15 min", t)

    def test_the_text_never_carries_an_authorization(self):
        t, _ = ladder.rung_text("s1", 1, 15, None, last_words="still waiting on you")
        for word in ("approve", "authoriz", "deploy to production", "yes"):
            self.assertNotIn(word, t.lower())


if __name__ == "__main__":
    unittest.main()
