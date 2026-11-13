import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts/codex_dispatch.py"
spec = importlib.util.spec_from_file_location("codex_dispatch", MODULE_PATH)
assert spec is not None and spec.loader is not None
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)


class CodexDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.payload = self.root / "payload.txt"
        self.payload.write_text("Check this read-only item.")
        self.registry = self.root / "fleet_registry.json"
        self.state = self.root / "fleet_state.py"
        self.ack = self.root / "fleet_ack.py"
        self.entry = {"name": "cc-w-canary", "short": "canary", "provider": "codex",
                      "uuid": "01a17a5a-100f-78f3-92f4-65802cee3c72",
                      "codex_home": str(self.root / "codex"), "cwd": str(self.cwd)}
        self.write_registry()
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def write_registry(self):
        self.registry.write_text(json.dumps({"sessions": [self.entry]}))

    def runner(self, answers):
        def call(command, **kwargs):
            self.calls.append((command, kwargs))
            return answers.pop(0)
        return call

    def test_queues_native_thread_and_requires_transcript_receipt(self):
        answers = [subprocess.CompletedProcess([], 0, "IDLE|uuid|assistant", ""),
                   subprocess.CompletedProcess([], 0,
                       "Queued message 01a17afe-d778-7730-8682-70e47ead4457 for thread "
                       + self.entry["uuid"] + ".", ""),
                   subprocess.CompletedProcess([], 0, "DELIVERED", "")]
        result = cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                             state_path=self.state, ack_path=self.ack, runner=self.runner(answers))
        self.assertTrue(result.startswith("LANDED DISPATCH-canary-"))
        cmd, kwargs = self.calls[1]
        self.assertEqual(cmd[:4], ["codex", "queue", "--thread", self.entry["uuid"]])
        self.assertEqual(cmd[-2:], ["--cd", str(self.cwd)])
        self.assertIn("Check this read-only item.", cmd[5])
        self.assertIn(result.split()[1], cmd[5])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.root / "codex"))
        self.assertEqual(self.payload.read_text(), "Check this read-only item.")
        self.assertEqual(len(self.calls), 3)

    def test_needs_input_stops_before_queue(self):
        runner = self.runner([subprocess.CompletedProcess([], 0, "NEEDS-INPUT|uuid|user", "")])
        with self.assertRaisesRegex(ValueError, "NOT-READY"):
            cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                        state_path=self.state, ack_path=self.ack, runner=runner)
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_queue_result_is_never_retried(self):
        runner = self.runner([subprocess.CompletedProcess([], 0, "IDLE|uuid", ""),
                              subprocess.CompletedProcess([], 0, "queued", "")])
        with self.assertRaisesRegex(ValueError, "UNCERTAIN"):
            cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                        state_path=self.state, ack_path=self.ack, runner=runner)
        self.assertEqual(len(self.calls), 2)

    def test_unreadable_receipt_is_uncertain_not_a_retry(self):
        runner = self.runner([subprocess.CompletedProcess([], 0, "WORKING|uuid", ""),
                              subprocess.CompletedProcess([], 0,
                                  "Queued message 01a17afe-d778-7730-8682-70e47ead4457 for thread "
                                  + self.entry["uuid"] + ".", ""),
                              subprocess.CompletedProcess([], 3, "UNKNOWN", "")])
        with self.assertRaisesRegex(ValueError, "UNCERTAIN"):
            cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                        state_path=self.state, ack_path=self.ack, runner=runner)
        self.assertEqual(len(self.calls), 3)

    def test_large_valid_payload_below_limit_is_queued(self):
        body = "x" * (cd.MAX_MESSAGE_BYTES - 100)
        self.payload.write_text(body)
        answers = [subprocess.CompletedProcess([], 0, "IDLE|uuid|assistant", ""),
                   subprocess.CompletedProcess([], 0,
                       "Queued message 01a17afe-d778-7730-8682-70e47ead4457 for thread "
                       + self.entry["uuid"] + ".", ""),
                   subprocess.CompletedProcess([], 0, "DELIVERED", "")]
        result = cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                             state_path=self.state, ack_path=self.ack, runner=self.runner(answers))
        self.assertTrue(result.startswith("LANDED DISPATCH-canary-"))
        message = self.calls[1][0][5]
        self.assertTrue(message.startswith(body))
        self.assertLessEqual(len(message.encode()), cd.MAX_MESSAGE_BYTES)

    def test_large_payload_fails_closed_before_state_or_queue(self):
        self.payload.write_text("x" * (cd.MAX_MESSAGE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "CODEX-PAYLOAD-TOO-LARGE"):
            cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                        state_path=self.state, ack_path=self.ack, runner=self.runner([]))
        self.assertEqual(self.calls, [])

    def test_only_explicit_codex_provider_uses_adapter(self):
        self.entry.pop("provider")
        self.write_registry()
        with self.assertRaisesRegex(ValueError, "not explicitly registered"):
            cd.dispatch("cc-w-canary", self.payload, registry_path=self.registry,
                        state_path=self.state, ack_path=self.ack, runner=self.runner([]))
        self.assertEqual(self.calls, [])


class FleetDispatchRoutingTests(unittest.TestCase):
    def test_codex_provider_routes_away_from_claude_composer_path(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp)
            ccw = home / ".hermes/scripts/cc-watch"
            ccw.mkdir(parents=True)
            (ccw / "fleet_registry.json").write_text(json.dumps({"sessions": [
                {"name": "cc-w-canary", "provider": "codex"}]}))
            log = home / "codex-adapter.log"
            helper = ccw / "codex_dispatch.py"
            helper.write_text(f"import pathlib,sys\npathlib.Path({str(log)!r}).write_text(' '.join(sys.argv[1:]))\nprint('LANDED')\n")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            bindir = home / "bin"
            bindir.mkdir()
            tmux = bindir / "tmux"
            tmux.write_text("#!/bin/sh\n[ \"$1\" = has-session ]\n")
            tmux.chmod(tmux.stat().st_mode | stat.S_IXUSR)
            payload = home / "payload.txt"
            payload.write_text("task")
            env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
            result = subprocess.run(["bash", str(REPO / "scripts/fleet_dispatch.sh"),
                                     "cc-w-canary", str(payload)], capture_output=True,
                                    text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LANDED", result.stdout)
            self.assertEqual(log.read_text(), f"cc-w-canary {payload} 60")


if __name__ == "__main__":
    unittest.main()
