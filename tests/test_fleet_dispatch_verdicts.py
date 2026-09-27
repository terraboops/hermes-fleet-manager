"""Tests for fleet_dispatch.sh's verdicts, driven with a stubbed PATH and HOME.

The failure these exist to stop: the dispatcher read every non-zero answer from
the receipt check as "not delivered", so a session whose transcript could not be
read (exit 3 = UNKNOWN) was treated exactly like a genuine miss (exit 1) and the
payload was re-sent. A delivered message posted twice is worse than a miss.

The harness fakes `tmux`, the plugin's receipt check, and the transcript resolver,
so each verdict can be provoked deterministically without touching a real session.
"""
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCH = os.path.join(REPO, "scripts", "fleet_dispatch.sh")


def _write(path, text, executable=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class Harness:
    """A fake HOME + PATH around the real dispatcher."""

    def __init__(self, tmp, receipt_exits=None, pane_has_marker=False, receipt_after=None):
        self.tmp = tmp
        self.receipt_after = receipt_after
        self.pastes = os.path.join(tmp, "pastes.log")
        self.calls = os.path.join(tmp, "receipt-calls.log")
        open(self.pastes, "w").close()
        open(self.calls, "w").close()
        # 1..n exit codes for successive receipt calls; the last repeats.
        self.exits = [str(x) for x in (receipt_exits or [])]
        self.bin = os.path.join(tmp, "bin")
        self._fake_tmux(pane_has_marker)
        self._fake_plugins()

    def _fake_tmux(self, pane_has_marker):
        marker_line = 'printf "[%s]\\n" "$(cat %s)"' % (
            self.tmp + "/marker", self.tmp + "/marker") if False else ":"
        _write(os.path.join(self.bin, "tmux"), textwrap.dedent(f"""\
            #!/usr/bin/env bash
            case "$1" in
              has-session) exit 0 ;;
              load-buffer)
                # record what would be pasted: tmux load-buffer -b BUF FILE (file is LAST)
                cat "${{@: -1}}" >> "{self.pastes}"
                printf '\\n----\\n' >> "{self.pastes}"
                exit 0 ;;
              paste-buffer|send-keys) exit 0 ;;
              capture-pane)
                case "$*" in
                  *"-S -10"*)
                    # the pane genuinely shows the pasted block (including its marker)
                    if [ "{'1' if pane_has_marker else '0'}" = "1" ]; then
                      grep -oE 'DISPATCH-[A-Za-z0-9_.-]+-[0-9]+' "{self.pastes}" | tail -1 || true
                    fi ;;
                  *"-S -6"*) printf '❯ \\n' ;;
                  *"-S -8"*) printf '✻ Cooked for 1m 9s · done\\n❯ \\n' ;;
                  *) : ;;
                esac
                exit 0 ;;
              *) exit 0 ;;
            esac
        """))

    def _fake_plugins(self):
        ccw = os.path.join(self.tmp, ".hermes", "scripts", "cc-watch")
        # receipt check: walk the scripted exit codes, one per call
        after = self.receipt_after
        _write(os.path.join(ccw, "fleet_ack.py"), textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import os, sys, time
            calls = "{self.calls}"
            first = calls + ".first"
            if not os.path.exists(first):
                open(first, "w").write(str(time.time()))
            n = len(open(calls).read().splitlines())
            with open(calls, "a") as f:
                f.write(" ".join(sys.argv[1:]) + "\\n")
            after = {repr(after)}
            if after is not None:
                # time-based: absent for the whole first window, delivered after it
                elapsed = time.time() - float(open(first).read())
                sys.exit(0 if elapsed >= after else 1)
            exits = "{",".join(self.exits)}".split(",")
            code = int(exits[min(n, len(exits) - 1)])
            sys.exit(code)
        """))
        # transcript resolver: report a readable transcript unless told otherwise
        _write(os.path.join(ccw, "fleet_transcript.py"), textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import sys
            if len(sys.argv) > 2 and sys.argv[1] == "resolve":
                print("/tmp/fake.jsonl\\tuuid\\tok")
            sys.exit(0)
        """))
        _write(os.path.join(ccw, "fleet_registry.json"),
               json.dumps({"sessions": [{"name": "cc-w-test-1234", "short": "test",
                                         "uuid": "0" * 36}]}))

    def run(self, payload_text, extra_env=None):
        payload = os.path.join(self.tmp, "payload.txt")
        with open(payload, "w") as f:
            f.write(payload_text)
        env = dict(os.environ)
        env["HOME"] = self.tmp
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["FLEET_DELIVERY_TIMEOUT_S"] = "4"
        env.update(extra_env or {})
        r = subprocess.run(["bash", DISPATCH, "cc-w-test-1234", payload, "5"],
                           capture_output=True, text=True, env=env, timeout=90)
        return r

    def paste_count(self):
        with open(self.pastes) as f:
            return f.read().count("----")

    def receipt_call_count(self):
        with open(self.calls) as f:
            return len(f.read().splitlines())

    def pasted_text(self):
        with open(self.pastes) as f:
            return f.read()


class TestVerdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_delivered_first_try_reports_landed(self):
        h = Harness(self.tmp, [0])
        r = h.run("hello, please do the thing\n")
        self.assertIn("LANDED", r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(h.paste_count(), 1)

    def test_absent_then_delivered_reports_landed_retry(self):
        # absent for the WHOLE first window (that is what earns a retry), delivered after it
        h = Harness(self.tmp, receipt_after=5)
        r = h.run("hello, please do the thing\n")
        self.assertIn("LANDED-RETRY", r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(h.paste_count(), 2, "a definite miss is retried once")

    def test_unknown_is_not_a_miss_and_is_never_retried(self):
        """exit 3 = we could not read the transcript -> UNCERTAIN, one paste only."""
        h = Harness(self.tmp, [3])
        r = h.run("hello, please do the thing\n")
        self.assertIn("UNCERTAIN", r.stdout)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(h.paste_count(), 1,
                         "an unreadable transcript must never trigger a re-send")
        self.assertEqual(h.receipt_call_count(), 1,
                         "polling must stop on UNKNOWN rather than spinning to the deadline")

    def test_absent_and_not_in_pane_reports_not_landed(self):
        h = Harness(self.tmp, [1, 1])
        r = h.run("hello, please do the thing\n")
        self.assertIn("NOT-LANDED-AFTER-RETRY", r.stdout)
        self.assertEqual(r.returncode, 4)

    def test_absent_but_visible_in_pane_reports_not_submitted(self):
        h = Harness(self.tmp, [1, 1], pane_has_marker=True)
        r = h.run("hello, please do the thing\n")
        self.assertIn("NOT-SUBMITTED-AFTER-RETRY", r.stdout)
        self.assertEqual(r.returncode, 2)


class TestMarkerInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_payload_without_a_dispatch_id_gets_one_injected(self):
        """No marker in the payload used to fall back to 'first 40 chars of line 1'."""
        h = Harness(self.tmp, [0])
        h.run("a payload with no dispatch id at all\n")
        self.assertIn("DISPATCH-test-", h.pasted_text())

    def test_existing_dispatch_id_is_respected_not_duplicated(self):
        h = Harness(self.tmp, [0])
        h.run("payload\n[DISPATCH-test-1790000000000]\n")
        self.assertEqual(h.pasted_text().count("DISPATCH-test-1790000000000"), 1)

    def test_the_callers_file_is_never_modified(self):
        h = Harness(self.tmp, [0])
        payload = os.path.join(self.tmp, "payload.txt")
        with open(payload, "w") as f:
            f.write("original bytes, no marker\n")
        env = dict(os.environ)
        env["HOME"] = self.tmp
        env["PATH"] = os.path.join(self.tmp, "bin") + os.pathsep + env["PATH"]
        env["FLEET_DELIVERY_TIMEOUT_S"] = "4"
        subprocess.run(["bash", DISPATCH, "cc-w-test-1234", payload, "5"],
                       capture_output=True, text=True, env=env, timeout=90)
        self.assertEqual(open(payload).read(), "original bytes, no marker\n")


if __name__ == "__main__":
    unittest.main()
