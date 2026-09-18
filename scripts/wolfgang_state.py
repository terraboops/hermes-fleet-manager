#!/usr/bin/env python3
"""Deterministic state fingerprint for one Claude Code tmux session.

Used as a Hermes cron `monitor`: the agent only WAKES when this output changes.
While the session works steadily the output is constant, so the agent stays
asleep. It changes on a real transition: dead, went idle (needs input / stalled),
queued a message, or a new fleet-daemon event landed for it.

Deliberately contains NO timestamps / counters / dates — anything volatile makes
every tick look "changed" and defeats the gate.

Usage: wolfgang_state.py [session-name]
Exit:  always 0, prints one line.
"""
import hashlib
import re
import subprocess
import sys

SESS = sys.argv[1] if len(sys.argv) > 1 else "cc-w-wolfgang-1c2b"
LOG = "/Users/terra/.hermes/logs/fleet-watch.log"


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


def is_chrome(line):
    """Claude Code's status bar / chrome — volatile, must not enter the hash."""
    s = line.strip()
    if not s:
        return True
    if "✧" in s or "⏱" in s or "◈" in s:          # status bar segments
        return True
    if s.startswith("[━") or s.startswith("[─"):     # context bar
        return True
    if s.startswith("auto mode") or s.startswith("/rc") or s == "$":
        return True
    if re.match(r"^(shift\+tab|ctrl\+)", s):
        return True
    return False


def main():
    if subprocess.run(["tmux", "has-session", "-t", SESS],
                      capture_output=True).returncode != 0:
        print("DEAD")
        return

    pane = sh("tmux", "capture-pane", "-t", SESS, "-p", "-S", "-40")
    lines = [l.rstrip() for l in pane.splitlines()]
    content = [l for l in lines if not is_chrome(l)]
    blob = "\n".join(content)

    # --- classify ---------------------------------------------------------
    # Claude Code shows an interrupt hint while a turn is in flight, but only
    # sometimes — the busy state is otherwise a bare spinner word + ellipsis
    # ("Roosting…", "Sautéing…"), and a FINISHED turn looks like
    # "Roosting for 39s · done 4:22 PM". So: spinner present AND no done-marker.
    tail = [l for l in lines if l.strip()][-12:]
    spinner = any(
        re.match(r"^\s*[✻✽✳✢✷·⏺]?\s*[A-Za-z]{3,}(…|\.\.\.)\s*(\([^)]*\))?\s*$", l)
        for l in tail
    ) or any("esc to interrupt" in l for l in tail)
    finished = any("· done" in l for l in tail)
    working = spinner and not finished
    queued = ("Press up to edit queued" in pane) or ("queued messages" in pane)
    # A pending question / needs-input surface (approval gate, a question posed,
    # or Claude Code's own question UI: "Select with numbers [1-5]...").
    needs_input = bool(
        re.search(r"(?i)(\?\s*$|do you want|shall I|approve|y/n)", blob[-600:])
        or re.search(r"(?i)(Select with numbers|Then Enter to submit|answered Claude's questions)",
                     blob[-900:])
    )
    if working:
        state = "WORKING"
    elif queued:
        state = "QUEUED"
    elif needs_input:
        state = "NEEDS-INPUT"
    else:
        state = "IDLE"

    # --- newest daemon event for this session (deterministic: latest line) --
    ev = ""
    try:
        with open(LOG, "r", errors="replace") as fh:
            for ln in fh:
                if SESS in ln or "wolfgang" in ln:
                    ev = ln.strip()[-160:]
    except FileNotFoundError:
        pass

    # --- fingerprint ------------------------------------------------------
    # While WORKING, ignore pane content so steady progress does NOT wake the
    # agent. Only the state + daemon event matter. When it stops working, the
    # pending context is hashed so a NEW question/stall wakes it.
    if state == "WORKING":
        sig = f"{state}|{ev}"
    else:
        h = hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]
        sig = f"{state}|{h}|{ev}"
    print(sig)


if __name__ == "__main__":
    main()
