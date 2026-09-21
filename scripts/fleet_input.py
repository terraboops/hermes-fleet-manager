#!/usr/bin/env python3
"""Read ONLY the input box of a Claude Code session -- the one permitted tmux read.

Why this is the only read left: the pane's scrollback is stale and its superseded
questions make an idle session look blocked, and its glyphs cannot prove delivery. The
input box is different -- an unsent draft exists nowhere else, so it is the one thing the
log cannot tell you. This targets it exactly rather than guessing at a row count.

How: Claude renders the composer as a bordered box, and the terminal cursor sits inside
it. Find the cursor row, expand to the nearest full-width border above and below, and the
region between them is the composer, verbatim. Everything above is scrollback; everything
below is status chrome.

Detecting a MENU matters as much as reading text. A numbered choice (a permission prompt,
the trust dialog) also renders with the '❯' marker, so a naive read calls it input -- and
the dispatch path then clears it with Ctrl-C, cancelling the prompt or ending the session.

Usage:
  fleet_input.py <session>            # JSON: text, empty, menu, cursor_row
  fleet_input.py <session> --clear    # empty the composer, but ONLY if it holds text
"""
import json
import re
import subprocess
import sys

BORDER_CHARS = set("─═━┄┈")            # box-drawing runs used as borders
MENU_RE = re.compile(r"^\s*❯\s*\d+\s*[.)]\s")   # a numbered option, i.e. a menu, not input


def run(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def parse_input(rows, cy):
    """Pure: given pane rows and the cursor row, return the input box's contents.

    The box is the region between the border rows that enclose the cursor. Everything
    above it is scrollback and everything below is status chrome, so neither can leak in.
    A numbered option with the same '❯' marker means a MENU is up, which is not input.
    """
    if not rows:
        return {"error": "no pane output"}

    def is_border(i):
        if not (0 <= i < len(rows)):
            return False
        s = rows[i].strip()
        return bool(s) and len(set(s)) == 1 and s[0] in BORDER_CHARS

    top = next((i for i in range(cy, -1, -1) if is_border(i)), None)
    bottom = next((i for i in range(cy, len(rows)) if is_border(i)), None)

    if top is None or bottom is None or bottom <= top:
        # No box found: report the cursor row alone rather than inventing a region.
        line = rows[cy] if 0 <= cy < len(rows) else ""
        return {"cursor_row": cy, "menu": bool(MENU_RE.match(line)),
                "text": line.strip(), "empty": not line.strip(), "box_found": False}

    body = rows[top + 1:bottom]
    menu = any(MENU_RE.match(r) for r in body)
    text = "\n".join(re.sub(r"^\s*❯\s?", "", r).rstrip() for r in body)
    return {"cursor_row": cy, "menu": menu, "box_found": True,
            "text": text, "empty": not text.strip(), "top": top, "bottom": bottom}


def clear_decision(info):
    """Pure: may we clear the composer, and why or why not.

    Ctrl-C is only safe against a draft that holds text. Against an open menu it cancels
    the prompt, and on the trust dialog it can end the session -- so refusing is the
    behaviour that matters, not the clearing.
    """
    if "error" in info:
        return False, "cannot read the pane"
    if info.get("menu"):
        return False, "menu is open; refuse to send Ctrl-C into it"
    if info.get("empty"):
        return False, "input box already empty"
    return True, "draft holds text"


def read_input(session):
    t = "=" + session + ":"
    pos = run("display-message", "-p", "-t", t, "#{cursor_y}").stdout.strip()
    rows = run("capture-pane", "-t", t, "-p").stdout.rstrip("\n").split("\n")
    if not rows or rows == [""]:
        return {"error": "no pane output", "session": session}
    try:
        cy = int(pos)
    except ValueError:
        cy = len(rows) - 1
    out = {"session": session}
    out.update(parse_input(rows, cy))
    return out


def main():
    argv = sys.argv[1:]
    if not argv:
        print((__doc__ or "").strip())
        return 2
    s = argv[0]
    info = read_input(s)
    if "--clear" in argv:
        may, why = clear_decision(info)
        info["reason"] = why
        if not may:
            info["cleared"] = False
        else:
            run("send-keys", "-t", "=" + s + ":", "C-c")
            after = read_input(s)
            info["cleared"] = bool(after.get("empty"))
            info["after"] = after.get("text") or ""
    info["ok"] = "error" not in info
    print(json.dumps(info, indent=2))
    return 0 if info["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
