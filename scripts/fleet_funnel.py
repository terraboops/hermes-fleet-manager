#!/usr/bin/env python3
"""Control and report on the escalation funnel.

    fleet_funnel.py status            mode, the last day of pages by reason, shadow vs legacy, heartbeat
    fleet_funnel.py enforce           only the three cases (plus hard system events) page the operator
    fleet_funnel.py shadow            the previous behaviour, with the funnel recording beside it
    fleet_funnel.py off
    fleet_funnel.py replay [days] [-v]  re-run the logged asks of the last days through the current code
    fleet_funnel.py weekly [days]     pages sent, the share the operator acted on, asks the fleet answered

`enforce` also stops each armed overwatch job from delivering its reports to the chat (deliver=local),
so a page can only come from the daemon's single door. The previous setting is saved and `shadow`
restores it. The daemon reads the mode file every tick; no restart is needed.
"""
import collections
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
import escalation  # noqa: E402
import policy      # noqa: E402

FUNNEL_DIR = os.path.expanduser(os.environ.get("FLEET_FUNNEL_DIR", "~/.hermes/cache/fleet-funnel"))
ARMED = os.path.expanduser("~/.hermes/scripts/cc-watch/overwatch/armed.json")
MANIFEST = os.path.join(FUNNEL_DIR, "overwatch-delivery.json")
HERMES = os.environ.get("HERMES_BIN", "hermes")


def _write_mode(m):
    os.makedirs(FUNNEL_DIR, exist_ok=True)
    with open(os.path.join(FUNNEL_DIR, "mode"), "w", encoding="utf-8") as f:
        f.write(m + "\n")


def _jobs():
    """{job name: job id} from `hermes cron list`."""
    out = subprocess.run([HERMES, "cron", "list"], capture_output=True, text=True, timeout=120).stdout
    jobs, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"\s+([0-9a-f]{12})\s+\[", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"\s+Name:\s+(\S+)", line)
        if m and cur:
            jobs[m.group(1)] = cur
            cur = None
    return jobs


def _set_delivery(to_local):
    try:
        armed = json.load(open(ARMED))
    except (OSError, ValueError):
        armed = {}
    jobs = _jobs()
    try:
        saved = json.load(open(MANIFEST))
    except (OSError, ValueError):
        saved = {}
    changed = []
    for sn, info in armed.items():
        name = info.get("name")
        jid = jobs.get(name)
        if not jid:
            print("  skip %s: no cron job named %s" % (sn, name))
            continue
        if to_local:
            saved.setdefault(name, {"id": jid, "deliver": info.get("deliver", "origin")})
            target = "local"
        else:
            if name not in saved:
                continue
            target = saved[name]["deliver"]
        r = subprocess.run([HERMES, "cron", "edit", jid, "--deliver", target],
                           capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        print("  %s %s -> deliver=%s" % ("ok  " if ok else "FAIL", name, target))
        if ok:
            changed.append(name)
    if to_local:
        os.makedirs(FUNNEL_DIR, exist_ok=True)
        json.dump(saved, open(MANIFEST, "w"))
    else:
        for n in changed:
            saved.pop(n, None)
        json.dump(saved, open(MANIFEST, "w"))
    return changed


def status():
    since = time.time() - 86400
    esc = escalation.read(escalation.ESCALATIONS, since)
    att = escalation.read(escalation.ATTEMPTS, since)
    print("mode: %s" % escalation.mode())
    pages = collections.Counter(r["reason"] for r in esc if r.get("reason"))
    print("pages, last 24h: %s" % (dict(pages) or "none"))
    handled = collections.Counter(r.get("note") for r in esc if r.get("kind") == "handled")
    if handled:
        print("handled without telling anyone: %s" % dict(handled))
    unarmed = sum(1 for r in esc if r.get("kind") == "unarmed_hard")
    if unarmed:
        print("hard events on unarmed sessions (not paged): %d" % unarmed)
    sh = [r for r in att if r.get("kind") == "shadow"]
    if sh:
        both = sum(1 for r in sh if r.get("legacy_paged") and r.get("funnel_would_page"))
        only_legacy = sum(1 for r in sh if r.get("legacy_paged") and not r.get("funnel_would_page"))
        only_funnel = sum(1 for r in sh if not r.get("legacy_paged") and r.get("funnel_would_page"))
        print("shadow stalls: %d rows; both would page %d; only the old gate %d; only the funnel %d"
              % (len(sh), both, only_legacy, only_funnel))
    rungs = collections.Counter(r.get("rung") for r in att if r.get("rung") in ("nudge", "challenge", "realign", "answer"))
    print("rungs sent: %s" % (dict(rungs) or "none"))
    outs = collections.Counter(r.get("outcome") for r in att if r.get("kind") == "outcome")
    if outs:
        print("outcomes: %s" % dict(outs))
    hb = [r for r in att if r.get("kind") == "heartbeat"]
    age = (time.time() - hb[-1]["at"]) / 60 if hb else None
    print("daemon heartbeat: %s" % ("none in 24h (the funnel is not running)" if age is None else "%.0f min ago" % age))
    return 0


def _inbound(session, since, until):
    """Texts of messages that reached `session` between two times (transcript-based)."""
    import fleet_watch
    import fleet_transcript
    path = fleet_watch.find_jsonl(session)
    if not path:
        raise LookupError("no transcript for %s" % session)
    return [t for ep, t in fleet_transcript._scan_inbound(path, 400, window=60_000_000, max_chars=600)
            if ep is not None and since <= ep <= until]


def _ask_of(text):
    m = re.search(r"do not cover:\s*(.*?)\s*Recommendation: reply in", text or "", re.S)
    return m.group(1) if m else None


def _gated_ask_of(text):
    m = re.search(r"Question:\s*(.*)$", text or "", re.S)
    return m.group(1) if m else None


def replay(days=30, verbose=False):
    """Pages before and after: every logged page of the window, with the ungrounded asks re-decided by
    the current code. Dispatch is a no-op and every file it would write goes to a scratch directory."""
    import tempfile
    import clarify
    tmp = tempfile.mkdtemp(prefix="fleet-replay-")
    clarify.FUNNEL_DIR = tmp
    os.environ["FLEET_DECISIONS_LOG"] = os.path.join(tmp, "decisions.md")
    since = time.time() - days * 86400
    pages = [r for r in escalation.read(escalation.ESCALATIONS, since) if r.get("reason") and not r.get("kind")]
    before, after, outcome, kept = {}, {}, {}, {}
    rkept, mass = {}, {}
    _inb = {}
    clarify.INBOUND = lambda sn: _inb.setdefault(sn, clarify._inbound_texts(sn))
    for r in pages:
        reason = r["reason"]
        before[reason] = before.get(reason, 0) + 1
        ask = (_ask_of(r.get("text")) if reason == "clarification_ungrounded"
               else _gated_ask_of(r.get("text")) if reason == "gated_action" else None)
        kind = "page"
        if ask is not None:
            k, new_reason, text, detail = clarify.resolve(r["session"], ask, lambda sn, t: True, r["at"])
            kind = {"answered": "answered", "silent": "silent"}.get(k, "page")
            if kind == "page" and reason == "gated_action" and new_reason:
                reason = new_reason               # no longer a hard floor: it is an ordinary ask now
            if verbose and kind != "page":
                print("%-8s %s | %s | %s" % (kind, r["session"], (detail or {}).get("cite"), " ".join(ask.split())[:150]))
        if kind == "page" and reason in ("clarification_ungrounded", "gated_action"):
            # the door's repeat suppression, replayed over the pages that survive
            key = (r["session"], reason)
            body = r.get("text") or ""
            win = escalation.repeat_window(body)
            if any(r["at"] - t < win and escalation.similar(body, b) for t, b in kept.get(key, [])):
                kind = "repeat"
            else:
                kept.setdefault(key, []).append((r["at"], body))
        # batching, in the live order: a mass event first, then the receipt-review window
        body = r.get("text") or ""
        if kind == "page" and escalation.mass_event_batched(mass, reason, r["session"], r["at"]):
            escalation.mass_event_note(mass, reason, r["session"], r["at"], True)
            kind = "batched"
        if kind == "page" and escalation.is_receipt_review(body):
            win = int(policy.get("receipt_review_window_s"))
            rk = (r["session"], reason)
            if any(r["at"] - t < win for t in rkept.get(rk, [])):
                kind = "receipt_repeat"
            else:
                rkept.setdefault(rk, []).append(r["at"])
        if kind == "page":
            escalation.mass_event_note(mass, reason, r["session"], r["at"], False)
        outcome[kind] = outcome.get(kind, 0) + 1
        if kind == "page":
            after[reason] = after.get(reason, 0) + 1
    print("window: %d days" % days)
    print("pages before: %d  %s" % (sum(before.values()), dict(sorted(before.items(), key=lambda x: -x[1]))))
    print("pages after:  %d  %s" % (sum(after.values()), dict(sorted(after.items(), key=lambda x: -x[1]))))
    print("re-decided asks: %s" % outcome)
    return 0


def weekly(days=7):
    since = time.time() - days * 86400
    w = policy.weekly_counts(escalation.read(escalation.ESCALATIONS, since),
                             escalation.read(escalation.ATTEMPTS, since), _inbound, days=days)
    print(policy.format_weekly(w))
    print("by reason: %s" % (w["by_reason"] or "none"))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        return status()
    if cmd == "replay":
        rest = [a for a in argv[2:] if a != "-v"]
        return replay(int(rest[0]) if rest else 30, "-v" in argv)
    if cmd == "weekly":
        return weekly(int(argv[2]) if len(argv) > 2 else 7)
    if cmd == "enforce":
        _write_mode("enforce")
        print("mode: enforce")
        _set_delivery(True)
        return 0
    if cmd in ("shadow", "off"):
        _write_mode(cmd)
        print("mode: %s" % cmd)
        _set_delivery(False)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
