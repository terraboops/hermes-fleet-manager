#!/usr/bin/env python3
"""Deterministic STATE FINGERPRINT for one Claude Code tmux session.

Engine behind the fleet OVERWATCH (see `fleet_overwatch.py`). Used as a Hermes
cron `monitor`: the agent only WAKES when this output changes.

While the session works steadily the output is constant, so the overwatch agent
stays asleep — near-zero token burn instead of an agent session every tick. It
changes on a real transition: the session died, went idle (needs input / parked),
queued a message, or a new fleet-daemon event landed for it.

Deliberately contains NO timestamps / counters / dates — anything volatile makes
every tick look "changed" and defeats the gate entirely.

TWO SIGNALS, and the second is not optional (2026-09-21). The pane is the INPUT
channel: whether a turn is running, whether the input box holds a draft, a menu or
a parked shell line. It does NOT show whether the session is still producing
output — a finished turn renders "· done" with no spinner, so pane-only
classification called a session IDLE while it was mid-publish and still writing.
The session's own transcript IS the output channel, so a transcript written to
within ACTIVITY_WINDOW seconds counts as WORKING even with no spinner. Without
this, every finished-but-still-working session reads IDLE.

Usage:  fleet_state.py <tmux-session-name>
Exit:   always 0 unless the session arg is missing (prints one line to stdout).
"""
import hashlib
import os
import re
import subprocess
import sys
import time

LOG = os.path.expanduser("~/.hermes/logs/fleet-watch.log")


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


REGISTRY = os.environ.get(
    "FLEET_REGISTRY",
    os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_registry.json"))
ACTIVITY_WINDOW = float(os.environ.get("FLEET_ACTIVITY_WINDOW", "180"))


def transcript_of(sess):
    """The session's own jsonl, via the registry: <config_dir>/projects/*/<uuid>.jsonl.

    Returns "" when the session is unknown or unregistered — the caller then falls
    back to pane-only classification rather than guessing.
    """
    import glob
    import json
    try:
        reg = json.load(open(os.path.expanduser(REGISTRY)))
    except Exception:
        return ""
    entries = reg.get("sessions", reg if isinstance(reg, list) else [])
    for e in entries:
        if sess not in (e.get("name"), e.get("short")):
            continue
        cfg = os.path.expanduser(str(e.get("config_dir") or ""))
        uid = e.get("uuid") or ""
        if not (cfg and uid):
            return ""
        hits = glob.glob(os.path.join(cfg, "projects", "*", f"{uid}.jsonl"))
        return max(hits, key=os.path.getmtime) if hits else ""
    return ""


def written_recently(path, window=None):
    """True when the transcript grew within `window` seconds: output is still landing."""
    window = ACTIVITY_WINDOW if window is None else window
    try:
        return bool(path) and (time.time() - os.path.getmtime(path)) < window
    except OSError:
        return False


def event_age(sess):
    """Seconds since the newest fleet-watch log line for this session, or None.

    The daemon's own log is the second output surface: a session emits its sentinel
    tokens and lifecycle events there, and those writes land the moment a turn
    finishes — often BEFORE and INSTEAD OF any transcript growth. A transcript-only
    activity signal therefore still misses the case that started this: a session
    that had just emitted its token and was mid-publish. Written to the log = not
    idle, whether or not the pane shows a spinner.
    """
    try:
        newest = None
        with open(LOG, "r", errors="replace") as fh:
            for ln in fh:
                if sess in ln:
                    newest = ln
        if not newest:
            return None
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", newest.strip())
        if not m:
            return None
        stamped = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        return max(0.0, time.time() - stamped)
    except Exception:
        return None


def is_chrome(line):
    """Claude Code's status bar / chrome — volatile, must not enter the hash."""
    s = line.strip()
    if not s:
        return True
    if "✧" in s or "⏱" in s or "◈" in s:            # status bar segments
        return True
    if s.startswith("[━") or s.startswith("[─"):      # context bar
        return True
    if s.startswith("auto mode") or s.startswith("/rc") or s == "$":
        return True
    if re.match(r"^(shift\+tab|ctrl\+)", s):
        return True
    return False


def fingerprint(sess, heartbeat=0):
    if subprocess.run(["tmux", "has-session", "-t", sess],
                      capture_output=True).returncode != 0:
        return "DEAD"

    pane = sh("tmux", "capture-pane", "-t", sess, "-p", "-S", "-40")
    lines = [l.rstrip() for l in pane.splitlines()]
    content = [l for l in lines if not is_chrome(l)]
    blob = "\n".join(content)

    # --- classify ---------------------------------------------------------
    # The pane's "esc to interrupt" hint is only SOMETIMES present, and the tail
    # always carries the status bar. A busy turn is otherwise a bare spinner word
    # + ellipsis ("Roosting…", "Mulling…"), while a FINISHED turn renders
    # "Roosting for 39s · done 4:22 PM". So: spinner present AND no done-marker.
    #
    # DO NOT enumerate spinner glyphs. Claude Code CYCLES through them (observed
    # ✻ ✽ ✳ ✢ ✶ ✷ ⏺ ·) and an unknown glyph silently reads as IDLE — a false IDLE
    # on a WORKING session changes the hash every tick, waking the overwatch and
    # causing nudge churn on a session that is fine. Match ANY leading glyph and
    # keep a length guard so ordinary prose ending in "..." cannot match.
    tail = [l for l in lines if l.strip()][-12:]
    spinner = any(
        len(l.strip()) < 80
        and re.match(r"^\s*[^A-Za-z0-9]{0,4}\s*[A-Za-z]{3,}(…|\.\.\.)\s*(\(.*\))?\s*$", l)
        for l in tail
    ) or any("esc to interrupt" in l for l in tail)
    finished = any("· done" in l for l in tail)
    working = spinner and not finished
    queued = ("Press up to edit queued" in pane) or ("queued messages" in pane)
    # A pending question / needs-input surface (approval gate, a posed question, or
    # Claude Code's own question UI: "Select with numbers [1-5]...").
    needs_input = bool(
        re.search(r"(?i)(\?\s*$|do you want|shall I|approve|y/n)", blob[-600:])
        or re.search(r"(?i)(Select with numbers|Then Enter to submit|answered Claude's questions)",
                     blob[-900:])
    )

    # The pane cannot see output that is still being written. A finished turn
    # renders "· done" with no spinner, so this is precisely where IDLE was being
    # reported for a session that was mid-publish. Either output surface counts:
    # the session's transcript, and the daemon's own log. EITHER signal means the
    # session is working.
    tp = transcript_of(sess)
    ea = event_age(sess)
    active = written_recently(tp) or (ea is not None and ea < ACTIVITY_WINDOW)

    if working or active:
        state = "WORKING"
    elif queued:
        state = "QUEUED"
    elif needs_input:
        state = "NEEDS-INPUT"
    else:
        state = "IDLE"

    # --- newest daemon event for this session (deterministic: latest line) ---
    ev = ""
    try:
        with open(LOG, "r", errors="replace") as fh:
            for ln in fh:
                if sess in ln:
                    ev = ln.strip()[-160:]
    except FileNotFoundError:
        pass

    # --- fingerprint ------------------------------------------------------
    # While WORKING, ignore pane content so steady progress does NOT wake the
    # agent. When it stops working, hash the pending context so a NEW question or
    # stall wakes it.
    #
    # HEARTBEAT: with no heartbeat a session that is BUSY FOREVER (wedged turn, a
    # loop, a 2-hour "Computing…") produces a byte-identical signature every tick,
    # so the gate suppresses the agent indefinitely and the overwatch silently
    # stops watching. Passing heartbeat=N appends a coarse time bucket so the
    # signature changes at most once per N seconds, guaranteeing the agent wakes
    # periodically even with no state change.
    hb = ""
    if heartbeat and heartbeat > 0:
        hb = f"|hb={int(time.time() // heartbeat)}"
    if state == "WORKING":
        return f"{state}|{ev}{hb}"
    h = hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{state}|{h}|{ev}{hb}"


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: fleet_state.py <tmux-session-name> [heartbeat-seconds]\n")
        return 2
    heartbeat = 0
    if len(sys.argv) > 2:
        try:
            heartbeat = int(float(sys.argv[2]))
        except ValueError:
            heartbeat = 0
    print(fingerprint(sys.argv[1], heartbeat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
