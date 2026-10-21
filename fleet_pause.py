#!/usr/bin/env python3
"""Pause or resume the fleet daemon's attention to a session.

    fleet_pause.py pause <session|short> [--until-epoch N] [--why TEXT]
    fleet_pause.py resume <session|short>
    fleet_pause.py list

A paused session gets no nudges, no pages and no forwarded questions from fleet-watch. The session
itself is untouched. Reversible: `resume` removes the entry.
"""
import argparse, json, os, sys, time

PAUSE_FILE = os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_paused.json")
REG = os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_registry.json")


def _load():
    try:
        return json.load(open(PAUSE_FILE))
    except Exception:
        return {}


def _save(d):
    tmp = PAUSE_FILE + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1)
    os.replace(tmp, PAUSE_FILE)


def _resolve(ident):
    sessions = json.load(open(REG))["sessions"]
    hits = [e["name"] for e in sessions
            if ident in (e.get("name"), e.get("short"), e.get("uuid")) or
            (len(ident) >= 6 and str(e.get("uuid", "")).startswith(ident))]
    if len(hits) != 1:
        sys.exit("%s: %s" % ("unknown session" if not hits else "ambiguous", ident + (" -> " + ", ".join(hits) if hits else "")))
    return hits[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pause"); p.add_argument("session"); p.add_argument("--until-epoch", type=int); p.add_argument("--why", default="")
    r = sub.add_parser("resume"); r.add_argument("session")
    sub.add_parser("list")
    a = ap.parse_args()
    d = _load()
    if a.cmd == "list":
        for k, v in d.items():
            print(k, "until", v.get("until") or "resumed manually", "|", v.get("why", ""))
        return
    sn = _resolve(a.session)
    if a.cmd == "pause":
        d[sn] = {"since": int(time.time()), "until": a.until_epoch, "why": a.why}
        print("paused", sn)
    else:
        print("resumed" if d.pop(sn, None) else "was not paused", sn)
    _save(d)


if __name__ == "__main__":
    main()
