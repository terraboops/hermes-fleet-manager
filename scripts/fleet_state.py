#!/usr/bin/env python3
"""Deterministic STATE FINGERPRINT for one managed coding-agent tmux session.

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
import json
import os
import re
import subprocess
import sys
import time

LOG = os.path.expanduser("~/.hermes/logs/fleet-watch.log")

# The transcript authority lives in the repo next to this file; import it through
# the realpath so the cc-watch symlink still resolves (same trick fleet_ack uses).
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import fleet_transcript  # noqa: E402


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


REGISTRY = os.environ.get(
    "FLEET_REGISTRY",
    os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_registry.json"))
ACTIVITY_WINDOW = float(os.environ.get("FLEET_ACTIVITY_WINDOW", "180"))

# A REAL queued-message indicator is short chrome next to the composer: Claude Code renders
# "Press up to edit queued messages", or a bare count ("1 queued message"). A session's own
# PROSE is not an indicator -- and prose WRAPS, so a continuation row can be short enough to
# slip a length guard ("queued messages can't be deleted." is 34 chars).
#
# The old rule was a bare substring scan and classified a stopped, IDLE session as QUEUED
# (live, 2026-09-27). QUEUED means "leave it alone", so that false positive silently swallowed
# the nudge and the escalation for exactly the session that was parked waiting on a decision.
#
# Bias deliberately toward MISSING a queue: a missed one costs nothing (the next state change
# wakes the overwatch), a false one costs the escalation.
QUEUED_MARK = "Press up to edit queued"
QUEUED_RE = re.compile(r"(?i)^[^\w]{0,4}\d{0,3}\s*queued messages?\.?$")
QUEUED_MAX_LEN = 60


def queued_state(lines):
    """Pure: is a queued-message indicator showing? Whole-line shape test, not a substring scan."""
    for raw in lines:
        s = raw.strip()
        if not s or len(s) > QUEUED_MAX_LEN:
            continue
        if QUEUED_MARK in s or QUEUED_RE.match(s):
            return True
    return False


def transcript_entry(sess):
    """Return the exact registry entry for a managed session or short name."""
    import json
    try:
        with open(os.path.expanduser(REGISTRY), encoding="utf-8") as fh:
            reg = json.load(fh)
    except Exception:
        return None
    entries = reg if isinstance(reg, list) else reg.get("sessions", []) if isinstance(reg, dict) else []
    return next((e for e in entries if sess in (e.get("name"), e.get("short"))), None)


def transcript_of(sess):
    """Resolve this session's transcript using its provider and native identity."""
    import glob
    import json
    e = transcript_entry(sess)
    if not e:
        return ""
    if fleet_transcript._provider(e) == "codex":
        path, _ = fleet_transcript.resolve_codex(e)
        return path or ""
    cfg = os.path.expanduser(str(e.get("config_dir") or ""))
    uid = e.get("uuid") or ""
    if not (cfg and uid):
        return ""
    hits = glob.glob(os.path.join(cfg, "projects", "*", f"{uid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else ""


def written_recently(path, window=None):
    """True when the transcript grew within `window` seconds: output is still landing."""
    window = ACTIVITY_WINDOW if window is None else window
    try:
        return bool(path) and (time.time() - os.path.getmtime(path)) < window
    except OSError:
        return False


OUTPUT_EVENTS = ("MATCH", "WATCH-SATISFIED")


def _last_output_line(sess):
    """Newest daemon-log line showing THIS session producing output, or None.

    One definition, used by both the age signal and the fingerprint text: they
    previously diverged, and the fingerprint kept a substring match that credited one
    session with another's lines. A line counts only when it is a positive output event
    for exactly this session name, matched as a whole token.
    """
    try:
        want = re.compile(r"fleetwatch: (?:%s) %s(?:\s|$)"
                          % ("|".join(OUTPUT_EVENTS), re.escape(sess)))
        newest = None
        with open(LOG, "r", errors="replace") as fh:
            for ln in fh:
                if want.search(ln):
                    newest = ln
        return newest
    except Exception:
        return None




def event_age(sess):
    """Seconds since the newest log line showing this session PRODUCING output, or None.

    The daemon's own log is the second output surface: a session emits its sentinel
    tokens and lifecycle events there, and those writes land the moment a turn
    finishes — often BEFORE and INSTEAD OF any transcript growth. A transcript-only
    activity signal therefore misses the case that started this: a session that had
    just emitted its token and was mid-publish.

    ONLY positive output events count. The log also carries daemon bookkeeping ABOUT
    a session — WATCH-EXPIRED, ACK-MISSED — which means the daemon did NOT see
    something. Counting those made a session that had been idle for hours read as
    WORKING because the daemon logged a failure about it. (Raised by an independent
    adversarial review of an earlier version of this function.)

    The session name is matched as a whole token beside the event, never as a
    substring: one session's short name otherwise appears inside another's lines.
    """
    try:
        newest = _last_output_line(sess)
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


def codex_startup_prompt(pane_text):
    """Return a safe, structured classification of an owner-facing Codex startup prompt."""
    raw = str(pane_text or "")
    clean = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", raw)
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", clean)
    clean = clean.replace("\\r\\n", "\n").replace("\\n", "\n")
    clean = "".join(ch for ch in clean if ch in "\n\t" or ord(ch) >= 32)
    lower = clean.lower()
    choices = []
    selected_choice = None
    for line in clean.splitlines():
        match = re.match(r"^\s*[›>*]?\s*\d+[.)]\s*(.+?)\s*$", line)
        if match:
            label = re.sub(r"\s*\([^)]*\)\s*$", "", match.group(1)).strip()
            if label and label not in choices:
                choices.append(label)
            if re.match(r"^\s*[›>]\s*\d+[.)]", line):
                selected_choice = label

    category = None
    details = {}
    reason = None
    if ("update available" in lower or "updat available" in lower or
            ("update now" in lower and "skip" in lower)):
        category = "update"
        versions = re.search(r"(\d+\.\d+\.\d+)\s*(?:→|->|to)\s*(\d+\.\d+\.\d+)", clean)
        if versions:
            details = {"from_version": versions.group(1), "to_version": versions.group(2)}
            reason = "Codex update prompt requires an owner decision ({} → {})".format(*versions.groups())
        else:
            reason = "Codex update prompt requires an owner decision"
        for label in ("Update now", "Skip", "Skip until next version"):
            if label.lower() in lower and label not in choices:
                choices.append(label)
        choices.sort(key=lambda value: ("update now", "skip", "skip until next version").index(value.lower())
                     if value.lower() in ("update now", "skip", "skip until next version") else 9)
    elif ("hooks need review" in lower or "review hooks" in lower or
          ("hook" in lower and any(word in lower for word in ("new or changed", "hooks changed", "hook changed")) and
           any(word in lower for word in ("review", "trust")))):
        category = "hook-review"
        changed = re.search(r"(\d+)\s+hooks?\s+(?:are\s+)?(?:new\s+or\s+changed|changed|new)", lower)
        if changed:
            details = {"changed_hooks": int(changed.group(1))}
            reason = "Codex hook review requires an owner decision ({} changed hooks)".format(changed.group(1))
        else:
            reason = "Codex hook review requires an owner decision"
        for label in ("Review hooks", "Trust all and continue", "Continue without trusting"):
            if label.lower() in lower and label not in choices:
                choices.append(label)
    elif ("sign in with chatgpt to use codex" in lower or "sign in with device code" in lower or
          ("sign in" in lower and "device code" in lower)):
        category = "sign-in"
        reason = "Codex sign-in prompt requires an owner"
    elif ("do you trust the contents of this directory" in lower or "trust this folder" in lower or
          "trust this project" in lower or "folder access" in lower):
        category = "project-trust"
        reason = "Codex project trust prompt requires an owner decision"
        for label in ("Trust and continue", "Back to Agent Command Center", "No, exit"):
            if label.lower() in lower and label not in choices:
                choices.append(label)
    elif ("ask codex to do anything" not in lower and
          any(marker in lower for marker in ("approve", "allow", "choose one", "select an option",
                                             "press enter to confirm", "continue without trusting",
                                             "enter continue"))):
        category = "unknown-choice"
        reason = "Unrecognized Codex choice screen requires an owner decision"

    if category is None:
        return None
    semantic = json.dumps({"category": category, "choices": choices, "details": details},
                          sort_keys=True, separators=(",", ":"))
    return {"category": category, "reason": reason, "choices": choices, "details": details,
            "selected_choice": selected_choice,
            "fingerprint": hashlib.sha256(semantic.encode("utf-8")).hexdigest()[:16],
            "screen_sha256": hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest()}


def record_codex_prompt_event(entry, prompt, event="observed", path=None):
    """Append a privacy-minimized Codex prompt event; never store raw pane text or IDs."""
    if not isinstance(prompt, dict) or not prompt.get("category"):
        return False
    if event not in ("observed", "cleared", "auto-resolved", "update-started",
                     "update-timeout", "update-version-failed", "update-verified"):
        raise ValueError("unrecognized Codex prompt lifecycle event")
    identity = str((entry or {}).get("uuid") or (entry or {}).get("name") or "unknown")
    record = {
        "timestamp": time.time(),
        "event": event,
        "session_fingerprint": hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:16],
        "category": prompt.get("category"),
        "reason": prompt.get("reason"),
        "choices": prompt.get("choices", []),
        "selected_choice": prompt.get("selected_choice"),
        "details": prompt.get("details", {}),
        "fingerprint": prompt.get("fingerprint"),
        "screen_sha256": prompt.get("screen_sha256"),
    }
    target = os.path.expanduser(path or os.environ.get(
        "FLEET_CODEX_PROMPT_LOG", "~/.hermes/scripts/cc-watch/codex_prompt_events.jsonl"))
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as output:
            output.write(json.dumps(record, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def codex_startup_blocker(pane_text):
    """Return the owner-facing Codex TUI prompt blocking startup, if any."""
    prompt = codex_startup_prompt(pane_text)
    return prompt["reason"] if prompt else None


def codex_state(sess, entry, pane):
    """Conservative Codex-specific state from native transcript plus visible TUI cues."""
    import json
    import re
    path = transcript_of(sess)
    uid = str(entry.get("uuid") or entry.get("session_id") or "")
    identity = uid[:12] or "unknown"
    if not path or not os.path.isfile(path):
        return f"UNKNOWN|{identity}|transcript-missing"

    last_role = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                role = fleet_transcript.record_role(obj)
                if role in ("user", "assistant"):
                    last_role = role
    except OSError:
        return f"UNKNOWN|{identity}|transcript-unreadable"

    lines = [line.strip() for line in pane.splitlines() if line.strip()]
    text = "\n".join(lines[-20:])
    lower = text.lower()
    if "esc to interrupt" in lower or re.search(r"(?im)^\s*[•*]\s*(thinking|working|planning|running|searching|reading|writing)\.{3}", text):
        state = "WORKING"
    elif codex_startup_blocker(text):
        state = "NEEDS-INPUT"
    elif last_role == "user":
        # A user turn is unanswered, but without a live working or explicit queue cue
        # the transcript alone cannot distinguish running from queued.
        state = "UNKNOWN"
    elif re.search(r"(?i)(approve|allow|select an option|choose one|press enter to confirm)", text) and not re.search(r"(?i)ask codex to do anything", text):
        state = "NEEDS-INPUT"
    elif re.search(r"(?i)ask codex to do anything", text):
        state = "IDLE"
    else:
        state = "UNKNOWN"
    inbound = ""
    in_epoch, in_text = fleet_transcript.newest_inbound(path, max_chars=100)
    if in_text:
        digest = hashlib.sha1(in_text.encode("utf-8", "replace")).hexdigest()[:8]
        inbound = f"|u={digest}"
    return f"{state}|{identity}|{last_role or 'none'}{inbound}"


def fingerprint(sess, heartbeat=0):
    if subprocess.run(["tmux", "has-session", "-t", "=" + sess + ":"],
                      capture_output=True).returncode != 0:
        return "DEAD"

    entry = transcript_entry(sess)
    pane = sh("tmux", "capture-pane", "-t", "=" + sess + ":", "-p", "-S", "-40")
    if entry and fleet_transcript._provider(entry) == "codex":
        state = codex_state(sess, entry, pane)
        if heartbeat and heartbeat > 0:
            state += f"|hb={int(time.time() // heartbeat)}"
        return state

    if entry and entry.get('profile') == 'work' and fleet_transcript._provider(entry) == 'claude':
        from pathlib import Path
        import fleet_activity
        detail = fleet_activity.inspect_entry(entry, transcript_of(sess), Path(os.path.abspath(__file__)).parent)
        state = {'WAITING_TOOL': 'WORKING', 'BUSY': 'WORKING', 'NEEDS_INPUT': 'NEEDS-INPUT'}.get(detail['state'], detail['state'])
        # Stable until a meaningful event or the one five-minute eligibility edge.
        result = f"{state}|native|{detail.get('last_meaningful_at')}|idle_due={int(detail.get('idle_eligible', False))}|{detail['reason']}"
        if heartbeat and heartbeat > 0:
            result += f"|hb={int(time.time() // heartbeat)}"
        return result

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
    # The LIVE region: the composer and the rows just above it. Everything higher in the
    # 40-line window is superseded scrollback, and scrollback lies in both directions.
    nonempty = [l for l in lines if l.strip()]
    composer = "\n".join(nonempty[-6:])
    near_tail = "\n".join(nonempty[-14:])
    ui_region = "\n".join(nonempty[-30:])
    spinner = any(
        len(l.strip()) < 80
        and re.match(r"^\s*[^A-Za-z0-9]{0,4}\s*[A-Za-z]{3,}(…|\.\.\.)\s*(\(.*\))?\s*$", l)
        for l in tail
    ) or any("esc to interrupt" in l for l in tail)
    finished = any("· done" in l for l in tail)
    working = spinner and not finished
    # QUEUED is scoped to the LIVE region AND shape-tested (see queued_state): a session's
    # own prose must never read as a queued-message indicator, because QUEUED means "leave
    # it alone" and that silently swallows the nudge/escalation for a stopped session.
    queued = queued_state(nonempty[-30:])
    # A pending question / needs-input surface (approval gate, a posed question, or
    # Claude Code's own question UI: "Select with numbers [1-5]... ").
    #
    # SCOPE MATTERS (2026-09-21). These patterns used to search 600-900 characters of
    # scrollback, so a THROWAWAY shell pane classified as NEEDS-INPUT because
    # "[oh-my-zsh] Would you like to update? [Y/n]" matched the y/n rule from deep in
    # history — a session sitting at a shell prompt was reported as waiting on the
    # operator. Only the LIVE region counts: the composer and the rows just above it,
    # which is where a real question or a real prompt is rendered. Claude's own
    # question UI occupies more rows, so it gets a taller window than the composer.
    needs_input = bool(
        re.search(r"(?i)(y/n)", composer)
        or re.search(r"(?i)(\?\s*$|do you want|shall I|approve\b)", near_tail)
        or re.search(r"(?i)(Select with numbers|Then Enter to submit|answered Claude's questions)",
                     ui_region)
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
    line = _last_output_line(sess)
    ev = line.strip()[-160:] if line else ""
    hb = ""
    if heartbeat and heartbeat > 0:
        # EVERY state, deliberately. The heartbeat covers the session BUSY FOREVER
        # above, AND -- by design -- an IDLE one: that periodic wake is the prompt to
        # ask whether the session is really finished and what else it could complete,
        # so it keeps working and routes around blockers instead of stopping early.
        # Narrowing this to WORKING removes exactly the nudge that drives that.
        hb = f"|hb={int(time.time() // heartbeat)}"
    # --- the operator's newest instruction (2026-09-28) --------------------
    # The fingerprint is what WAKES the overwatch, and it used to carry no user
    # messages at all - so an agent watching a session could not see what the operator
    # had actually asked for, and a session's plan got parked as
    # "not-yet-authorised" 24 minutes after she requested it. The newest inbound
    # instruction now rides in every fingerprint: it is part of the change
    # signal, so a new instruction from her re-wakes the watcher, and it is
    # readable, so the watcher does not have to guess at her intent.
    inbound = ""
    if tp:
        in_epoch, in_text = fleet_transcript.newest_inbound(tp, max_chars=140)
        if in_text:
            digest = hashlib.sha1(in_text.encode("utf-8", "replace")).hexdigest()[:8]
            clean = re.sub(r"[\r\n\t|]+", " ", in_text)[:110].strip()
            inbound = f"|u={digest}|{clean}"

    if state == "WORKING":
        return f"{state}|{ev}{hb}{inbound}"
    h = hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{state}|{h}|{ev}{hb}{inbound}"


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
