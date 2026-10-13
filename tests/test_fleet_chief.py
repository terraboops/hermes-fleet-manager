import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import fleet_chief as fch  # noqa: E402
import fleet_contract as fc  # noqa: E402
import fleet_manager as fm  # noqa: E402

ACC = [{"check": "c", "positive": ["p"], "negative": ["n"]}]
B = ("/r/src",)
EFF = dict(dispatch=lambda s, d: None, send=lambda s, t: None, state_of=lambda s: "working",
           last_growth=lambda s: 0, transcript_of=lambda s: None, verify=lambda c, r: True)


class Chief(unittest.TestCase):
    def test_goal_creates_registered_manager_and_contract(self):
        cos = fch.ChiefOfStaff()
        m = cos.take_goal("g1", "do it", B, ACC, 9e9, 3, "mgr-1", EFF)
        self.assertEqual(cos.registry.nodes["mgr-1"]["role"], fm.MANAGER)
        self.assertEqual(m.own.owner_session, "mgr-1")
        self.assertEqual(m.budget.left, 3)

    def test_duplicate_manager_refused_and_nothing_left_behind(self):
        cos = fch.ChiefOfStaff()
        cos.take_goal("g1", "do it", B, ACC, 9e9, 3, "mgr-1", EFF)
        with self.assertRaises(fm.HierarchyError):
            cos.take_goal("g2", "again", B, ACC, 9e9, 3, "mgr-1", EFF)
        self.assertEqual(list(cos.registry.nodes), ["mgr-1"])

    def test_bad_effects_roll_back_registration(self):
        cos = fch.ChiefOfStaff()
        with self.assertRaises(TypeError):
            cos.take_goal("g1", "x", B, ACC, 9e9, 3, "mgr-1", {})
        self.assertEqual(cos.registry.nodes, {})

    def test_report_is_pointers_and_verdict_only(self):
        cos = fch.ChiefOfStaff()
        m = cos.take_goal("g1", "do it", B, ACC, 9e9, 3, "mgr-1", EFF)
        text = cos.report("g1", None, None, m.ledger)
        self.assertEqual(text, "goal g1: pending (no token)")
        self.assertEqual(len(cos.level.delivered), 1)


if __name__ == "__main__":
    unittest.main()
