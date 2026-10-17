#!/usr/bin/env python3
"""Control and report on the escalation funnel.

    fleet_funnel.py status            mode, the last day of pages by reason, shadow vs legacy, heartbeat
    fleet_funnel.py enforce           only the three cases (plus hard system events) page the operator
    fleet_funnel.py shadow            the previous behaviour, with the funnel recording beside it
    fleet_funnel.py off

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


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        return status()
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
