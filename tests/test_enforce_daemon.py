"""The daemon's enforce branch, driven through the real main loop with its inputs faked."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import escalation   # noqa: E402
import fleet_watch  # noqa: E402

S = "cc-t"


class Stop(Exception):
    pass


class EnforceLoop(unittest.TestCase):
    def run_loop(self, events, mode="enforce", modeltext=None, gate=None, nudges=None, state="IDLE"):
        escalation.LOG_DIR = tempfile.mkdtemp()
        delivered, channel = [], []
        gate = gate or {"lane": 2, "escalate": False, "rule": "silent"}
        tmp = tempfile.mkdtemp()
        env = {"FLEET_FUNNEL": mode, "FLEET_FUNNEL_DIR": tmp}
        sleeps = {"n": 0}

        def sleep(_):
            raise Stop()

        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(sys, "argv", ["fleet_watch.py", "--daemon", "--interval", "0"]),
            mock.patch.object(fleet_watch, "setup_logging"), mock.patch.object(fleet_watch, "acquire_lock", return_value=1),
            mock.patch.object(fleet_watch, "_write_slug"), mock.patch.object(fleet_watch, "_load_pending", return_value=[]),
            mock.patch.object(fleet_watch, "_backfill_ring"), mock.patch.object(fleet_watch, "ensure_gate", return_value="ok"),
            mock.patch.object(fleet_watch, "PENDING_FILE", os.path.join(tmp, "pending.json")),
            mock.patch.object(fleet_watch, "EVENTS", os.path.join(tmp, "events.txt")),
            mock.patch.object(fleet_watch, "scan", return_value=(events, {}, {}, modeltext or {})),
            mock.patch.object(fleet_watch, "process_watches", return_value=[]),
            mock.patch.object(fleet_watch, "process_acks", return_value=[]),
            mock.patch.object(fleet_watch, "delivery_scope", return_value={S}),
            mock.patch.object(fleet_watch, "sessions_expecting", return_value=set()),
            mock.patch.object(fleet_watch, "hier_route", return_value=None),
            mock.patch.object(fleet_watch, "hier_escalations", return_value=[]),
            mock.patch.object(fleet_watch, "gate_watch", return_value=[]),
            mock.patch.object(fleet_watch, "gate_verdict", return_value=gate),
            mock.patch.object(fleet_watch, "_session_state", return_value=state),
            mock.patch.object(fleet_watch, "_maybe_nudge", return_value=nudges),
            mock.patch.object(fleet_watch, "_manager_owns", return_value=False),
            mock.patch.object(fleet_watch, "find_jsonl", return_value=None),
            mock.patch.object(fleet_watch, "_dispatch_nudge", return_value=True),
            mock.patch.object(fleet_watch, "deliver_batch", side_effect=lambda b, now=None: delivered.extend(b) or []),
            mock.patch.object(fleet_watch, "post_fleet_channel", side_effect=lambda s, m: channel.append(m)),
            mock.patch.object(fleet_watch, "_ask_usage_setup"), mock.patch.object(fleet_watch, "_usage_advice"),
            mock.patch.dict(sys.modules, {"usage_meter": mock.Mock()}),
            mock.patch.object(fleet_watch.time, "sleep", side_effect=sleep),
        ]
        for p in patches:
            p.start()
        try:
            try:
                fleet_watch.main()
            except Stop:
                pass
        finally:
            for p in reversed(patches):
                p.stop()
        return delivered, escalation.read(escalation.ESCALATIONS)

    def test_an_ungrounded_question_pages_once_with_its_reason(self):
        ask = "Which color should the harvest festival banner use this year for the shop front?"
        delivered, led = self.run_loop([(S, "NEEDS-INPUT-x-cc-t-1")], modeltext={S: [ask]})
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["reason"], "clarification_ungrounded")
        self.assertIn("Default", delivered[0]["match"])
        self.assertEqual([r["reason"] for r in led if r.get("reason")], ["clarification_ungrounded"])

    def test_the_same_event_pages_the_legacy_way_in_shadow(self):
        ask = "Which color should the harvest festival banner use this year for the shop front?"
        delivered, led = self.run_loop([(S, "NEEDS-INPUT-x-cc-t-1")], mode="shadow", modeltext={S: [ask]},
                                       gate={"lane": 1, "escalate": True, "rule": "session asks for input"})
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["reason"], "legacy")

    def test_a_stall_the_ladder_is_still_working_is_not_paged(self):
        n = {"episode_id": "e", "nudge_count": 2, "nudge_max": 5, "last_outcome": "ack_only", "failed_dispatches": 0}
        delivered, led = self.run_loop([(S, "STALL-CC-T: no new transcript output for 905s")], nudges=n,
                                       gate={"lane": 1, "escalate": True, "rule": "would have paged the old way"})
        self.assertEqual(delivered, [])
        self.assertEqual([r.get("note") for r in led if r.get("kind") == "handled"], ["ladder in progress"])

    def test_a_ladder_that_ran_out_pages_as_nudge_refused(self):
        n = {"episode_id": "e", "nudge_count": 5, "nudge_max": 5, "last_outcome": "restate_done", "failed_dispatches": 0}
        delivered, _ = self.run_loop([(S, "STALL-CC-T: no new transcript output for 905s")], nudges=n)
        self.assertEqual([d["reason"] for d in delivered], ["nudge_refused"])

    def test_a_dead_session_still_reaches_the_gate_and_pages(self):
        delivered, _ = self.run_loop([(S, "SESSION-DEAD-CC-T: no tmux session")],
                                     gate={"lane": 1, "escalate": True, "rule": "session is dead"})
        self.assertEqual([d["reason"] for d in delivered], ["session_dead"])

    def test_an_event_that_is_not_a_case_is_ledgered_not_paged(self):
        delivered, led = self.run_loop([(S, "ERROR-x: something odd happened")],
                                       gate={"lane": 1, "escalate": True, "rule": "model said so"})
        self.assertEqual(delivered, [])
        self.assertEqual([r["note"] for r in led if r.get("kind") == "handled"], ["not one of the three cases"])


if __name__ == "__main__":
    unittest.main()
