"""Tests for fleet_dispatch.sh's verdicts and its composer-draft lifecycle.

Two failures these exist to stop:

1. The dispatcher read every non-zero answer from the receipt check as "not
   delivered", so a session whose transcript could not be read (exit 3 = UNKNOWN)
   was treated like a genuine miss (exit 1) and the payload was re-sent. A
   delivered message posted twice is worse than a miss.

2. Clearing the composer before a paste DESTROYED an operator's unsubmitted draft.
   Terra's instruction: "it should save whatever is in the input, clear it, send the
   message, verify sent, restore it, and verify restore."

The harness fakes tmux (including a tiny composer model), the receipt check, the
transcript resolver and the composer reader, so each behaviour is provoked
deterministically without touching a real session.
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

    def __init__(self, tmp, receipt_exits=None, pane_has_marker=False, receipt_after=None,
                 composer="", menu=False, corrupt_restore=False, enter_submits=True):
        self.tmp = tmp
        self.pastes = os.path.join(tmp, "pastes.log")
        self.calls = os.path.join(tmp, "receipt-calls.log")
        self.composer = os.path.join(tmp, "composer.txt")
        self.loaded = os.path.join(tmp, "loaded.txt")
        self.after_read = os.path.join(tmp, "after_read.count")
        open(self.pastes, "w").close()
        open(self.calls, "w").close()
        with open(self.composer, "w") as f:
            f.write(composer)
        with open(self.loaded, "w") as f:
            f.write("")
        with open(self.after_read, "w") as f:
            f.write("0")
        self.exits = [str(x) for x in (receipt_exits or [])]
        self.receipt_after = receipt_after
        self.corrupt_restore = corrupt_restore
        self.menu = menu
        self.enter_submits = enter_submits
        self.bin = os.path.join(tmp, "bin")
        self._fake_tmux(pane_has_marker, menu)
        self._fake_plugins()

    def _fake_tmux(self, pane_has_marker, menu):
        _write(os.path.join(self.bin, "tmux"), textwrap.dedent(f"""\
            #!/usr/bin/env bash
            case "$1" in
              has-session) exit 0 ;;
              load-buffer)
                # tmux load-buffer -b BUF FILE  -> the file is the LAST argument
                cat "${{@: -1}}" >> "{self.pastes}"
                printf '\\n----\\n' >> "{self.pastes}"
                cp "${{@: -1}}" "{self.loaded}"
                exit 0 ;;
              paste-buffer)
                # a paste puts the loaded text in the composer
                cp "{self.loaded}" "{self.composer}"
                exit 0 ;;
              send-keys)
                # Enter submits, so the composer empties ... unless the test says the
                # Enter was swallowed (the NOT-SUBMITTED case)
                if [ "{'1' if self.enter_submits else '0'}" = "1" ]; then
                  case "$*" in *Enter*) : > "{self.composer}" ;; esac
                fi
                exit 0 ;;
              capture-pane)
                case "$*" in
                  *"-S -10"*)
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
                elapsed = time.time() - float(open(first).read())
                sys.exit(0 if elapsed >= after else 1)
            exits = "{",".join(self.exits)}".split(",")
            code = int(exits[min(n, len(exits) - 1)])
            sys.exit(code)
        """))
        _write(os.path.join(ccw, "fleet_transcript.py"), textwrap.dedent("""\
            #!/usr/bin/env python3
            import sys
            if len(sys.argv) > 2 and sys.argv[1] == "resolve":
                print("/tmp/fake.jsonl\\tuuid\\tok")
            sys.exit(0)
        """))
        # The composer reader: --save-draft copies the model composer out; --clear empties it.
        # The second read of a run is corrupted when the test asks for that.
        _write(os.path.join(ccw, "fleet_input.py"), textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, shutil, sys
            composer = "{self.composer}"
            count_file = "{self.after_read}"
            argv = sys.argv[1:]
            text = open(composer).read() if os.path.exists(composer) else ""
            if "--save-draft" in argv:
                path = argv[argv.index("--save-draft") + 1]
                n = int(open(count_file).read() or "0")
                open(count_file, "w").write(str(n + 1))
                out = text
                # read #1 = the draft, #2 = the composer before the restore paste,
                # #3 = after it. Corrupting #3 models a restore that did not take.
                if n >= 2 and {1 if self.corrupt_restore else 0}:
                    out = "something else entirely"
                open(path, "w").write(out)
            if "--clear" in argv:
                open(composer, "w").write("")
            print(json.dumps({{"text": text, "empty": not text,
                               "menu": {repr(bool(self.menu))}}}, indent=2))
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
        env["FLEET_DRAFT_STASH"] = os.path.join(self.tmp, "drafts.log")
        env.update(extra_env or {})
        return subprocess.run(["bash", DISPATCH, "cc-w-test-1234", payload, "5"],
                              capture_output=True, text=True, env=env, timeout=90)

    def paste_count(self):
        with open(self.pastes) as f:
            return f.read().count("----")

    def receipt_call_count(self):
        with open(self.calls) as f:
            return len(f.read().splitlines())

    def pasted_text(self):
        with open(self.pastes) as f:
            return f.read()

    def composer_text(self):
        with open(self.composer) as f:
            return f.read()

    def stash(self):
        p = os.path.join(self.tmp, "drafts.log")
        return open(p).read() if os.path.exists(p) else ""


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
        h = Harness(self.tmp, receipt_after=5)
        r = h.run("hello, please do the thing\n")
        self.assertIn("LANDED-RETRY", r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(h.paste_count(), 2, "a definite miss is retried once")

    def test_unknown_is_not_a_miss_and_is_never_retried(self):
        h = Harness(self.tmp, [3])
        r = h.run("hello, please do the thing\n")
        self.assertIn("UNCERTAIN", r.stdout)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(h.paste_count(), 1,
                         "an unreadable transcript must never trigger a re-send")
        self.assertEqual(h.receipt_call_count(), 1,
                         "polling must stop on UNKNOWN rather than spinning to the deadline")

    def test_absent_and_not_in_pane_reports_not_landed(self):
        h = Harness(self.tmp, [1])
        r = h.run("hello, please do the thing\n")
        self.assertIn("NOT-LANDED-AFTER-RETRY", r.stdout)
        self.assertEqual(r.returncode, 4)

    def test_absent_but_visible_in_pane_reports_not_submitted(self):
        h = Harness(self.tmp, [1], pane_has_marker=True, enter_submits=False)
        r = h.run("hello, please do the thing\n")
        self.assertIn("NOT-SUBMITTED-AFTER-RETRY", r.stdout)
        self.assertEqual(r.returncode, 2)


class TestMarkerInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_payload_without_a_dispatch_id_gets_one_injected(self):
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
        with open(payload) as f:
            self.assertEqual(f.read(), "original bytes, no marker\n")


class TestComposerDraftLifecycle(unittest.TestCase):
    """Terra: save what is in the input, clear it, send, verify sent, restore, verify restore."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_human_draft_is_restored_after_the_dispatch(self):
        h = Harness(self.tmp, [0], composer="cut the CLI release")
        r = h.run("the dispatched payload\n")
        self.assertIn("LANDED", r.stdout)
        self.assertIn("DRAFT-STASHED: cut the CLI release", r.stdout)
        self.assertIn("DRAFT-RESTORED", r.stdout)
        self.assertEqual(h.composer_text(), "cut the CLI release",
                         "the operator's draft must be back in the composer")
        self.assertIn("cut the CLI release", h.stash(), "and recoverable from the stash")
        self.assertEqual(h.paste_count(), 2, "payload + restored draft")

    def test_a_draft_is_restored_even_when_the_dispatch_fails(self):
        h = Harness(self.tmp, [1], composer="ssh into the ERX and run those three commands")
        r = h.run("the dispatched payload\n")
        self.assertIn("NOT-LANDED", r.stdout)
        self.assertIn("DRAFT-RESTORED", r.stdout)
        self.assertEqual(h.composer_text(), "ssh into the ERX and run those three commands")

    def test_a_menu_is_never_cleared_and_no_keys_are_sent(self):
        h = Harness(self.tmp, [0], composer="keep me", menu=True)
        r = h.run("the dispatched payload\n")
        self.assertIn("MENU-BLOCKED", r.stdout)
        self.assertEqual(r.returncode, 5)
        self.assertEqual(h.paste_count(), 0, "no paste may happen into a choice UI")
        self.assertEqual(h.composer_text(), "keep me", "the menu's text is left alone")

    def test_an_unverifiable_restore_is_reported_not_hidden(self):
        h = Harness(self.tmp, [0], composer="a draft that comes back wrong",
                    corrupt_restore=True)
        r = h.run("the dispatched payload\n")
        self.assertIn("DRAFT-RESTORE-FAILED", r.stdout)
        self.assertIn("a draft that comes back wrong", h.stash())

    def test_no_draft_means_no_draft_noise(self):
        h = Harness(self.tmp, [0])
        r = h.run("the dispatched payload\n")
        self.assertNotIn("DRAFT-", r.stdout)
        self.assertEqual(h.paste_count(), 1)

    def test_a_draft_is_not_restored_over_our_own_unsent_payload(self):
        h = Harness(self.tmp, [1], composer="an operator draft", pane_has_marker=True,
                    enter_submits=False)
        r = h.run("the dispatched payload\n")
        self.assertIn("DRAFT-NOT-RESTORED-COMPOSER-OCCUPIED", r.stdout)


if __name__ == "__main__":
    unittest.main()
