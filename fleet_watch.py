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
import argparse, os, sys, re, json, glob, time, pathlib, logging, logging.handlers, hmac, hashlib, urllib.request, urllib.error, subprocess, fcntl

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

def post_batch(events, url=WH_URL, secret=WH_SECRET):
    """POST a BATCH of sentinel events in ONE request (debounce/reconcile), HMAC-signed.
    Returns True on 2xx. The Hermes handler turns the whole batch into ONE digest."""
    if not (url and secret) or not events:
        return False
    body = json.dumps({"events": events, "count": len(events),
                       "ts": int(time.time())}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            LOG.info("WEBHOOK POST BATCH (%d events) -> HTTP %s", len(events), r.status)
            return True
    except Exception as e:
        LOG.error("WEBHOOK POST BATCH FAILED (%d events) -> %r", len(events), e)
        return False

REGISTRY_FILE = _cfg("registry_file", "~/.hermes/scripts/cc-watch/fleet_registry.json")
PENDING_FILE = _cfg("pending_file", "~/.hermes/scripts/cc-watch/fleet_watch_pending.json")
LOCK_FILE = _cfg("lock_file", "~/.hermes/scripts/cc-watch/fleet_watch.lock")

def acquire_lock():
    """Single-instance guard: flock the lock file NON-BLOCKING. Returns the fd on
    success, or None if another daemon already holds it (refuse to double-run and
    double-process every event). flock auto-releases on process death, so a crash
    never leaves a stale lock needing cleanup."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except Exception as e:
        LOG.error("another fleet-watch daemon is running (%s locked): %r", LOCK_FILE, e)
        return None

def _load_pending():
    """Reload the buffered (not-yet-flushed) events so a daemon restart does NOT
    lose an in-flight digest batch. Defaults empty; old events flush immediately."""
    try:
        if os.path.exists(PENDING_FILE):
            d = json.load(open(PENDING_FILE))
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return []

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
                # ROLE-FILTER (root fix for false positives): only treat lines the
                # MODEL (Claude) emitted as sentinels. Dispatcher/instruction text I
                # paste lands as type="user", so skipping non-assistant lines stops
                # the watcher from firing on the "end with DONE-<id>" echo in every
                # ask (the simultaneous 17:28:20 flood we saw). A sentinel nobody
                # emitted is a false positive.
                role = (obj.get("message") or {}).get("role") or obj.get("type")
                if role != "assistant":
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
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)                    # atomic state write (crash-safe)
    except Exception as e:
        LOG.warning("state persist failed: %r", e)
    return events


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
        lk = acquire_lock()
        if lk is None:
            LOG.error("refusing to start: another fleet-watch daemon already holds the lock")
            sys.exit(1)
        LOG.info("fleet-watch daemon start pid=%s interval=%.1fs target=%s",
                 os.getpid(), a.interval, a.to)
        # NOTE: events are APPENDED to EVENTS (for a cron monitor_script that wakes the
        # Hermes agent to act), NOT sent straight to the user — a passthrough DM would
        # bypass the agent entirely (lesson: user flagged a raw 'cc-x: done' that I never saw).
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        _pending = _load_pending()                # resume undelivered events across restart
        DEBOUNCE = float(os.environ.get("FLEET_DIGEST_DEBOUNCE", "15"))   # measured burst window
        MAX_BATCH = int(os.environ.get("FLEET_DIGEST_MAX", "12"))          # digest legibility cap
        if _pending:
            LOG.info("RESUMED %d pending events from previous run", len(_pending))
        def _persist():
            try:
                tmp = PENDING_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(_pending, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, PENDING_FILE)     # atomic: a crash can't corrupt the file
            except Exception as e:
                LOG.warning("pending persist failed: %r", e)
        def _flush():
            if not _pending:
                return
            batch = _pending[:]
            del _pending[:]
            _persist()                            # durable: clear file only after we took the batch
            if WH_URL and post_batch(batch):
                return                            # one webhook POST -> handler makes ONE digest
            LOG.warning("webhook down -> events file fallback (%d events)", len(batch))
            with open(EVENTS, "a", encoding="utf-8") as f:
                for e in batch:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e['session']}: {e['match']}\n")
            LOG.info("FLUSH %d events to events file", len(batch))
        while True:
            try:
                added = False
                for sn, match in scan(a.patterns):
                    urgent = match.startswith("NEEDS-INPUT-") or "traceback" in match.lower()
                    _pending.append({"session": sn, "match": match,
                                     "at": int(time.time()), "urgent": urgent})
                    added = True
                if added:
                    _persist()                    # durable even if we crash inside the window
                now = time.time()
                if any(e["urgent"] for e in _pending):
                    _flush()                      # urgent -> flush now (still one summarized digest)
                elif _pending and (now - _pending[0]["at"] >= DEBOUNCE or len(_pending) >= MAX_BATCH):
                    _flush()                      # routine -> debounce into ONE batched digest
            except Exception as e:
                LOG.exception("daemon scan/send loop error")
            time.sleep(a.interval)
        return

    # monitor_script mode: print matches so a changed output wakes the cron agent.
    events = scan(a.patterns)
    if events:
        print("\n".join(f"{sn}: {match}" for sn, match in events))


if __name__ == "__main__":
    main()
