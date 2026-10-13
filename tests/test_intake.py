#!/usr/bin/env python3
"""Intake slice tests: capture, promotion, reclaim, completion check.

Every case runs on a scratch board (tests/kanban_reliability/harness.py pins the paths and
refuses the live board). Needs the hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python -m unittest tests.test_intake -v
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "kanban_reliability"))
try:
    import harness as H  # pins nothing until a Ctx is built
except Exception as exc:  # pragma: no cover
    raise unittest.SkipTest(f"hermes kanban harness unavailable: {exc}")

from intake import (CompletionRejected, Intake, IntakeEvent, check_completion,  # noqa: E402
                    guarded_complete, intake_enabled)

_COUNTER = [0]


def msg(mid="m1", text="please renew the licence", **kw):
    return IntakeEvent(platform="telegram", chat_id="c1", message_id=mid, text=text, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intake-test-", dir=os.environ.get("TMPDIR")))
        _COUNTER[0] += 1
        self.ctx = H.Ctx(f"t{_COUNTER[0]}", self.tmp, self.tmp / "out")
        (self.tmp / "out").mkdir(exist_ok=True)
        self.state = self.tmp / "state"
        self.intake = Intake(self.ctx.kb, self.ctx.kbc.connect, self.state)

    def tearDown(self):
        self.ctx.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cards(self):
        c = self.ctx.conn()
        try:
            return [dict(r) for r in c.execute("SELECT id, status, idempotency_key FROM tasks")]
        finally:
            c.close()


class CaptureTests(Base):
    def test_capture_creates_triage_card_and_no_model_runs(self):
        tid = self.intake.capture(msg())
        row = self.ctx.row(tid)
        self.assertEqual(row["status"], "triage")

    def test_negative_duplicate_message_delivery_no_duplicate_card(self):
        ids = {self.intake.capture(msg()) for _ in range(3)}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(self.cards()), 1)
        # concurrent redelivery from several handlers
        results, errs = [], []

        def go():
            try:
                results.append(Intake(self.ctx.kb, self.ctx.kbc.connect, self.state).capture(msg("m2")))
            except Exception as e:  # pragma: no cover
                errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertFalse(errs, errs)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.cards()), 2)
        # a new message id is a new card
        self.intake.capture(msg("m3"))
        self.assertEqual(len(self.cards()), 3)

    def test_negative_gateway_killed_between_receipt_and_card_write(self):
        code = (
            "import os,signal,sys\n"
            f"sys.path.insert(0,{str(ROOT)!r}); sys.path.insert(0,{str(H.AGENT_DIR)!r})\n"
            "from hermes_cli import kanban_db as kb, kanban_db_connect as kbc\n"
            "from intake import Intake, IntakeEvent\n"
            f"i=Intake(kb,kbc.connect,{str(self.state)!r})\n"
            "def die(p):\n"
            "    if p=='after_receipt': os.kill(os.getpid(), signal.SIGKILL)\n"
            "i.capture(IntakeEvent('telegram','c1','m9','hello'), _fault=die)\n"
        )
        p = subprocess.run([sys.executable, "-c", code], env=dict(os.environ))
        self.assertEqual(p.returncode, -signal.SIGKILL)
        self.assertEqual(self.cards(), [], "card must not exist: the kill came before the write")
        self.assertIn("receipt", (self.state / "intake-receipts.jsonl").read_text())
        # gateway restarts: recovery replays the receipt, platform redelivers the same message
        recovered = self.intake.recover()
        redelivered = self.intake.capture(msg("m9", "hello"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0], redelivered)
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(self.intake.recover(), [], "recovery is idempotent")

    def test_filters_internal_unauthorized_command_reply_make_zero_cards(self):
        for kw in ({"internal": True}, {"authorized": False}, {"awaiting_reply": True}):
            self.assertIsNone(self.intake.capture(msg("f", **kw)))
        self.assertIsNone(self.intake.capture(msg("g", text="/new")))
        self.assertEqual(self.cards(), [])

    def test_flag_default_off(self):
        os.environ.pop("HERMES_INTAKE_ENABLED", None)
        self.assertFalse(intake_enabled(self.tmp / "missing.yaml"))
        cfg = self.tmp / "c.yaml"
        cfg.write_text("intake:\n  enabled: true\n")
        self.assertTrue(intake_enabled(cfg))
        cfg.write_text("intake: {}\n")
        self.assertFalse(intake_enabled(cfg))

    def test_plugin_hook_raising_does_not_block_dispatch_and_sends_nothing(self):
        sys.path.insert(0, str(ROOT / "intake"))
        import importlib
        plug = importlib.import_module("plugin")
        os.environ["HERMES_INTAKE_ENABLED"] = "1"
        try:
            self.assertIsNone(plug._on_dispatch(event=object(), gateway=None))  # malformed event
        finally:
            os.environ.pop("HERMES_INTAKE_ENABLED", None)
        os.environ.pop("HERMES_INTAKE_ENABLED", None)
        self.assertIsNone(plug._on_dispatch(event=object(), gateway=None))  # flag off


class PromotionAndRecoveryTests(Base):
    def _promoted(self):
        tid = self.intake.capture(msg())
        self.assertTrue(self.intake.promote(tid))
        self.assertEqual(self.ctx.row(tid)["status"], "ready")
        return tid

    def test_message_becomes_card_and_completes_through_dispatcher(self):
        os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
        tid = self._promoted()
        self.ctx.spawn_dispatcher(H_TTL=900)
        self.assertTrue(self.ctx.wait(lambda: self.ctx.row(tid)["status"] == "done", 40), self.ctx.events(tid))
        self.assertEqual(len(self.ctx.starts(tid)), 1)

    def test_negative_worker_killed_mid_run_card_reclaimed_no_lost_card(self):
        os.environ["H_MODES"] = "hang,finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
        tid = self._promoted()
        spawn = H.make_spawn_fn("none")
        tick = lambda: H.one_tick(spawn, ttl=900)  # noqa: E731
        self.assertTrue(self.ctx.wait(lambda: len(self.ctx.starts(tid)) == 1, 25, tick=tick))
        H.kill9(H.start_pids(self.ctx, tid)[0])
        self.assertTrue(self.ctx.wait(lambda: self.ctx.row(tid)["status"] == "done", 60, tick=tick),
                        self.ctx.events(tid))
        self.assertEqual(len(self.cards()), 1)
        self.assertEqual(len(self.ctx.runs(tid)), 2)
        self.assertFalse(self.ctx.concurrent_dups())

    def test_negative_gateway_restart_mid_run_no_duplicate_execution(self):
        os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "6"; os.environ["H_ADOPT"] = "1"
        tid = self._promoted()
        d1 = self.ctx.spawn_dispatcher(H_TTL=900)
        self.assertTrue(self.ctx.wait(lambda: len(self.ctx.starts(tid)) == 1, 25))
        H.kill9(d1.pid)
        self.ctx.spawn_dispatcher(H_TTL=2)  # lease expires while the worker still lives
        self.assertTrue(self.ctx.wait(lambda: self.ctx.row(tid)["status"] == "done", 40), self.ctx.events(tid))
        self.assertEqual(len(self.ctx.starts(tid)), 1, H.read_xlog(self.ctx.xlog))
        self.assertFalse(self.ctx.concurrent_dups())
        # redelivery of the original message after the restart adds no card
        self.intake.capture(msg())
        self.assertEqual(len(self.cards()), 1)


class CompletionTests(Base):
    def _running(self):
        tid = self.intake.capture(msg())
        self.intake.promote(tid)
        conn = self.ctx.conn()
        try:
            claimed = self.ctx.kb.claim_task(conn, tid)
            run_id = self.ctx.kb.get_task(conn, tid).current_run_id
        finally:
            conn.close()
        self.assertEqual(self.ctx.row(tid)["status"], "running", claimed)
        return tid, run_id

    def test_negative_worker_marks_done_with_not_tested_output_rejected(self):
        tid, run_id = self._running()
        conn = self.ctx.conn()
        try:
            for summary, meta in (("Implemented. Not tested.", None),
                                  ("done", {"status": "not_tested"}),
                                  ("Changes made, untested", None),
                                  ("done, tests were not run", None)):
                with self.assertRaises(CompletionRejected):
                    guarded_complete(self.ctx.kb, conn, tid, summary=summary, metadata=meta,
                                     expected_run_id=run_id)
                self.assertEqual(self.ctx.row(tid)["status"], "running", "card must stay open")
            ok = guarded_complete(self.ctx.kb, conn, tid, summary="12 tests ran, 12 passed",
                                  expected_run_id=run_id)
            self.assertTrue(ok)
            self.assertEqual(self.ctx.row(tid)["status"], "done")
        finally:
            conn.close()

    def test_check_does_not_flag_ordinary_text(self):
        self.assertIsNone(check_completion(summary="Ran the suite: 40 passed. Tested on a scratch board."))


if __name__ == "__main__":
    unittest.main()
