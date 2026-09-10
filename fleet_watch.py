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
import argparse, os, sys, re, json, glob, time, pathlib, logging, logging.handlers, hmac, hashlib, urllib.request, urllib.error, subprocess, fcntl, secrets

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
SLUG_FILE = _cfg("slug_file", "~/.hermes/scripts/cc-watch/fleet_watch.slug")

def _rand_slug():
    return "fw" + secrets.token_hex(4)          # fresh random namespace per daemon run

def _write_slug(slug):
    try:
        tmp = SLUG_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(slug); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, SLUG_FILE)
    except Exception as e:
        LOG.warning("slug write failed: %r", e)

def _read_slug():
    try:
        s = open(SLUG_FILE).read().strip()
        return s if re.fullmatch(r"fw[0-9a-f]{8}", s) else None
    except Exception:
        return None

def _token_pats(slug):
    """Exact, case-sensitive, whole-token sentinel patterns scoped to this run's slug.
    Collision-hard: a random per-run slug prefix + a whole-token match means a token
    only fires when it is exactly the sentinel for THIS run (never an instruction echo,
    a substring, or a differently-cased / prior-run match)."""
    return [
        re.compile(r"\bDONE-" + re.escape(slug) + r"-[A-Za-z0-9_.:-]+\b"),
        re.compile(r"\bNEEDS-INPUT-" + re.escape(slug) + r"-[A-Za-z0-9_.:-]+\b"),
        re.compile(r"\btraceback\b"),            # exact + case-sensitive whole word
    ]

def _generic_pats():
    """Generic sentinel tokens (session-lifetime notifications), matched on Claude's own
    lines only (the role-filter already excludes dispatcher text). Broader net than the
    slug-namespaced per-task tokens: catches DONE- / NEEDS-INPUT- / ERROR- / PROGRESS-
    emitted by a session without a task-specific slug. Whole-token, case-sensitive."""
    return [
        re.compile(r"\bDONE-[A-Za-z0-9_.:-]+\b"),
        re.compile(r"\bNEEDS-INPUT-[A-Za-z0-9_.:-]+\b"),
        re.compile(r"\bERROR-[A-Za-z0-9_.:-]+\b"),
        re.compile(r"\bPROGRESS-[A-Za-z0-9_.:-]+\b"),
    ]

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
WATCH_FILE = _cfg("watch_file", "~/.hermes/scripts/cc-watch/fleet_watch_requests.json")
ACK_FILE = _cfg("ack_file", "~/.hermes/scripts/cc-watch/fleet_watch_acks.json")
USERMSG_FILE = _cfg("user_msgs_file", "~/.hermes/scripts/cc-watch/fleet_user_msgs.json")
STALL_WINDOW = int(_cfg("stall_window", "120"))   # seconds of no transcript growth before a STALL check-in fires (alive-but-silent; default 120s = 2m; overridable via FLEET_CONFIG JSON key "stall_window")
SUSPEND_GAP = int(_cfg("suspend_gap", "300"))      # daemon scan-gap longer than this = machine slept/lid closed; rebaseline, no false STALL



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

def _load_watches():
    """Sentinel watches = what the dispatcher tells the daemon it's waiting for:
    {id, session, token, deadline(unix), created, note}. Read from a control file the
    dispatcher writes each time it arms a watch. The daemon satisfies a watch when the
    token appears as a model line on that session, or emits a SENTINEL-MISSED event the
    instant the deadline passes with no token (which wakes the agent to check in,
    re-estimate the ETA, and re-arm). This is how a hung agent gets caught by a
    deadline instead of being silently missed."""
    try:
        d = json.load(open(WATCH_FILE))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save_watches(ws):
    try:
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ws, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, WATCH_FILE)          # atomic: a crash can't corrupt the control file
    except Exception as e:
        LOG.warning("watch persist failed: %r", e)


def process_watches(matched):
    """Sweep active sentinel watches against this tick's matched tokens.
    - satisfied: the watched token appeared on that session's model lines -> drop watch
    - expired:   deadline passed with no token -> emit SENTINEL-MISSED (fires the agent)
    These events only fire on MODEL-emitted lines (matched already role-filtered).
    Returns (session, match) events; persists the remaining watches."""
    _now = int(time.time())
    watches = _load_watches()
    events = []
    kept = []
    for w in watches:
        sn = w.get("session", ""); tok = w.get("token", "")
        hit = any(tok in toks for s2, toks in matched.items() if s2 == sn)
        if w.get("_satisfied") or hit:
            if hit:
                LOG.info("WATCH-SATISFIED %s %s", sn, tok)
            continue
        dl = w.get("deadline", 0)
        if _now >= int(dl or 0):
            events.append((sn, f"SENTINEL-MISSED-{sn.upper()}: expected {tok} by {dl} — check in on this session (re-estimate ETA + re-arm the watch)"))
            LOG.warning("WATCH-EXPIRED %s %s deadline=%s", sn, tok, dl)
            continue
        kept.append(w)
    if len(kept) != len(watches):
        _save_watches(kept)
    return events


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


def _probe_liveness(sn, entry):
    """A managed Claude session is ALIVE iff its tmux session exists. Each managed session is
    launched as `tmux new-session -d -s <name> \"... claude ...\"`, so the tmux session IS the
    pane; when claude exits the window closes and the session dies. tmux has-session is therefore
    the reliable liveness truth. (NOT proc matching by config_dir: CLAUDE_CONFIG_DIR is an env
    var, not in argv, so pgrep-by-path gives false-negatives. Returns (bool, detail).)
    Class-fix: the daemon used to be transcript-only, so a session that died went silent."""
    try:
        ok = subprocess.run(["tmux", "has-session", "-t", sn],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        ok = False
    return (ok, "ALIVE" if ok else "no tmux session")


_USAGE_LIMIT_RE = re.compile(r"hit your (session|weekly|usage) limit|usage limit|/upgrade to increase", re.I)
_RESET_RE = re.compile(r"resets\s+(.+?)\s*[\)\n]", re.I)


def _probe_usage_limit(sn):
    """Return (limited: bool, reset_hint: str|None) by scanning the session's tmux pane for a
    credit/usage-limit banner (Claude Code shows \"You've hit your session limit · resets <t> /
    /upgrade to increase your usage limit\"). So the orchestrator becomes AWARE of credit gates
    instead of dispatching into a gated (silent) session."""
    try:
        out = subprocess.run(["tmux", "capture-pane", "-t", sn, "-p"],
                             capture_output=True, text=True, timeout=10).stdout or ""
    except Exception:
        return False, None
    if not _USAGE_LIMIT_RE.search(out):
        return False, None
    r = _RESET_RE.search(out)
    return True, (r.group(1).strip() if r else None)


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
    matched = {}
    userturns = {}
    _now_i = int(time.time())   # used by sleep-gap + STALL logic
    # SLEEP / LID-CLOSE HANDLING (2026-09-09): if the gap between daemon scans is huge
    # (> SUSPEND_GAP), the MACHINE slept/suspended — the agents had no chance to emit, so a
    # long-silent transcript is NOT a stall. Rebaseline every session's STALL clock and
    # transcript size on the woke scan (no false STALL, no re-processing pre-sleep bytes).
    _last_scan = state.get("_last_scan", _now_i)
    state["_last_scan"] = _now_i
    if _now_i - _last_scan > SUSPEND_GAP:
        for k in list(state.setdefault("_stall", {})):
            state["_stall"][k] = {"since": _now_i, "flagged": False}
        for _sn in SESSIONS():
            _jl = find_jsonl(_sn)
            if _jl:
                try:
                    state[_sn] = os.path.getsize(_jl)
                except Exception:
                    pass
        LOG.info("machine sleep/lid-close detected (gap %ss) — rebaselined STALL + sizes, no false alerts", _now_i - _last_scan)
    for sn in SESSIONS():
        # LIVENESS (class-fix): surface a tracked session that went dead instead of going
        # silent. Baseline on first sight (no event); only real ALIVE->dead transitions fire.
        entry = load_registry().get(sn) or {}
        alive, ldetail = _probe_liveness(sn, entry)
        lv = state.setdefault("_liveness", {})
        prev = lv.get(sn, {}).get("alive")
        if prev is True and not alive:
            events.append((sn, f"SESSION-DEAD-{sn.upper()}: {ldetail}"))
        lv[sn] = {"alive": alive, "detail": ldetail, "ts": int(time.time())}
        # USAGE-LIMIT (credit-gate awareness): detect a profile out of usage credits. Throttled
        # pane scan so the orchestration becomes AWARE instead of dispatching into gated sessions.
        ul = state.setdefault("_usage_limit", {})
        ul.setdefault(sn, {"limited": False, "reset": None, "checked_at": 0, "ts": 0})
        en = ul[sn]
        now_t = int(time.time())
        if now_t - en["checked_at"] >= 45:
            prev = en["limited"]
            en["checked_at"], en["ts"] = now_t, now_t
            en["limited"], en["reset"] = _probe_usage_limit(sn)
            if en["limited"] and not prev:
                events.append((sn, f"USAGE-LIMIT-{sn.upper()}: credit/session limit hit" +
                               (f" (resets {en['reset']})" if en['reset'] else "")))
        jl = find_jsonl(sn)
        if not jl:
            if sn not in _unresolved_reported:
                LOG.warning("no jsonl resolved for %s", sn)
                if alive:
                    events.append((sn, f"NO-TRANSCRIPT-{sn.upper()}: registered but no session jsonl (not recoverable)"))
                _unresolved_reported.add(sn)
            continue
        try:
            size = state.get(sn, 0)
            cur = os.path.getsize(jl)
            if cur < size:
                size = 0
            # STALL detection (alive-but-silent): a live session whose transcript has
            # not grown for STALL_WINDOW seconds has stopped producing — emit a check-in
            # event so the supervisor notices (compaction, a parked prompt, or a hung
            # loop all look identical to "the agent went quiet"). Urgent, so it surfaces.
            _now_i = int(time.time())
            _st = state.setdefault("_stall", {})
            _prev = _st.get(sn, {"since": _now_i, "flagged": False})
            if cur > size or size == 0:
                _st[sn] = {"since": _now_i, "flagged": False}           # grew / first sight -> reset clock
            elif alive and not _prev["flagged"] and (_now_i - _prev["since"] >= STALL_WINDOW):
                events.append((sn, f"STALL-{sn.upper()}: no new transcript output for {STALL_WINDOW}s (alive, not producing) — check in on this session"))
                _st[sn] = {"since": _prev["since"], "flagged": True}    # fire once until it grows again
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
                txt = extract_text(obj)
                if not txt:
                    continue
                if role == "user":
                    userturns.setdefault(sn, []).append(txt)   # delivery-ACK: user turns = what was actually landable
                if role != "assistant":
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
                        matched.setdefault(sn, set()).add(m.group(0))
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
    return events, matched, userturns


def _load_acks():
    try:
        return json.load(open(os.path.expanduser(ACK_FILE)))
    except Exception:
        return []

def _save_acks(acks):
    tmp = ACK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(acks, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, ACK_FILE)   # atomic, crash-safe

RING_MINUTES = 60
RING_CAP = 8000

def _load_ring():
    try:
        return json.load(open(os.path.expanduser(USERMSG_FILE)))
    except Exception:
        return []

def _save_ring(ring):
    tmp = USERMSG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ring, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, USERMSG_FILE)   # atomic, crash-safe

def ingest_user_msgs(userturns):
    """Hold the last RING_MINUTES of USER messages per session (2026-09-10): a queryable rolling
    buffer of what each session was actually told (real user-role text turns — Terra's remote feedback,
    dispatcher pastes, scheduled sweeps), so 'what did this session get told?' has ONE authoritative
    answer instead of hand-parsing a giant jsonl. Pane-swallowed dispatches never land here, which is
    itself the delivery truth."""
    if not userturns:
        return
    now = time.time()
    ring = _load_ring()
    for sn, texts in userturns.items():
        for t in texts:
            ring.append({"at": now, "session": sn, "text": t[:3000]})
    # prune to the window + cap
    ring = [e for e in ring if now - e.get("at", 0) <= RING_MINUTES * 60][-RING_CAP:]
    if ring:
        _save_ring(ring)

def cmd_user_lines(argv):
    """fleet_watch.py user-lines [--session <reg>] [--minutes N] — print the daemon's rolling
    USER-message buffer (last N minutes, default 60) for one session or all."""
    import argparse as _a
    ap = _a.ArgumentParser("fleet_watch user-lines")
    ap.add_argument("--session", default="")
    ap.add_argument("--minutes", type=int, default=RING_MINUTES)
    a = ap.parse_args(argv)
    ring = _load_ring()
    cutoff = time.time() - a.minutes * 60
    rows = [e for e in ring if e.get("at", 0) >= cutoff and (not a.session or e.get("session") == a.session)]
    if not rows:
        print("(no user messages in window)")
        return 0
    for e in rows:
        ts = time.strftime("%H:%M:%S", time.localtime(e["at"]))
        print(f"[{ts}] {e['session']}: {e['text']}")
    return 0


def process_acks(userturns):
    """DELIVERY-ACK (2026-09-10): a dispatch is only 'delivered' once the payload appears as a
     real USER turn on the session's canonical transcript — pane-visibility is NOT proof (a busy
    auto-mode pane can swallow the paste before submit). Arm an ack before dispatching; the daemon
    satisfies it when the marker lands as a user turn within the deadline, else fires ACK-MISSED
    (urgent) so the agent re-delivers instead of believing a false 'LANDED'."""
    acks = _load_acks()
    if not acks:
        return []
    now = time.time(); keep = []; fired = []
    for ak in acks:
        sn, marker, dl = ak.get("session"), ak.get("marker"), ak.get("deadline", now)
        seen = any(marker in t for t in userturns.get(sn, []))
        if seen:
            LOG.info("ACK-OK %s | %s", sn, marker)
            continue                                   # satisfied + removed (not fired as an event)
        if now >= dl:
            fired.append((sn, f"ACK-MISSED-{sn.upper()}: dispatch '{marker}' NOT received as a user turn by deadline — re-deliver (it was swallowed / never submitted)"))
            LOG.info("ACK-MISSED %s | %s", sn, marker)
            continue
        keep.append(ak)
    if len(keep) != len(acks):
        _save_acks(keep)
    return fired


def cmd_watch(argv):
    """Dispatcher control-plane for sentinel watches (the ETA-negotiation seam):
      fleet_watch.py watch add --session <reg> --token <DONE-...> [--deadline-min N] [--note ...]
      fleet_watch.py watch list
      fleet_watch.py watch cancel --id <id>
    The daemon satisfies the watch when the token is seen on that session, or fires a
    SENTINEL-MISSED event at the deadline (so a hung agent is caught, not silently missed)."""
    ap = argparse.ArgumentParser("fleet_watch watch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("add")
    pa.add_argument("--session", required=True)
    pa.add_argument("--token", required=True)
    pa.add_argument("--deadline-min", type=float, default=15.0)
    pa.add_argument("--note", default="")
    sub.add_parser("list")
    pc = sub.add_parser("cancel"); pc.add_argument("--id", required=True)
    pak = sub.add_parser("ack")
    pak.add_argument("--session", required=True)
    pak.add_argument("--marker", required=True)
    pak.add_argument("--deadline-min", type=float, default=0.7)
    pak.add_argument("--note", default="")
    pck = sub.add_parser("ack-cancel"); pck.add_argument("--id", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "add":
        ws = _load_watches(); wid = secrets.token_hex(3)
        ws.append({"id": wid, "session": a.session, "token": a.token,
                   "deadline": int(time.time()) + int(a.deadline_min * 60),
                   "created": int(time.time()), "note": a.note})
        _save_watches(ws)
        print(f"WATCH ARMED id={wid} session={a.session} token={a.token} deadline={a.deadline_min}min")
    elif a.cmd == "list":
        ws = _load_watches()
        print("\n".join(f"{w['id']} {w.get('session')} {w.get('token')} dl={w.get('deadline')} note={w.get('note','')}" for w in ws) if ws else "no active watches")
    elif a.cmd == "cancel":
        ws = [w for w in _load_watches() if w.get("id") != a.id]; _save_watches(ws)
        print(f"cancelled {a.id}")
    elif a.cmd == "ack":
        acks = _load_acks(); aid = secrets.token_hex(3)
        acks.append({"id": aid, "session": a.session, "marker": a.marker,
                     "deadline": int(time.time()) + int(a.deadline_min * 60),
                     "created": int(time.time()), "note": a.note})
        _save_acks(acks)
        print(f"ACK ARMED id={aid} session={a.session} marker={a.marker} deadline={a.deadline_min}min")
    elif a.cmd == "ack-cancel":
        _save_acks([x for x in _load_acks() if x.get("id") != a.id])
        print(f"ack cancelled {a.id}")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "watch":
        return cmd_watch(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "user-lines":
        return cmd_user_lines(sys.argv[2:])
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
        slug = _rand_slug()
        _write_slug(slug)
        LOG.info("sentinel namespace slug=%s (exact, case-sensitive matching)", slug)
        scan_pats = _token_pats(slug) + _generic_pats()   # slug-scoped per-task + generic session notifications
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
                scan_events, matched, userturns = scan(scan_pats)
                ingest_user_msgs(userturns)                       # rolling 1h user-message buffer
                # Sentinel watches: satisfy on a seen token; fire SENTINEL-MISSED on expiry.
                # Delivery-ACKs: confirm a dispatch landed as a USER turn; fire ACK-MISSED if not.
                scan_events = scan_events + process_watches(matched) + process_acks(userturns)
                for sn, match in scan_events:
                    urgent = (match.startswith("NEEDS-INPUT-") or "traceback" in match.lower()
                              or match.startswith("SENTINEL-MISSED-") or match.startswith("ACK-MISSED-"))
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
    events, _, _ = scan(a.patterns)
    if events:
        print("\n".join(f"{sn}: {match}" for sn, match in events))


if __name__ == "__main__":
    main()
