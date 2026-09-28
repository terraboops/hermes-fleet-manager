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

Detecting GHOST TEXT matters for the same reason. When the composer is EMPTY, Claude Code
renders a dimmed SUGGESTED next prompt after the '❯' marker, which reads exactly like a
human draft -- and the dispatch path's stale-draft guard clears it. The suggestion usually
echoes the session's own last offer, so it looks like a plausible human instruction and
invites a fabricated "her message never arrived" story. `dim`/`ghost` report the discriminator:
the visible text is wrapped in the dim SGR (ESC[2m).

Usage:
  fleet_input.py <session>            # JSON: text, empty, menu, dim, ghost, cursor_row
  fleet_input.py <session> --clear    # empty the composer, but ONLY if it holds text
"""
import json
import re
import subprocess
import sys

BORDER_CHARS = set("─═━┄┈")            # box-drawing runs used as borders
MENU_RE = re.compile(r"^\s*❯\s*\d+\s*[.)]\s")   # a numbered option, i.e. a menu, not input
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DIM = "\x1b[2m"


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
        if not s:
            return False
        n = sum(1 for ch in s if ch in BORDER_CHARS)
        # A composer border is NOT always one repeated glyph: Claude labels the top edge
        # ("───── maintenance ─────"), and that label made the strict `len(set(s)) == 1`
        # test fail -- so the box was never found, the cursor row was reported alone, and
        # dim/ghost fell back to False, which is exactly how a ghost suggestion gets
        # mistaken for a human draft. Require border glyphs to DOMINATE the row instead.
        return n >= 8 and n >= 0.75 * len(s)

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


def detect_dim(esc_rows, top, bottom):
    """Pure: is the composer's visible text rendered DIM (i.e. a ghost suggestion)?

    Claude Code draws an empty composer's suggested next prompt in the dim SGR, so the
    dimension of the glyphs -- not their presence -- is what separates a suggestion from
    something a person typed. Rows must come from `capture-pane -e`; without escapes there
    is nothing to measure, so an absent/blank region reports False rather than guessing.
    """
    if top is None or bottom is None or bottom <= top:
        body = esc_rows
    else:
        body = esc_rows[top + 1:bottom]
    for row in body:
        visible = ANSI_RE.sub("", row).strip()
        if not visible or visible.startswith("─"):
            continue
        return DIM in row
    return False


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
    esc_rows = run("capture-pane", "-t", t, "-p", "-e").stdout.rstrip("\n").split("\n")
    try:
        cy = int(pos)
    except ValueError:
        cy = len(rows) - 1
    out = {"session": session}
    out.update(parse_input(rows, cy))
    # Ghost text is the composer's SUGGESTED prompt drawn on an EMPTY composer: it holds
    # visible text, so `empty` is false, but it is not anything a person typed.
    if not out.get("box_found"):
        # Without a located box there is nothing to measure: detect_dim would scan the whole
        # pane and confidently answer "not dim" about some scrollback row, which reads as a
        # REAL draft. Report UNKNOWN (null) instead -- falsy, so the dispatch path declines
        # to clear, and an operator sees the uncertainty rather than a false negative.
        out["dim"] = out["ghost"] = None
    else:
        dim = detect_dim(esc_rows, out.get("top"), out.get("bottom"))
        out["dim"] = dim
        out["ghost"] = bool(dim and not out.get("menu"))
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
    # --save-draft PATH: write the composer's text verbatim so a caller can put it BACK
    # after using the composer for its own paste. Read-only; no keys are sent.
    if "--save-draft" in argv:
        i = argv.index("--save-draft")
        if i + 1 >= len(argv):
            info["error"] = "--save-draft needs a path"
        else:
            path = argv[i + 1]
            try:
                with open(path, "w") as f:
                    f.write(info.get("text") or "")
                info["saved"] = path
                info["saved_bytes"] = len(info.get("text") or "")
            except OSError as e:
                info["error"] = f"cannot write {path}: {e}"
    info["ok"] = "error" not in info
    print(json.dumps(info, indent=2))
    return 0 if info["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
