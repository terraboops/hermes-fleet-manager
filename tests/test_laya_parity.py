"""Parity harness: replay is exact, perturbations give the expected flips and best-fit
threshold, bounds map to exit codes, and a bad fixture hash refuses to run."""
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import laya_parity as lp  # noqa: E402

THR = 0.59
# (score, tags). Texts repeat on purpose: the real log has one text with many scores.
SPEC = [(0.10, ["low"]), (0.30, ["low"]), (0.50, ["low", "near_threshold"]),
        (0.58, ["low", "near_threshold"]), (0.60, ["near_threshold"]),
        (0.62, ["near_threshold"]), (0.80, ["high"]), (0.95, ["high"])]


def write_fixtures(d, spec=SPEC, sha=None):
    rows = [{"id": "r%02d" % i, "text": "event %d" % (i % 3), "mlx_score": s,
             "mlx_decision": s >= THR, "source_ts": None, "tags": t}
            for i, (s, t) in enumerate(spec)]
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    with open(os.path.join(d, "fixtures.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(body)
    with open(os.path.join(d, "manifest.json"), "w") as fh:
        json.dump({"version": "t", "threshold": THR, "decision_rule": "score >= 0.59 -> passes",
                   "sha256_of_fixtures": sha or hashlib.sha256(body.encode()).hexdigest()}, fh)
    return rows


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.rows = write_fixtures(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def run_fn(self, fn, **kw):
        return lp.run_parity("test", self.dir, fn=fn, **kw)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lp.main(["--fixtures", self.dir, *args])
        return code, out.getvalue(), err.getvalue()


class ReplayTests(Base):
    def test_replay_is_exact(self):
        r = lp.run_parity("replay", self.dir, batch_size=3)
        m = r["metrics"]
        self.assertEqual(m["agreement"]["overall"], 1.0)
        self.assertEqual(m["agreement"]["near_threshold"], 1.0)
        self.assertEqual(m["score_drift"]["max_abs_delta"], 0)
        self.assertEqual(m["score_drift"]["mean_abs_delta"], 0)
        self.assertAlmostEqual(m["score_drift"]["pearson"], 1.0)
        self.assertAlmostEqual(m["score_drift"]["spearman"], 1.0)
        self.assertEqual(m["flips"]["count"], 0)
        self.assertEqual(m["best_fit"]["agreement"], 1.0)

    def test_replay_survives_repeated_texts_with_different_scores(self):
        scores, _ = lp.run_backend(lp.resolve_backend("replay", self.rows),
                                   [r["text"] for r in self.rows], 2)
        self.assertEqual(scores, [r["mlx_score"] for r in self.rows])

    def test_report_metadata(self):
        r = lp.run_parity("replay", self.dir)
        meta = r["meta"]
        for k in ("backend", "fixture_version", "fixture_sha256", "timestamp", "host", "python",
                  "package_version", "git_commit"):
            self.assertIn(k, meta)
        self.assertEqual(meta["backend"], "replay")
        self.assertEqual(meta["fixture_version"], "t")
        json.dumps(r)  # serialisable


class PerturbationTests(Base):
    def test_known_flips_and_direction(self):
        # 0.62 -> 0.55 loses a pass; 0.58 -> 0.65 gains one.
        shift = {0.62: 0.55, 0.58: 0.65}
        want = [shift.get(s, s) for s, _ in SPEC]
        it = iter(want)
        fn = lambda texts: [next(it) for _ in texts]  # noqa: E731
        m = self.run_fn(fn, batch_size=4)["metrics"]
        self.assertEqual(m["flips"]["count"], 2)
        self.assertEqual(m["flips"]["by_direction"], {"pass_to_hold": 1, "hold_to_pass": 1})
        ids = {f["id"]: f for f in m["flips"]["cases"]}
        self.assertEqual(set(ids), {"r03", "r05"})
        self.assertAlmostEqual(ids["r05"]["delta"], -0.07)
        self.assertTrue(ids["r05"]["mlx_decision"])
        self.assertFalse(ids["r05"]["backend_decision"])
        self.assertAlmostEqual(m["agreement"]["overall"], 6 / 8)
        self.assertAlmostEqual(m["agreement"]["near_threshold"], 2 / 4)
        self.assertAlmostEqual(m["score_drift"]["max_abs_delta"], 0.07)

    def test_best_fit_threshold_recovers_a_uniform_shift(self):
        # Backend scores 0.1 lower everywhere: 0.59 now misfires, 0.49..0.50 is perfect.
        fn = lambda texts: [self._next_low() for _ in texts]  # noqa: E731
        self._low = iter([s - 0.10 for s, _ in SPEC])
        m = self.run_fn(fn)["metrics"]
        self.assertGreater(m["flips"]["count"], 0)
        bf = m["best_fit"]
        self.assertEqual(bf["agreement"], 1.0)
        self.assertEqual(bf["flips_remaining"], 0)
        # Separation lies between 0.48 (mlx 0.58) and 0.50 (mlx 0.60).
        self.assertTrue(0.48 < bf["threshold"] <= 0.50, bf["threshold"])

    def _next_low(self):
        return next(self._low)

    def test_best_fit_all_false_and_ties(self):
        # A backend that is uninformative cannot beat predicting the majority class.
        bf = lp.best_fit_threshold([0.5] * 8, [r["mlx_decision"] for r in self.rows])
        self.assertAlmostEqual(bf["agreement"], 4 / 8)  # 4 holds, 4 passes: a coin flip
        self.assertGreater(bf["threshold"], 0.5)

    def test_constant_backend_flips_every_disagreeing_row(self):
        m = lp.run_parity("constant:0.9", self.dir)["metrics"]
        self.assertEqual(m["flips"]["by_direction"]["hold_to_pass"], 4)
        self.assertIsNone(m["score_drift"]["pearson"])  # zero variance, not a crash

    def test_spearman_handles_ties(self):
        self.assertAlmostEqual(lp.spearman([1, 2, 2, 3], [1, 2, 2, 3]), 1.0)
        self.assertAlmostEqual(lp.spearman([1, 2, 3], [3, 2, 1]), -1.0)

    def test_p95(self):
        self.assertAlmostEqual(lp.percentile(list(range(101)), 95), 95)


class SelectionTests(Base):
    def test_tags_limit_and_batching(self):
        calls = []

        def fn(texts):
            calls.append(len(texts))
            return [0.5] * len(texts)
        r = self.run_fn(fn, tags=["near_threshold"], limit=4, batch_size=3)
        self.assertEqual(r["metrics"]["n"], 4)
        self.assertEqual(calls, [3, 1])

    def test_tag_prefix_matches_valued_tags(self):
        rows = [{"tags": ["prior_24h:4"]}, {"tags": ["other"]}]
        self.assertEqual(len(lp.filter_rows(rows, tags=["prior_24h"])), 1)

    def test_empty_selection_is_an_error(self):
        with self.assertRaises(lp.ParityError):
            self.run_fn(lambda t: [0.0] * len(t), tags=["nope"])

    def test_wrong_length_backend_rejected(self):
        with self.assertRaises(lp.ParityError):
            self.run_fn(lambda t: [0.0])

    def test_module_attr_backend(self):
        fn = lp.resolve_backend("tests.test_laya_parity:ADAPTER")
        self.assertEqual(fn(["a", "b"]), [0.25, 0.25])
        with self.assertRaises(lp.ParityError):
            lp.resolve_backend("nonexistent_mod_xyz:f")
        with self.assertRaises(lp.ParityError):
            lp.resolve_backend("bogus")


class _Obj:
    def score(self, texts):
        return [0.25] * len(texts)


ADAPTER = _Obj()


class IntegrityTests(Base):
    def test_sha_mismatch_refuses(self):
        write_fixtures(self.dir, sha="0" * 64)
        with self.assertRaises(lp.ParityError):
            lp.run_parity("replay", self.dir)
        code, _, err = self.cli("--backend", "replay")
        self.assertEqual(code, lp.EXIT_ERROR)
        self.assertIn("sha256 mismatch", err)

    def test_no_verify_overrides(self):
        write_fixtures(self.dir, sha="0" * 64)
        code, _, _ = self.cli("--backend", "replay", "--no-verify")
        self.assertEqual(code, 0)

    def test_real_v1_fixtures_verify_and_replay(self):
        if not os.path.exists(os.path.join(lp.DEFAULT_FIXTURE_DIR, "manifest.json")):
            self.skipTest("v1 fixtures not present")
        r = lp.run_parity("replay")
        self.assertEqual(r["metrics"]["agreement"]["overall"], 1.0)
        self.assertEqual(r["metrics"]["score_drift"]["max_abs_delta"], 0)


class BoundsAndCliTests(Base):
    def test_exit_codes(self):
        self.assertEqual(self.cli("--backend", "replay", "--min-agreement", "1.0",
                                  "--max-delta", "0")[0], 0)
        code, out, _ = self.cli("--backend", "constant:0.9", "--min-agreement", "0.99")
        self.assertEqual(code, lp.EXIT_BOUNDS)
        self.assertIn("FAIL", out)
        self.assertEqual(self.cli("--backend", "constant:0.9")[0], 0)  # no bound, no failure
        self.assertEqual(self.cli("--backend", "constant:0.9", "--max-delta", "0.1")[0], 1)
        self.assertEqual(self.cli("--backend", "constant:0.9", "--min-agreement", "0.5")[0], 0)
        self.assertEqual(self.cli("--backend", "nonsense")[0], lp.EXIT_ERROR)

    def test_undefined_metric_violates_its_bound(self):
        m = lp.run_parity("replay", self.dir, tags=["high"])["metrics"]
        self.assertEqual(m["agreement"]["near_threshold_n"], 0)
        self.assertTrue(lp.check_bounds(m, min_near_agreement=0.9))

    def test_out_and_summary_files(self):
        out, summ = os.path.join(self.dir, "r.json"), os.path.join(self.dir, "s.txt")
        code, stdout, _ = self.cli("--backend", "replay", "--out", out, "--summary", summ)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(json.load(open(out))["meta"]["backend"], "replay")
        self.assertIn("agreement:", open(summ).read())

    def test_script_entry_point(self):
        import subprocess
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                              "scripts", "laya.py")
        r = subprocess.run([sys.executable, script, "parity", "--backend", "replay",
                            "--fixtures", self.dir], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("laya parity", r.stdout)


class RowAwareBackendTests(Base):
    def test_backend_with_wants_rows_gets_matching_rows_and_report_lists_scores(self):
        seen = []

        def fn(texts, rows=None):
            seen.append([r["id"] for r in rows])
            return [0.5] * len(texts)
        fn.wants_rows = True
        with tempfile.TemporaryDirectory() as d:
            write_fixtures(d, SPEC)
            r = lp.run_parity("rows", d, batch_size=3, fn=fn)
        self.assertEqual(sum(len(x) for x in seen), len(SPEC))
        self.assertEqual([x["id"] for x in r["rows"]], [i for b in seen for i in b])
        self.assertEqual({x["score"] for x in r["rows"]}, {0.5})


class ScaleTests(unittest.TestCase):
    def test_2k_rows_replay_in_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            spec = [((i * 37 % 1000) / 1000.0, ["near_threshold"] if i % 3 == 0 else ["low"])
                    for i in range(2000)]
            write_fixtures(d, spec)
            t0 = time.time()
            r = lp.run_parity("replay", d, batch_size=64)
            self.assertLess(time.time() - t0, 5)
            self.assertEqual(r["metrics"]["agreement"]["overall"], 1.0)
            self.assertEqual(r["metrics"]["n"], 2000)


if __name__ == "__main__":
    unittest.main()
