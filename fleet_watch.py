#!/usr/bin/env python3
"""Continuous fleet JSONL watcher (monitor_script contract).

Tails every active Claude Code session's CANONICAL transcript jsonl and emits a line
per NEW high-signal event (sentinel token like DONE-<x>, or a failure marker). Silent
when nothing new. Used as a Hermes cron `monitor_script`: output identical to prior tick
=> no fire; a NEW event changes output => wakes the agent once. NOT a nudge.

Patterns targeted (low-noise on purpose; not every token, not 'MESSAGE RECEIVED'):
  - sentinel completion tokens: DONE-..., MESSAGE-RECEIVED variants the agent is asked to emit
  - failure markers: error / failed / traceback / nonzero
Use --patterns to override.
"""
import argparse, os, sys, re, json, glob, time, pathlib, logging, logging.handlers, hmac, hashlib, urllib.request, urllib.error, subprocess

DEFAULT_PAT = [r"\bDONE-[A-Za-z0-9_.:-]+\b", r"\bNEEDS-INPUT-[A-Za-z0-9_.:-]+\b", r"\b[tT]raceback\b"]
LOG = logging.getLogger("fleetwatch")

# ---- config (dogfood: all paths come from a config file; back-compat defaults) ----
def _load_cfg():
    p = os.environ.get("FLEET_CONFIG")
    if not p:
        return {}
    try:
        return json.load(open(os.path.expanduser(p)))
    except Exception as e:
        print(f"fleet-config load failed for {p}: {e!r}", flush=True)
        return {}

_CFG = _load_cfg()
def _cfg(key, default):
    v = _CFG.get(key)
    return os.path.expanduser(v) if v else os.path.expanduser(default)

LOG_PATH = _cfg("log_path", "~/.hermes/logs/fleet-watch.log")
STATE = _cfg("state_file", "~/.hermes/scripts/cc-watch/fleet_watch_state.json")
EVENTS = _cfg("events_file", "~/.hermes/cache/fleet-watch-events.log")

def setup_logging():
    stream = os.path.expanduser("~/.hermes")
    os.makedirs(os.path.join(stream, "logs"), exist_ok=True)
    LOG.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    LOG.addHandler(fh)
    return LOG

STATE = os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_watch_state.json")
EVENTS = os.path.expanduser("~/.hermes/cache/fleet-watch-events.log")
_unresolved_reported = set()

# Webhook push (the proper push model): when set, matched events are POSTed to the Hermes
# webhook adapter (which runs an agent + delivers to the target) instead of file-appended.
def _load_fleet_env():
    """Fallback: read FLEET_WEBHOOK_* from ~/.hermes/.env (covers the launchd daemon, which
    does not inherit the manual process env). Returns dict of any found vars."""
    out = {}
    p = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(p):
        try:
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if k in ("FLEET_WEBHOOK_URL", "FLEET_WEBHOOK_SECRET"):
                        out[k] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return out

_fleet_env = _load_fleet_env()
WH_URL = os.environ.get("FLEET_WEBHOOK_URL") or _fleet_env.get("FLEET_WEBHOOK_URL")
WH_SECRET = os.environ.get("FLEET_WEBHOOK_SECRET") or _fleet_env.get("FLEET_WEBHOOK_SECRET")


def post_webhook(session, match, url=WH_URL, secret=WH_SECRET):
    """POST an event to the Hermes webhook adapter, HMAC-signed. Returns True on 2xx."""
    if not (url and secret):
        return False
    body = json.dumps({"event": match, "session": session, "match": match,
                       "ts": int(time.time())}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            LOG.info("WEBHOOK POST %s %s -> HTTP %s", session, match, r.status)
            return True
    except Exception as e:
        LOG.error("WEBHOOK POST FAILED %s | %s -> %r", session, match, e)
        return False

REGISTRY_FILE = _cfg("registry_file", "~/.hermes/scripts/cc-watch/fleet_registry.json")

def load_registry():
    """Registry is the ONLY source of sessions to watch. Explicit: register on create,
    unregister on close. The daemon never discovers or audits on its own."""
    try:
        return {e["name"]: e for e in json.load(open(REGISTRY_FILE))["sessions"]}
    except Exception as e:
        LOG.warning("registry load failed: %r", e)
        return {}

def _slug(p):
    return re.sub(r"[^A-Za-z0-9]+", "-", os.path.expanduser(p))

def find_jsonl(sname):
    """Resolve a REGISTERED session's transcript from the registry (config_dir+cwd+uuid)."""
    e = load_registry().get(sname)
    if not e:
        return None
    base = pathlib.Path(os.path.expanduser(e["config_dir"])) / "projects" / _slug(e["cwd"])
    if e.get("uuid"):
        p = base / f"{e['uuid']}.jsonl"
        return os.fspath(p) if p.is_file() else None
    cand = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return os.fspath(cand[0]) if cand else None


def SESSIONS():
    """Registered session names (explicit registry; no tmux discovery)."""
    return list(load_registry())


def extract_text(obj):
    m = obj.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def scan(patterns):
    """Tail all sessions once; return new events. Updates persistent state."""
    pats = [re.compile(p) for p in patterns]
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    events = []
    for sn in SESSIONS():
        jl = find_jsonl(sn)
        if not jl:
            if sn not in _unresolved_reported:
                LOG.warning("no jsonl resolved for %s", sn)
                _unresolved_reported.add(sn)
            continue
        try:
            size = state.get(sn, 0)
            cur = os.path.getsize(jl)
            if cur < size:
                size = 0
            if size == 0:
                state[sn] = cur   # baseline on first sight, no old playback
                continue
            with open(jl, "rb") as fh:
                fh.seek(size)
                data = fh.read(cur - size).decode("utf-8", "replace")
            state[sn] = cur
            fired = state.setdefault("_fired", {}).setdefault(sn, [])
            for line in data.splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                txt = extract_text(obj)
                if not txt:
                    continue
                for p in pats:
                    m = p.search(txt)
                    if m:
                        # dedupe: skip if this exact line already fired. Claude transcripts
                        # compact/replay in place, so byte offsets can reset and re-present a
                        # sentinel; keying on the line's uuid (or content hash) stops replays.
                        uid = obj.get("uuid") or (obj.get("message") or {}).get("id")
                        key = uid or hashlib.sha256(line.encode("utf-8")).hexdigest()
                        if key in fired:
                            break
                        fired.append(key)
                        if len(fired) > 300:
                            fired[:] = fired[-300:]
                        events.append((sn, m.group(0)))
                        LOG.info("MATCH %s | %s", sn, m.group(0))
                        break
        except Exception as e:
            LOG.warning("scan error %s: %r", jl, e)
    json.dump(state, open(STATE, "w"))
    return events


# ---- context guard (root cause: unbounded context wedges idle sessions) ----
# Pride silently wedged at ~410k tokens (177h, no auto-clear). Never let that
# happen again: auto-clear an IDLE registered session when the CLI itself flags
# bloat ("/clear to save N tokens") OR its live token counter exceeds the
# threshold. Logs + surfaces a CONTEXT-AUTOCLEAR event. Never clears a busy one.
CLEAR_RE = re.compile(r"/clear to save\s+([\d,.]+)\s*([kKmM]?)tokens", re.I)
TOK_RE = re.compile(r"\u25b2\s*([\d.]+)\s*([kKmM])")
ALERT_MIN_TOKENS = int(os.environ.get("FLEET_CONTEXT_ALERT_TOKENS", "320000"))
AUTOCLEAR_MIN_INTERVAL = 30 * 60  # seconds between auto-clears per session
_context_lastclear = {}
_context_lastalert = {}


def _tok_val(num, unit):
    try:
        n = float(num)
    except Exception:
        return 0
    return int(n * (1_000_000 if unit.lower() == "m" else 1_000))


def _idle_pane(tail):
    return ("/rc" in tail or "/rac" in tail) and "Cogitat" not in tail


def context_guard():
    """Two-tier anti-wedge guard (root cause: Pride silently wedged at ~410k):
    1. ALERT any registered session whose live token counter is high -> surfaces
       CONTEXT-ALERT so bloat is never silent (Terra/agent can decide a /clear).
    2. AUTO-CLEAR only an IDLE session when the CLI ITSELF recommends bloat relief
       ("/clear to save N tokens") - the strong wedge signal. Never clears a busy
       session, never clears valuable-but-high context on threshold alone."""
    for nm, e in load_registry().items():
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-t", nm, "-p", "-S", "-30"],
                capture_output=True, text=True, timeout=5).stdout or ""
            if not out.strip():
                continue
            tail = out[-600:]
            idle = _idle_pane(tail)
            n, reason = 0, ""
            m = CLEAR_RE.search(out)
            if m:
                n, reason = _tok_val(m.group(1), m.group(2)), "cli-bloat"
            else:
                cm = TOK_RE.search(out)
                if cm:
                    n = _tok_val(cm.group(1), cm.group(2))
            now = time.time()

            # 1) ALERT on high context (any state) so nothing wedges unseen.
            # LOG/EVENTS only - NOT posted to the user (a per-session alert storm is
            # more noise than signal; the agent consolidates bloat on status checks).
            if n >= ALERT_MIN_TOKENS and now - _context_lastalert.get(nm, 0) >= 3600:
                _context_lastalert[nm] = now
                msg = "CONTEXT-ALERT %s @%dk" % (nm, n // 1000)
                LOG.warning("%s", msg)
                with open(EVENTS, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

            # 2) AUTO-CLEAR only idle + CLI-recommended (strong wedge signal).
            if reason == "cli-bloat" and idle and \
                    now - _context_lastclear.get(nm, 0) >= AUTOCLEAR_MIN_INTERVAL:
                _context_lastclear[nm] = now
                subprocess.run(["tmux", "send-keys", "-t", nm, "/clear", "Enter"], timeout=5)
                msg = "CONTEXT-AUTOCLEAR %s @~%dk (cli-bloat)" % (nm, n // 1000)
                LOG.warning("%s", msg)
                with open(EVENTS, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
                if WH_URL:
                    post_webhook(nm, msg)
        except Exception:
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patterns", action="append", default=DEFAULT_PAT)
    ap.add_argument("--daemon", action="store_true",
                    help="loop forever at --interval, pushing matches via `hermes send`")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--to", default="telegram:USER",
                    help="hermes send target (used with --daemon)")
    a = ap.parse_args()

    if a.daemon:
        setup_logging()
        LOG.info("fleet-watch daemon start pid=%s interval=%.1fs target=%s",
                 os.getpid(), a.interval, a.to)
        # NOTE: events are APPENDED to EVENTS (for a cron monitor_script that wakes the
        # Hermes agent to act), NOT sent straight to the user — a passthrough DM would
        # bypass the agent entirely (lesson: user flagged a raw 'cc-x: done' that I never saw).
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        while True:
            try:
                for sn, match in scan(a.patterns):
                    if WH_URL:
                        # Primary = push the event to the Hermes webhook adapter (agent run +
                        # telegram delivery). If it fails (e.g. gateway not yet restarted with
                        # webhooks loaded), fall back to the events file so the monitor cron
                        # still surfaces it. No double-delivery: a successful POST won't append.
                        if post_webhook(sn, match):
                            continue
                        LOG.warning("webhook down for %s %s -> falling back to events file", sn, match)
                    with open(EVENTS, "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {sn}: {match}\n")
                    LOG.info("EVENT %s: %s", sn, match)
            except Exception as e:
                LOG.exception("daemon scan/send loop error")
            try:
                context_guard()
            except Exception:
                pass
            time.sleep(a.interval)
        return

    # monitor_script mode: print matches so a changed output wakes the cron agent.
    events = scan(a.patterns)
    if events:
        print("\n".join(f"{sn}: {match}" for sn, match in events))


if __name__ == "__main__":
    main()
