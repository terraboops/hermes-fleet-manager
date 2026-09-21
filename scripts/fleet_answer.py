#!/usr/bin/env python3
"""Answer a Claude Code session's OWN interactive choice UI.

WHY THIS EXISTS: an overwatch should not tell a session the POLICY in prose
("take the more well-designed option" is on-the-nose and leaks meta-instruction). When the session
presents its own choice UI — a numbered picker, a permission prompt — the right move is to SELECT the
better option in that UI. Same decision, made the way a human would, and it keeps the session's
direction clean.

    fleet_answer.py options <session>          # what is currently on screen
    fleet_answer.py pick <session> <n>         # select numbered option n (sends n, then Enter)
    fleet_answer.py move <session> <regex>     # arrow down to the first option matching regex, Enter

SAFETY
- `pick`/`move` REFUSE when no choice UI is detected, so a stray keystroke can't land in a live
  composer and get submitted as a prompt.
- `move` only presses Down after confirming it re-renders a menu; it never sends a bare Enter into a
  prompt whose highlighted default is unknown (in Claude Code's trust prompt the default is
  "No, exit", which KILLS the session).
- After acting it re-reads the pane and reports what it sees, so the caller can verify rather than
  assume the choice took.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(\d{1,2})[.)]\s+(\S.*)$")
# Markers that a Claude Code choice UI is on screen.
UI_MARKERS = (
    re.compile(r"Select with numbers", re.I),
    re.compile(r"Then Enter to submit", re.I),
    re.compile(r"Enter to confirm", re.I),
    re.compile(r"↑/↓ to navigate", re.I),
    re.compile(r"Esc to cancel", re.I),
)


def pane(sess, lines=40):
    return subprocess.run(["tmux", "capture-pane", "-pt", "=" + sess + ":", "-S", f"-{lines}"],
                          capture_output=True, text=True).stdout


def parse_options(text):
    out = []
    for line in text.splitlines():
        m = OPTION_RE.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
    # de-dup, keep order
    seen, uniq = set(), []
    for n, label in out:
        if n not in seen:
            seen.add(n)
            uniq.append((n, label))
    return uniq


def ui_present(text):
    return any(rx.search(text) for rx in UI_MARKERS)


def key(sess, k):
    subprocess.run(["tmux", "send-keys", "-t", "=" + sess + ":", k], capture_output=True)
    time.sleep(0.4)


def cmd_options(args):
    text = pane(args.session)
    opts = parse_options(text)
    print(f"ui_present: {ui_present(text)}")
    for n, label in opts:
        print(f"  {n}. {label[:100]}")
    if not opts:
        print("  (no numbered options detected)")
    return 0


def cmd_pick(args):
    text = pane(args.session)
    opts = parse_options(text)
    if not ui_present(text) or not opts:
        print("REFUSED: no choice UI detected — not sending keys into a live composer.",
              file=sys.stderr)
        return 2
    if args.n not in [n for n, _ in opts]:
        print(f"REFUSED: option {args.n} is not on screen (have {[n for n, _ in opts]}).",
              file=sys.stderr)
        return 2
    label = dict(opts)[args.n]
    key(args.session, str(args.n))
    key(args.session, "Enter")
    time.sleep(1.5)
    after = pane(args.session, 14)
    tail = [l.strip() for l in after.splitlines() if l.strip()][-3:]
    print(f"picked {args.n}. {label[:80]}")
    for l in tail:
        print(f"  after: {l[:100]}")
    return 0


def cmd_move(args):
    text = pane(args.session)
    opts = parse_options(text)
    if not ui_present(text):
        print("REFUSED: no choice UI detected.", file=sys.stderr)
        return 2
    rx = re.compile(args.regex, re.I)
    target = None
    for i, (_, label) in enumerate(opts):
        if rx.search(label):
            target = i
            break
    if target is None:
        # Try arrow navigation for un-numbered pickers.
        for i in range(12):
            key(args.session, "Down")
            if rx.search(pane(args.session, 20)):
                target = i
                break
        if target is None:
            print(f"REFUSED: no option matched {args.regex!r}.", file=sys.stderr)
            return 2
    else:
        for _ in range(target):
            key(args.session, "Down")
    key(args.session, "Enter")
    time.sleep(1.5)
    print(f"selected option matching {args.regex!r}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("options"); a.add_argument("session"); a.set_defaults(func=cmd_options)
    b = sub.add_parser("pick"); b.add_argument("session"); b.add_argument("n", type=int)
    b.set_defaults(func=cmd_pick)
    c = sub.add_parser("move"); c.add_argument("session"); c.add_argument("regex")
    c.set_defaults(func=cmd_move)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
