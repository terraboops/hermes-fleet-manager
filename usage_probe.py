#!/usr/bin/env python3
"""Read the REAL plan-usage figures from a Claude Code session's /usage screen and record them.

    usage_probe.py once [--profile personal|work|all] [--no-record]
    usage_probe.py report [--hours N]

No model call: /usage is a built-in screen. For each profile it picks ONE idle live session (empty
composer, not mid-turn), opens /usage, pages to the plan-limit block, parses it, closes the screen,
and appends one row to ~/.hermes/metrics/usage-probe.jsonl. It also feeds usage_meter.py as a
calibration reading, so the estimator learns from the real number each hour.

A session that is mid-turn, shows a dialog, or is not at an empty prompt is never touched: the probe
skips that profile and logs why. Failing open (no row) is the right failure.
"""
import argparse, json, os, re, subprocess, sys, time

HOME = os.path.expanduser("~")
LOG = os.path.join(HOME, ".hermes/metrics/usage-probe.jsonl")
REG = os.path.join(HOME, ".hermes/scripts/cc-watch/fleet_registry.json")
METER = os.path.join(HOME, "Developer/hermes-fleet-manager/usage_meter.py")


def tmux(*a):
    return subprocess.run(["tmux", *a], capture_output=True, text=True).stdout


def pane(s, n=60):
    return tmux("capture-pane", "-pt", "=%s:" % s, "-S", "-%d" % n)


def keys(s, *k):
    subprocess.run(["tmux", "send-keys", "-t", "=%s:" % s, *k])


def live_sessions(profile):
    reg = json.load(open(REG))["sessions"]
    have = set(l.split(":")[0] for l in tmux("ls").splitlines())
    return [e["name"] for e in reg if e.get("profile") == profile and e["name"] in have]


def idle_at_prompt(s):
    p = pane(s, 25)
    tail = [l for l in p.splitlines() if l.strip()][-12:]
    text = "\n".join(tail)
    busy = re.search(r"esc to interrupt|Running…|✻ [A-Z][a-z]+ing", text) or "Esc to cancel" in text
    composer = [l for l in tail if l.startswith("❯")]
    return (not busy) and bool(composer) and composer[-1].strip() in ("❯", "❯ ")


def parse(screen):
    out = {}
    m = re.search(r"Current session\s*\n.*?(\d+)% used\s*\n\s*Resets ([^\n(]+)", screen, re.S)
    if m:
        out["session_pct"], out["session_resets"] = int(m.group(1)), m.group(2).strip()
    m = re.search(r"Current week \(all models\)\s*\n.*?(\d+)% used\s*\n\s*Resets ([^\n(]+)", screen, re.S)
    if m:
        out["week_pct"], out["week_resets"] = int(m.group(1)), m.group(2).strip()
    m = re.search(r"Current week \(([^)]+)\)\s*\n[^\n]*?(\d+)% used", screen)
    if m and m.group(1) != "all models":
        out["week_model"], out["week_model_pct"] = m.group(1), int(m.group(2))
    drivers = re.findall(r"^\s*(\d+)%\s+(.+?)\s*$", screen, re.M)
    if drivers:
        out["drivers"] = [{"pct": int(a), "advice": b[:90]} for a, b in drivers[:6]]
    return out


def read_usage(s):
    keys(s, "/usage"); time.sleep(1); keys(s, "Enter"); time.sleep(4)
    seen = []
    for step in range(5):
        keys(s, *(["Down"] * 14)); time.sleep(1.2)
        seen.append(pane(s, 60))
    keys(s, "Escape"); time.sleep(1.5)
    screen = "\n".join(seen)
    got = parse(screen)
    closed = idle_at_prompt(s)
    return got, closed


def to_meter(profile, got):
    if "week_pct" not in got:
        return "no weekly figure parsed"
    r = subprocess.run([sys.executable, METER, "reading", profile, str(got["week_pct"]) if got["week_pct"] > 0 else "1",
                        "--scope", "weekly", "--resets", got["week_resets"]],
                       capture_output=True, text=True)
    return (r.stdout or r.stderr).strip().splitlines()[-1][:160] if (r.stdout or r.stderr) else "no output"


def meter_estimate(profile):
    r = subprocess.run([sys.executable, METER, "status"], capture_output=True, text=True).stdout
    m = re.search(r"\.claude-%s:.*?week used=(\d+)%%" % profile, r, re.S)
    return int(m.group(1)) if m else None


def once(profile, record=True):
    cands = live_sessions(profile)
    row = {"ts": int(time.time()), "profile": profile, "candidates": cands}
    pick = next((s for s in cands if idle_at_prompt(s)), None)
    if not pick:
        row["skipped"] = "no live session idle at an empty prompt" if cands else "no live session"
        print(profile, "SKIPPED:", row["skipped"])
    else:
        row["session"] = pick
        row["meter_estimate_before"] = meter_estimate(profile)
        got, closed = read_usage(pick)
        row.update(got); row["screen_closed_cleanly"] = closed
        if not closed:
            row["warning"] = "session not back at an empty prompt after closing /usage"
        if record and got.get("week_pct") is not None:
            row["fed_to_meter"] = to_meter(profile, got)
            row["meter_estimate_after"] = meter_estimate(profile)
        print(profile, json.dumps({k: row.get(k) for k in ("session", "session_pct", "week_pct", "week_resets", "meter_estimate_before", "meter_estimate_after", "screen_closed_cleanly")}))
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "a").write(json.dumps(row) + "\n")
    return row


def report(hours):
    rows = [json.loads(l) for l in open(LOG) if l.strip()] if os.path.exists(LOG) else []
    rows = [r for r in rows if r["ts"] > time.time() - hours * 3600 and r.get("week_pct") is not None]
    for r in rows:
        err = "" if r.get("meter_estimate_before") is None else " meter said %s%%, error %+d" % (r["meter_estimate_before"], r["meter_estimate_before"] - r["week_pct"])
        print(time.strftime("%a %H:%M", time.localtime(r["ts"])), r["profile"], "real %s%%" % r["week_pct"], err)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("once"); o.add_argument("--profile", default="all"); o.add_argument("--no-record", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--hours", type=float, default=168)
    a = ap.parse_args()
    if a.cmd == "once":
        for p in (["personal", "work"] if a.profile == "all" else [a.profile]):
            once(p, record=not a.no_record)
    else:
        report(a.hours)
