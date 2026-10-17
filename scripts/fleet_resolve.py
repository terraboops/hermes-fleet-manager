#!/usr/bin/env python3
"""The sanctioned way for an overwatch to answer a session or to raise a question to the operator.

    fleet_resolve.py show <session>
    fleet_resolve.py answer <session> --cite focus:L12 --text "<that line, quoted>"
    fleet_resolve.py escalate <session> --situation "..." [--recommendation "..."] [--default "..."]

`answer` sends a decision the operator wrote into the session's focus file. It refuses unless the cite
names a line of that file and the text is that line, so an answer cannot be composed. `escalate`
queues a page for the daemon, which is the only thing allowed to page. Nothing here writes to the chat.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
import clarify     # noqa: E402
import escalation  # noqa: E402
import funnel      # noqa: E402

DISPATCH = os.path.join(os.path.expanduser("~/.hermes/scripts/cc-watch"), "fleet_dispatch.sh")


def main(argv):
    ap = argparse.ArgumentParser(prog="fleet_resolve.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("session")
    a = sub.add_parser("answer"); a.add_argument("session"); a.add_argument("--cite", required=True)
    a.add_argument("--text", required=True)
    e = sub.add_parser("escalate"); e.add_argument("session"); e.add_argument("--situation", required=True)
    e.add_argument("--recommendation", default=""); e.add_argument("--default", default="")
    args = ap.parse_args(argv)

    if args.cmd == "show":
        print("decisions on record for %s (citable):" % args.session)
        for n, line in clarify.focus_lines(args.session):
            if clarify.DECISION.search(line) and not clarify._pending_re().search(line):
                print("  focus:L%d  %s" % (n, line[:220]))
        return 0
    if args.cmd == "escalate":
        funnel.request_page(args.session, args.situation, args.recommendation, args.default)
        print("queued; the daemon pages on its next tick")
        return 0
    if not clarify.cite_ok(args.session, args.cite, args.text):
        print("REFUSED: %s is not a line of %s's focus file, or the text is not that line" % (args.cite, args.session))
        return 1
    shape = clarify.gated_shape(args.text)
    if shape:
        print("REFUSED: the text carries a gated shape (%s)" % shape)
        return 1
    text = clarify.FRAME.format(line=" ".join(args.text.split()))
    tmp = os.path.expanduser("~/.hermes/cache/scratch/resolve-%s-%d.txt" % (args.session, os.getpid()))
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    open(tmp, "w").write(text)
    try:
        r = subprocess.run(["bash", DISPATCH, args.session, tmp], capture_output=True, text=True, timeout=120)
    finally:
        os.remove(tmp)
    landed = "LANDED" in (r.stdout or "")
    escalation.record_attempt(args.session, "", "NEEDS-INPUT", "answer", text, grounded=True,
                              cite=args.cite, delivered="LANDED" if landed else "FAILED", via="overwatch")
    print("LANDED" if landed else "NOT DELIVERED: %s" % (r.stdout or r.stderr).strip()[-200:])
    return 0 if landed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
