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
import argparse, os, sys, re, json, glob, time, pathlib, logging, logging.handlers, hmac, hashlib, urllib.request, urllib.error, urllib.parse, subprocess, fcntl, secrets

DEFAULT_PAT = [r"\bDONE-[A-Za-z0-9_.:-]+\b", r"\bNEEDS-INPUT-[A-Za-z0-9_.:-]+\b", r"\b[tT]raceback\b"]
LOG = logging.getLogger("fleetwatch")

# ---- config (dogfood: all paths come from a config file; back-compat defaults) ----
# Launch specs live in config (fleet_harness): no profile name, harness or flag is
# fixed in code, so any harness and any env vars/flags can be declared.
# Aliased so this block does not depend on where the file's own imports sit.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import fleet_harness as _harness


def _load_cfg():
    """Config: FLEET_CONFIG when exported, otherwise the first config file found beside this
    module or one level up (fleet_harness.candidate_paths, so discovery cannot drift).

    Reading ONLY FLEET_CONFIG meant a deployment that never exported it -- a launchd
    daemon, for instance -- silently ran on the built-in defaults while its config file
    sat unread on disk. The file existed, was maintained, and had no effect.
    """
    for p in _harness.candidate_paths():
        if not os.path.isfile(p):
            continue
        try:
            if p.endswith((".yaml", ".yml")):
                import yaml
                return yaml.safe_load(open(p)) or {}
            return json.load(open(p))
        except Exception as e:
            print(f"fleet-config load failed for {p}: {e!r}", flush=True)
    return {}

_CFG = _load_cfg()
def _cfg(key, default):
    v = _CFG.get(key)
    if v is None:
        v = default
    # expand ~ only for path-like string values; leave ints/bools/etc untouched
    # (2026-09-11: a numeric config value like "stall_window": 360 hit
    # os.path.expanduser(int) -> TypeError, which would crash the daemon at
    # import. Never assume config values are paths.)
    if isinstance(v, str):
        return os.path.expanduser(v)
    return v

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
        # CASE-INSENSITIVE on purpose: Python prints "Traceback (most recent call last)",
        # so the case-sensitive form never fired for the errors it exists to surface.
        # Slug-scoped tokens above stay case-sensitive (collision-hard); this one is a
        # probe for the word itself.
        re.compile(r"(?i)\btraceback\b"),
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

# NOTE: STATE/EVENTS come from config (see the _cfg assignments above). A hardcoded
# reassignment here silently overrode them.

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
                    if k in ("FLEET_WEBHOOK_URL", "FLEET_WEBHOOK_SECRET",
                             "TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"):
                        out[k] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return out

_fleet_env = _load_fleet_env()
WH_URL = os.environ.get("FLEET_WEBHOOK_URL") or _fleet_env.get("FLEET_WEBHOOK_URL")
WH_SECRET = os.environ.get("FLEET_WEBHOOK_SECRET") or _fleet_env.get("FLEET_WEBHOOK_SECRET")

# Receipt-only event kinds. These confirm that something already happened: a dispatch landed
# (ACK-OK), a watch found its token (WATCH-SATISFIED), or a watch ran out of time
# (WATCH-EXPIRED). None of them asks the operator for anything, and the daemon log plus the
# session transcript remain the record. Measured 2026-09-29: 423 of 427 events in a day were
# these kinds, delivered as one webhook digest each, which put a notification on the user's
# phone every 3.3 minutes. They are logged, never delivered.
SUPPRESS_KINDS = ("ACK-OK", "WATCH-SATISFIED", "WATCH-EXPIRED")


# Fleet channel (lane 2 receipts). The fleet platform plugin creates this spool directory when
# it connects; the daemon drops one JSON file per event into it and the plugin posts them to
# the channel. No directory = plugin not live, and lane 2 stays log-only as before. A spool
# write that fails lands in the events file, so a receipt is never lost.
FLEET_SPOOL = _cfg("fleet_spool", "~/.hermes/cache/fleet-channel/spool")


import itertools as _itertools
_SPOOL_SEQ = _itertools.count()


def post_fleet_channel(session, match, spool=None):
    """Queue a lane 2 event for the fleet channel. Returns True when it was queued or
    written to the events file, False when the channel is not live (nothing written)."""
    spool = os.path.expanduser(spool or FLEET_SPOOL)
    if not os.path.isdir(spool):
        return False
    at = time.time()
    name = "%d-%08d-%s" % (int(at * 1000), next(_SPOOL_SEQ), os.urandom(3).hex())   # a counter orders same-millisecond events
    try:
        tmp = os.path.join(spool, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"session": session, "match": match, "at": int(at)}, f)
        os.replace(tmp, os.path.join(spool, name + ".json"))   # atomic: the poller never sees half a file
        return True
    except Exception as e:
        LOG.warning("fleet channel spool failed (%r) -> events file fallback", e)
        try:
            with open(os.path.expanduser(EVENTS), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {session}: {match}\n")
        except Exception as e2:
            LOG.error("events file fallback failed too: %r", e2)
        return True


def is_bookkeeping(match: str) -> bool:
    """True for events that need no human: the kinds above, plus a plain completion token.

    A bare DONE-... token means a session finished something. That is bookkeeping too: it is
    what the fleet_state view is for. Suppressing it here does NOT affect the watch machinery,
    which consumes the matched-token list directly, only what reaches the user's chat.
    """
    m = (match or "").strip()
    if any(m.startswith(k) for k in SUPPRESS_KINDS):
        return True
    return bool(re.match(r"^DONE[-_.]", m, re.I))



ARMED_FILE = _cfg("armed_file", "~/.hermes/scripts/cc-watch/overwatch/armed.json")
DELIVER_ALL = os.environ.get("FLEET_DELIVER_ALL", "") not in ("", "0", "false")
GATE_URL = os.environ.get("FLEET_GATE_URL", "http://127.0.0.1:11436/decide")
GATE_TIMEOUT = float(os.environ.get("FLEET_GATE_TIMEOUT", "3"))
GATE_LOG = os.path.expanduser(os.environ.get("FLEET_GATE_LOG",
                                             "~/.hermes/logs/laya-gate-decisions.jsonl"))
GATE_PY = os.path.expanduser(os.environ.get(
    "FLEET_GATE_PY", "~/Developer/hermes-fleet-manager/laya_gate.py"))
GATE_PYTHON = os.path.expanduser(os.environ.get(
    "FLEET_GATE_PYTHON", "~/.hermes/venvs/laya-mlx/bin/python"))
_GATE_SPAWNED_AT = [0.0]
# The gate normally runs under its own LaunchAgent (KeepAlive). When it does, launchd is the ONE
# owner of its process: the daemon asks launchd to restart it and never starts a second copy, because
# two owners race for the port and whichever loses either crash-loops or holds a broken gate.
GATE_LABEL = os.environ.get("FLEET_GATE_LABEL", "com.terra.laya-gate")
GATE_RESTART_EVERY = float(os.environ.get("FLEET_GATE_RESTART_EVERY", "300"))   # throttle
GATE_CHECK_EVERY = float(os.environ.get("FLEET_GATE_CHECK_EVERY", "60"))        # health poll
GATE_LOAD_GRACE = float(os.environ.get("FLEET_GATE_LOAD_GRACE", "300"))         # model load
# How long the gate may be unusable before the operator is told directly. Her number for a
# stopped session is ten minutes; a stopped gate silences every session at once, so it gets the same.
GATE_ALERT_AFTER = float(os.environ.get("FLEET_GATE_ALERT_AFTER", "600"))
_GATE = {"checked_at": 0.0, "bad_since": None, "loading_since": None, "alerted": False}


def _gate_port():
    """The port the gate listens on, taken from the URL the daemon actually calls."""
    try:
        return urllib.parse.urlsplit(GATE_URL).port or 11436
    except Exception:
        return 11436


def _gate_health():
    """The gate's /health as a dict, or None if nothing usable answered."""
    try:
        with urllib.request.urlopen(GATE_URL.replace("/decide", "/health"), timeout=1.5) as r:
            h = json.loads(r.read())
        return h if isinstance(h, dict) else None
    except Exception:
        return None


def _launchd_target():
    return f"gui/{os.getuid()}/{GATE_LABEL}"


def _launchd_owns_gate():
    """Whether launchd has the gate's job loaded, i.e. whether it is the gate's owner."""
    try:
        return subprocess.run(["launchctl", "print", _launchd_target()], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


def _stop_unhealthy_gate():
    """Stop the process LISTENING on the gate port, so it can be replaced by one that works.

    Only for a gate launchd does not own: one started by hand, which is how a gate ends up on the
    wrong interpreter. `-sTCP:LISTEN` matters -- without it lsof also lists every process with a
    connection open to the port, and a CLI call or an eval run mid-request would be killed with it.
    """
    port = _gate_port()
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10)
    except Exception as e:
        LOG.warning("could not look up the process listening on port %s: %r", port, e)
        return False
    killed = []
    for pid in (out.stdout or "").split():
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            os.kill(int(pid), 15)
            killed.append(pid)
        except Exception as e:
            LOG.warning("could not stop pid %s listening on port %s: %r", pid, port, e)
    if killed:
        LOG.warning("stopped unhealthy laya gate pid(s) %s", ",".join(killed))
        time.sleep(1.5)          # let the port free before the replacement binds it
    return bool(killed)


def _restart_gate(why):
    """Restart the gate through its owner. Throttled. Returns what was done, for the log."""
    if time.time() - _GATE_SPAWNED_AT[0] < GATE_RESTART_EVERY:
        return "throttled"
    _GATE_SPAWNED_AT[0] = time.time()
    if _launchd_owns_gate():
        try:
            p = subprocess.run(["launchctl", "kickstart", "-k", _launchd_target()],
                               capture_output=True, text=True, timeout=20)
            if p.returncode == 0:
                LOG.warning("laya gate %s; asked launchd to restart %s", why, GATE_LABEL)
                return "kickstarted"
            LOG.warning("launchctl kickstart %s failed (%s): %s", GATE_LABEL, p.returncode,
                        (p.stderr or p.stdout or "").strip()[:200])
        except Exception as e:
            LOG.warning("launchctl kickstart %s raised %r", GATE_LABEL, e)
        return "kickstart-failed"
    _stop_unhealthy_gate()
    try:
        with open(os.path.expanduser("~/.hermes/logs/laya-gate.log"), "ab") as f:
            subprocess.Popen([GATE_PYTHON, GATE_PY], stdout=f, stderr=f, start_new_session=True)
        LOG.warning("laya gate %s; started it (no LaunchAgent %s loaded)", why, GATE_LABEL)
        return "spawned"
    except Exception as e:
        LOG.warning("could not start the laya gate: %r", e)
        return "spawn-failed"


def ensure_gate():
    """Check the escalation gate and restart it if it is not usable. Throttled, never raises.

    Returns "ok", "loading" or "bad". Only "ok" is evidence the gate can decide.

    Liveness is NOT readiness. A gate on the wrong interpreter answers /health perfectly while
    being unable to import laya-mlx, so every decision silently falls back to the standing policy
    -- 27 decisions over 15 hours did exactly that (2026-10-01). The gate now loads its model at
    startup, so /health reports a broken model before the first decision rather than after it,
    and this is polled from the daemon loop (gate_watch), not only at startup.
    """
    try:
        h = _gate_health()
        if h is not None and h.get("model_loaded") and not h.get("model_error"):
            _GATE["loading_since"] = None
            return "ok"
        if h is not None and not h.get("model_error") and h.get("model_loaded") is False:
            # Up and still loading. Give it the grace period before calling it stuck.
            since = _GATE["loading_since"] = _GATE["loading_since"] or time.time()
            if time.time() - since < GATE_LOAD_GRACE:
                return "loading"
            why = f"has been loading its model for {int(time.time() - since)}s"
        elif h is not None:
            why = f"answers but cannot load its model ({str(h.get('model_error'))[:140]})"
        else:
            why = "is not answering /health"
        _GATE["loading_since"] = None
        _restart_gate(why)
        return "bad"
    except Exception as e:
        LOG.warning("ensure_gate failed: %r", e)
        return "bad"


def gate_watch(now=None):
    """Poll the gate from the daemon loop. Returns events for the operator, at most one per outage.

    A gate that is down does not lose events -- the daemon shows them instead -- but it does lose
    the filter, and a gate that answers with a broken model loses it silently. So an outage that
    restarts cannot fix within GATE_ALERT_AFTER is reported directly, and so is its recovery.
    """
    now = time.time() if now is None else now
    if now - _GATE["checked_at"] < GATE_CHECK_EVERY:
        return []
    _GATE["checked_at"] = now
    status = ensure_gate()
    if status == "ok":
        was_alerted = _GATE["alerted"]
        _GATE.update(bad_since=None, alerted=False)
        if was_alerted:
            return [{"session": "laya-gate", "urgent": False,
                     "match": "GATE-RECOVERED: the laya escalation gate is deciding again"}]
        return []
    if status == "loading":
        return []
    _GATE["bad_since"] = _GATE["bad_since"] or now
    down = now - _GATE["bad_since"]
    if down >= GATE_ALERT_AFTER and not _GATE["alerted"]:
        _GATE["alerted"] = True
        return [{"session": "laya-gate", "urgent": True,
                 "match": (f"GATE-DOWN: the laya escalation gate has been unusable for "
                           f"{int(down / 60)} min and restarting it has not fixed it; every event "
                           f"is reaching you unfiltered. See ~/.hermes/logs/laya-gate.log")}]
    return []


def _ask_snippet(lines, cap=240):
    """The words around a NEEDS-INPUT/ERROR token: what the session actually asked for.

    A token alone ('NEEDS-INPUT-fw439ea...') tells the operator and the classifier nothing.
    The ask is always in the model's own lines just before the token. Returns '' when there
    is nothing usable. The token itself is stripped so the snippet cannot read as a repeat.
    """
    tail = [x.strip() for x in (lines or []) if x.strip()][-3:]
    if not tail:
        return ""
    out = " ".join(tail)
    out = re.sub(r"\b(?:NEEDS-INPUT|DONE|ERROR|PROGRESS)-[A-Za-z0-9_.:-]+", " ", out)
    out = re.sub(r"\s+", " ", out).strip(" -\u2014|")
    return out[:cap]


def event_context(sn, modeltext=None):
    """The situation behind an event, for the classifier.

    A classifier given one line can only judge the line. The finding this gate rests on is
    that triage accuracy came from adding history, per-type behaviour and explicit patterns
    to each prompt rather than from a better model. So the classifier is told who the session
    is, what it was last doing, how long it has been quiet, what it owes, and how often this
    kind of event has happened on it before.
    """
    bits = []
    e = load_registry().get(sn) or {}
    bits.append(f"session {e.get('short') or sn} on the {e.get('profile') or 'unknown'} profile, "
                f"working in {e.get('cwd') or 'an unknown directory'}")

    lines = (modeltext or {}).get(sn) or []
    if lines:
        last = " ".join(x.strip() for x in lines[-2:])[:280]
        bits.append(f"its most recent output was {last!r}")
    else:
        bits.append("it produced no new output on this scan")

    try:
        st = json.load(open(STATE)).get("_stall", {}).get(sn)
        if st and st.get("since"):
            bits.append(f"quiet for {int(time.time() - st['since'])}s "
                        f"(the stall window is {STALL_WINDOW}s)")
    except Exception as e:
        LOG.debug("stall state unreadable for context: %r", e)

    try:
        for w in json.load(open(os.path.expanduser(WATCH_FILE))) or []:
            if w.get("session") == sn:
                due = max(int(w.get("deadline", 0) - time.time()), 0)
                bits.append(f"it owes the completion token {w.get('token')} within {due}s")
    except Exception as e:
        LOG.debug("watch file unreadable for context: %r", e)

    return "; ".join(bits)


def sessions_expecting():
    """Sessions with work in flight: an armed completion watch, or a pending dispatch ack.

    A stall means something different for these sessions than for the rest of the fleet. An
    idle session going quiet is the normal state; a session that owes a token going quiet is
    a stop worth reporting.
    """
    out = set()
    for path in (WATCH_FILE, ACK_FILE):
        try:
            with open(os.path.expanduser(path)) as f:
                for w in json.load(f) or []:
                    if isinstance(w, dict) and w.get("session"):
                        out.add(w["session"])
        except Exception as e:
            LOG.debug("could not read %s: %r", path, e)
    return out


def _decision_id(at, session, text):
    """A stable short id for one decision, so a review can reference it when labelling."""
    return hashlib.sha1(f"{at}|{session}|{text[:200]}".encode()).hexdigest()[:8]


def _append_decision(entry):
    """Append one gate decision, rotating at 5 MB so a month of review stays openable."""
    try:
        if os.path.exists(GATE_LOG) and os.path.getsize(GATE_LOG) > 5_000_000:
            os.replace(GATE_LOG, GATE_LOG + ".1")
        with open(GATE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        LOG.debug("gate decision log failed: %r", e)



# -- daemon-owned nudges ----------------------------------------------------
# Silence on an IDLE session is machine-handled HERE, not by the overwatch cron:
# the daemon nudges, counts, and tells the gate. The operator hears about a stall
# only when nudges were sent and failed to revive the session.
#
# NUDGE STATE INTERFACE (what the gate receives as `nudges`; None when the session
# is not IDLE, i.e. nothing is being nudged):
#
#     {"episode_id":     str,         # "<session>@<episode start, epoch s>"; changes per stop
#      "nudge_count":    int,         # nudges that LANDED in this episode, 0..NUDGE_MAX
#      "first_nudge_at": int | None,  # epoch s of the first landed nudge, None if none
#      "last_nudge_at":  int | None,  # epoch s of the latest landed nudge, None if none
#      "nudge_max":      int}         # this episode's cap; the gate escalates after exhaustion
#
# An EPISODE is one continuous stop. It ends (counts reset) when the session leaves
# IDLE (WORKING, QUEUED, NEEDS-INPUT, DEAD) or when a new stop begins (the stop's
# start time moves by more than NUDGE_EPISODE_SLACK seconds). The cap is the
# fleet manager's own max_nudges, read from scripts/fleet_manager.py, never a copy;
# the config key "nudge_max" can override it.

NUDGE_INTERVAL = int(_cfg("nudge_interval", "600"))    # 10 min between nudges in one stop
NUDGE_EPISODE_SLACK = 120


def _manager_max_nudges():
    """The fleet manager's max_nudges default: the single source of the per-episode cap."""
    import dataclasses
    scripts = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import fleet_manager
    for f in dataclasses.fields(fleet_manager.Manager):
        if f.name == "max_nudges":
            return int(f.default)
    raise RuntimeError("fleet_manager.Manager has no max_nudges field")


NUDGE_MAX = int(_cfg("nudge_max", _manager_max_nudges()))
_nudges = {}          # session -> episode dict (see NUDGE STATE INTERFACE)


def _episode_for(sn, stopped, now):
    """The live episode for this stop, starting a fresh one when the stop is a new one."""
    began = now - stopped
    ep = _nudges.get(sn)
    if not ep or abs(ep["began"] - began) > NUDGE_EPISODE_SLACK:
        ep = _nudges[sn] = {"episode_id": "%s@%d" % (sn, int(began)), "began": began,
                            "nudge_count": 0, "first_nudge_at": None, "last_nudge_at": None}
    return ep


def _nudge_view(ep):
    """The documented gate-facing structure, without the internal `began`."""
    view = {k: ep[k] for k in ("episode_id", "nudge_count", "first_nudge_at", "last_nudge_at")}
    view["nudge_max"] = NUDGE_MAX
    return view


def _dispatch_nudge(sn, text):
    """Send `text` through the existing fleet_dispatch.sh (polls for a clean prompt, refuses
    to paste mid-turn). True only when the dispatcher reports LANDED."""
    nf = os.path.expanduser(f"~/.hermes/cache/scratch/nudge-{sn}-{int(time.time())}.txt")
    try:
        os.makedirs(os.path.dirname(nf), exist_ok=True)
        with open(nf, "w") as f:
            f.write(text)
        r = subprocess.run(["bash", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "fleet_dispatch.sh"), sn, nf],
                           capture_output=True, text=True, timeout=90)
        return "LANDED" in (r.stdout or "")
    except Exception as e:
        LOG.warning("nudge dispatch failed %s: %r", sn, e)
        return False
    finally:
        try: os.remove(nf)
        except OSError: pass


USAGE_ASK_EVERY = 86400
_usage_asked = [0.0]


def _ask_usage_setup(now=None):
    """While the setting that says where Hermes' own agents bill is empty, say so once a day in the
    fleet channel. An explicit uncounted choice ends the question; so does naming a profile."""
    import usage_meter
    now = time.time() if now is None else now
    if now - _usage_asked[0] < USAGE_ASK_EVERY:
        return False
    state = usage_meter.hermes_setting()[0]
    if state != "unset":
        return False
    h = usage_meter.hermes_recent()
    if not h or not h[0]:
        return False                              # nothing to count: nothing to ask
    _usage_asked[0] = now
    return post_fleet_channel(
        "usage-meter",
        "Subscription usage: Hermes' own agents made %d API sessions (%.1fM tokens) in the last day and "
        "none of it is counted, because no profile is chosen. Open the Fleet channel plugin settings and "
        "pick the profile their account belongs to, or pick Uncounted." % (h[0], sum(h[1:]) / 1e6))


def _usage_factor(sn=None):
    """Nudge interval multiplier from the usage controller: below 1 when the session's subscription
    window is on course to finish under target, above 1 when it is on course to overshoot. Each
    session answers to its own profile's window. 1.0 when the meter is untrusted, missing or stale,
    or the session's profile is unknown, so a broken meter can never change fleet behaviour."""
    try:
        import usage_meter
        prof = (load_registry().get(sn) or {}).get("profile")
        return usage_meter.nudge_factor(".claude-%s" % prof) if prof else 1.0
    except Exception:
        return 1.0


def _maybe_nudge(sn, stopped, state="__live__", now=None):
    """Nudge an IDLE, silent session when due; return the nudge state dict (or None).

    Never nudges WORKING/QUEUED (or any non-IDLE) sessions, never during quiet hours,
    never past NUDGE_MAX per episode, never closer than NUDGE_INTERVAL. A non-LANDED
    dispatch changes nothing; the gate still sees the honest count. Every landed nudge
    is written as a decision row carrying the session STATE.
    """
    now = time.time() if now is None else now
    if state == "__live__":
        state = _session_state(sn)
    if state != "IDLE":
        _nudges.pop(sn, None)                    # left IDLE: the episode is over
        return None
    ep = _episode_for(sn, stopped, now)
    due = (ep["nudge_count"] < NUDGE_MAX and not _in_quiet_hours()
           and (ep["last_nudge_at"] is None
                or now - ep["last_nudge_at"] >= NUDGE_INTERVAL * _usage_factor(sn)))
    if due:
        text = (f"{sn}: you have been quiet {int(stopped // 60)} min. Continue the work and say "
                f"what you are doing, or emit NEEDS-INPUT naming what you need. Do not restate done.")
        if _dispatch_nudge(sn, text):
            ep["nudge_count"] += 1
            ep["first_nudge_at"] = ep["first_nudge_at"] or int(now)
            ep["last_nudge_at"] = int(now)
            _append_decision({"id": _decision_id(int(now), sn, "nudge"), "at": int(now),
                              "session": sn, "kind": "nudge", "text": text[:200],
                              "expecting": False, "state": state,
                              "verdict": {"lane": 0, "action": "nudged",
                                          "nudge": ep["nudge_count"],
                                          "episode_id": ep["episode_id"]}})
    return _nudge_view(ep)

def _session_state(sn):
    """The session's live state (WORKING/QUEUED/IDLE/NEEDS-INPUT/DEAD) from fleet_state.

    This is the input the STALL logic gate was missing: busy and idle silence are different
    events with different thresholds and different correct machine actions. Best effort:
    any failure returns None and the gate treats it as unknown.
    """
    try:
        repo_scripts = os.path.expanduser("~/Developer/hermes-fleet-manager/scripts")
        if repo_scripts not in sys.path:
            sys.path.insert(0, repo_scripts)
        import fleet_state
        return fleet_state.fingerprint(sn)
    except Exception:
        return None


def gate_verdict(session, kind, text, context="", expecting=False, state=None, nudges=None):
    """Ask the Laya escalation gate whether this event should reach the operator.

    Lane 1 (escalate) and lane 2 (silent) are decided inside the gate in code, so the
    model is only consulted for events whose form is not decisive. Returns the verdict
    dict, or None if the gate is unreachable, and the caller then fails toward showing
    the event rather than swallowing it.
    """
    payload = json.dumps({"session": session, "kind": kind, "text": text,
                          "context": context, "expecting": expecting,
                          "state": state, "nudges": nudges}).encode()
    req = urllib.request.Request(GATE_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=GATE_TIMEOUT) as r:
            verdict = json.loads(r.read())
    except Exception as e:
        LOG.warning("gate unreachable (%r); showing the event", e)
        ensure_gate()                             # try to bring it back for next time
        return None
    at = int(time.time())
    _append_decision({"id": _decision_id(at, session, text), "at": at, "session": session,
                      "kind": kind, "text": text[:400], "expecting": expecting,
                      "state": state, "nudges": nudges,
                      "nudge_count": (nudges or {}).get("nudge_count") if isinstance(nudges, dict) else None,
                      "last_nudge_at": (nudges or {}).get("last_nudge_at") if isinstance(nudges, dict) else None,
                      "verdict": verdict})
    return verdict


def delivery_scope():
    """The sessions whose events may reach the operator: the overwatch-armed set.

    Registered is not the same as watched. The registry holds every session on the machine,
    while overwatch is armed per session, deliberately, by the operator. Events from sessions
    with no armed overwatch were reaching the chat anyway, which is how a fleet of seventeen
    produced notifications about sessions nobody had asked to watch.

    Returns the set of armed session names, or None when it cannot be read. The caller then
    falls back to escalations only, so a missing file silences the receipts without silencing
    a genuine blocker.
    """
    try:
        with open(os.path.expanduser(ARMED_FILE)) as f:
            return set(json.load(f).keys())
    except Exception as e:
        LOG.warning("armed scope unreadable (%r); escalations only until it loads", e)
        return None


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

# Telegram is the pager: only events that reach _pending (lane 1, lane 3 judged worth knowing,
# the gate's own outage, hierarchy escalations) are sent there. Lane 2 never enters _pending.
# Sent directly through the Bot API, not through the webhook adapter, so a down gateway cannot
# swallow a page. telegram_via: "direct" (default) or "webhook" (the old agent-summarised path).
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _fleet_env.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_HOME_CHANNEL") or _fleet_env.get("TELEGRAM_HOME_CHANNEL")
TELEGRAM_VIA = str(_cfg("telegram_via", "direct"))
TG_MAX_CHARS = 3500          # Telegram silently drops over 4096
TG_MAX_RETRIES = 6
TG_BACKOFF = (15, 30, 60, 120, 300, 600)


def send_telegram(text, token=None, chat=None, timeout=5):
    """Send plain text through the Bot API. True only on {"ok": true}."""
    token, chat = token or TG_TOKEN, chat or TG_CHAT
    if not (token and chat):
        return False
    body = json.dumps({"chat_id": chat, "text": text, "disable_notification": False}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body,
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.load(r).get("ok"))
    except Exception as e:
        LOG.error("TELEGRAM SEND FAILED -> %s", type(e).__name__)    # never log the URL: it holds the token
        return False


def format_page(batch, limit=TG_MAX_CHARS):
    """One message: urgent first, `session: event` lines, truncated with '+N more'."""
    evs = sorted(batch, key=lambda e: not e.get("urgent"))
    out, used = [], 0
    for i, e in enumerate(evs):
        line = "%s: %s" % (e["session"], e["match"])
        if len(line) > 400:
            line = line[:397] + "..."
        if used + len(line) + 1 > limit - 20:
            out.append("+%d more" % (len(evs) - i))
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def deliver_batch(batch, now=None):
    """Deliver a batch of pending events. Fleet channel always (first attempt only), Telegram
    direct. Returns the events to re-queue (failed, with retry bookkeeping). Failures are also
    appended to the events file so nothing is silent."""
    now = now or time.time()
    for e in batch:
        if not e.get("retries"):
            post_fleet_channel(e["session"], e["match"])
    if TELEGRAM_VIA == "webhook":
        if WH_URL and post_batch(batch):
            return []
        sent = False
    else:
        sent = send_telegram(format_page(batch))
    if sent:
        LOG.info("TELEGRAM PAGE (%d events)", len(batch))
        return []
    LOG.warning("telegram down -> events file + retry (%d events)", len(batch))
    retry = []
    with open(EVENTS, "a", encoding="utf-8") as f:
        for e in batch:
            f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), e["session"], e["match"]))
            n = int(e.get("retries", 0)) + 1
            if n <= TG_MAX_RETRIES:
                retry.append({**e, "retries": n, "not_before": now + TG_BACKOFF[min(n, len(TG_BACKOFF)) - 1]})
            else:
                LOG.error("DROPPED after %d retries: %s | %s", TG_MAX_RETRIES, e["session"], e["match"])
    return retry


def _in_quiet_hours(start=23, end=5):
    """True between start:00 and end:00 local time. Only urgent events may interrupt then."""
    h = time.localtime().tm_hour
    return h >= start or h < end

REGISTRY_FILE = _cfg("registry_file", "~/.hermes/scripts/cc-watch/fleet_registry.json")
PENDING_FILE = _cfg("pending_file", "~/.hermes/scripts/cc-watch/fleet_watch_pending.json")
LOCK_FILE = _cfg("lock_file", "~/.hermes/scripts/cc-watch/fleet_watch.lock")
WATCH_FILE = _cfg("watch_file", "~/.hermes/scripts/cc-watch/fleet_watch_requests.json")
ACK_FILE = _cfg("ack_file", "~/.hermes/scripts/cc-watch/fleet_watch_acks.json")
USERMSG_FILE = _cfg("user_msgs_file", "~/.hermes/scripts/cc-watch/fleet_user_msgs.json")
STALL_WINDOW = int(_cfg("stall_window", "900"))   # seconds of no transcript growth before a STALL check-in fires (alive-but-silent). DEFAULT 300s = 5m: long enough that a normal quiet period (e.g. a 5m polling/sleep loop's idle gap) is NOT a false stall, short enough to still catch a genuinely hung agent. Overridable via FLEET_CONFIG JSON key "stall_window". The operator runs 5m loops sometimes, so our fleet config sets 360s (6m) to clear that. Root-caused 2026-09-11: 120s flagged 7 quiet-but-working sessions as stalled for ~12.4h straight.
# A stop is re-reported every STALL_REPEAT seconds until the transcript grows. Growth is the
# acknowledgement: the operator typing into the session, or the session resuming, both grow it.
# The gate decides which repeats reach her (first at its SILENCE_ALERT_AFTER, then reminders with
# backoff); without the repeats it only ever saw the first stall, which lands under that window,
# so a session that stayed stopped was never reported at all (210 swallowed, 2026-10-03).
STALL_REPEAT = int(_cfg("stall_repeat", "300"))
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


def process_watches(matched, modeltext=None):
    """Sweep active sentinel watches against this tick's matched tokens.
    - satisfied: the watched token appeared on that session's model lines -> drop watch
    - expired:   deadline passed with no token -> emit SENTINEL-MISSED (fires the agent)
    These events only fire on MODEL-emitted lines (matched already role-filtered).
    A watch token is matched LITERALLY against this tick's model-line text as well as
    against the pattern-matched tokens: contracts hand sessions tokens in whatever form
    the dispatcher chose (FW6F-POLL-LANDED), which the slug/generic patterns cannot see —
    a watch armed on such a token could only ever false-fire SENTINEL-MISSED.

    A watch may also carry `alt`: the NEEDS-INPUT-<slug> token a contract names for a session
    that legitimately ENDS BLOCKED. Satisfying on it is the point — the payload offered that
    route, so a session that took it has answered the contract, and firing SENTINEL-MISSED on
    it only buys the operator a check-in for a question that was already asked and answered.
    Returns (session, match) events; persists the remaining watches."""
    _now = int(time.time())
    watches = _load_watches()
    events = []
    kept = []
    for w in watches:
        sn = w.get("session", ""); tok = w.get("token", "")
        want = [t for t in (tok, w.get("alt") or "") if t]
        hit = None
        for t in want:
            if any(t in toks for s2, toks in matched.items() if s2 == sn):
                hit = t
                break
        if hit is None and modeltext:
            # Same strictness as scan(): a mention is not an emission. Loose substring here
            # re-opened the exact hole scan() now closes.
            for t in want:
                if any(_is_standalone(x, t) for x in modeltext.get(sn, [])):
                    hit = t
                    break
        if w.get("_satisfied") or hit:
            if hit:
                LOG.info("WATCH-SATISFIED %s %s", sn, hit)
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


def _text_of(v):
    """Coerce a transcript field to a plain string. Handles str, and a LIST of
    text blocks ({'text': ...}) — a shape Claude's jsonl actually ships (confirmed
    a transcript line: a queued_command whose 'prompt' is a list). Before this,
    _user_text could return a list and scan()'s .strip() raised AttributeError,
    aborting the whole session's scan and silently dropping events (2026-09-11)."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for b in v:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text") or b.get("content") or b.get("source") or ""
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _user_text(obj):
    """Extract a HUMAN user message from ANY shape it arrives in (2026-09-10): a role:user text turn,
    a queue-operation's content, or a queued_command attachment with origin.kind==human (the operator sends
    feedback via the remote surface -> those typed turns). Returns '' when the line is not a user msg.
    ALWAYS returns a str (coerced) so the caller's .strip() can never crash (2026-09-11 fix)."""
    otype = obj.get("type")
    if otype == "queue-operation":
        return _text_of(obj.get("content"))
    if otype == "attachment":
        at = obj.get("attachment") or {}
        if at.get("type") == "queued_command" and (at.get("origin") or {}).get("kind") == "human":
            return _text_of(at.get("prompt"))
    m = obj.get("message") or {}
    if m.get("role") == "user":
        return extract_text(obj)
    return ""


def _manager_owns(sn):
    """True when a live manager owns session `sn` and so issues its stall check-in. Any
    failure answers False: the daemon keeps the fallback rather than going silent."""
    try:
        _sd = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scripts")
        if _sd not in sys.path:
            sys.path.insert(0, _sd)
        import fleet_manager as fm
        reg = fm.Registry(os.path.expanduser(_cfg("hierarchy_file", fm.DEFAULT_PATH)))
        return fm.daemon_defers(
            sn, reg, lambda m: _probe_liveness(m, load_registry().get(m))[0])
    except Exception:
        return False


def _hier():
    """The hierarchy module, or None. Any import failure leaves the flat path as it was."""
    try:
        _sd = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scripts")
        if _sd not in sys.path:
            sys.path.insert(0, _sd)
        import fleet_hier
        return fleet_hier
    except Exception:
        return None


def hier_route(sn, match):
    """Route one event through the hierarchy levels. None for a flat session (the common
    case) or on any failure; a dict with action/trail/kind for a hierarchy member."""
    fh = _hier()
    if fh is None:
        return None
    try:
        return fh.route_event(sn, match, queue=False)
    except Exception:
        return None


def hier_escalations():
    """Escalations raised by managers since the last read: [(session, text)]."""
    fh = _hier()
    if fh is None:
        return []
    try:
        return fh.read_new_events("daemon")
    except Exception:
        return []


def _probe_liveness(sn, entry):
    """A managed Claude session is ALIVE iff its tmux session exists. Each managed session is
    launched as `tmux new-session -d -s <name> \"... claude ...\"`, so the tmux session IS the
    pane; when claude exits the window closes and the session dies. tmux has-session is therefore
    the reliable liveness truth. (NOT proc matching by config_dir: CLAUDE_CONFIG_DIR is an env
    var, not in argv, so pgrep-by-path gives false-negatives. Returns (bool, detail).)
    Class-fix: the daemon used to be transcript-only, so a session that died went silent."""
    try:
        ok = subprocess.run(["tmux", "has-session", "-t", "=" + sn + ":"],
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
        out = subprocess.run(["tmux", "capture-pane", "-t", "=" + sn + ":", "-p"],
                             capture_output=True, text=True, timeout=10).stdout or ""
    except Exception:
        return False, None
    if not _USAGE_LIMIT_RE.search(out):
        return False, None
    r = _RESET_RE.search(out)
    return True, (r.group(1).strip() if r else None)


# VERSION AWARENESS (optional). The daemon reports a session running behind the newest
# installed CLI, because the install channel can resolve to an older release than the fleet
# is on and a running process keeps the binary it started with. Degrades to a no-op if the
# module is missing: the daemon must never die for want of a reporting nicety.
try:
    _sysmod = __import__("sys")
    _sysmod.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fleet_version as _fv
except Exception:
    _fv = None


def stall_step(prev, now, grew, alive):
    """One scan's stall decision for one session: (new state, seconds quiet to report or None).

    A stop is reported when the transcript has not grown for STALL_WINDOW, then again every
    STALL_REPEAT until it grows. Growth resets everything: it is the acknowledgement, whether the
    operator typed into the session or the session resumed. The reported number is how long the
    session has ACTUALLY been quiet, and the gate's silence bound reads it, so the gate keeps no
    clock of its own that could outlive the stop.
    """
    if grew:
        return {"since": now, "flagged": False}, None             # grew / first sight -> reset
    if prev.get("flagged") and "last" not in prev:
        # Flagged by a daemon that fired once per stop. Start its repeat clock now rather than
        # re-reporting every long-stopped session in one burst on upgrade.
        return {**prev, "last": now}, None
    quiet_for = now - prev["since"]
    if alive and quiet_for >= STALL_WINDOW and (
            not prev.get("flagged") or now - prev.get("last", 0) >= STALL_REPEAT):
        return {"since": prev["since"], "flagged": True, "last": now}, quiet_for
    return prev, None


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
    modeltext = {}    # per-session MODEL-line text this tick: literal watch-token matching
    userturns = {}
    _now_i = int(time.time())   # used by sleep-gap + STALL logic
    # SLEEP / LID-CLOSE HANDLING (2026-09-09): if the gap between daemon scans is huge
    # (> SUSPEND_GAP), the MACHINE slept/suspended — the agents had no chance to emit, so a
    # long-silent transcript is NOT a stall. Rebaseline every session's STALL clock and
    # transcript size on the woke scan (no false STALL, no re-processing pre-sleep bytes).
    _last_scan = state.get("_last_scan", _now_i)
    state["_last_scan"] = _now_i
    if _now_i - _last_scan > SUSPEND_GAP:
        # SLEEP / LID-CLOSE (2026-09-09): a long scan gap = the machine slept; the agents had no
        # chance to emit, so long-silence right after wake is NOT a fresh stall. Rebaseline the
        # STALL *timer* and transcript sizes so we don't count pre-sleep silence.
        # 2026-09-11 FIX: PRESERVE the existing `flagged` value across suspend. Previously this
        # reset every session to `flagged=False`, which re-ARMED already-stalled sessions — so
        # every lid-close/wake re-fired a STALL notification ~2min later for sessions that had
        # never unstalled (confirmed: 4 sessions sat `flagged=False` for 17.7h of silence =
        # re-notifying constantly). Rule: once registered as stalled, keep it stored
        # stalled until the transcript GROWS (true resumption, which clears the flag) OR the
        # state file is deleted. Never re-arm a stall just because the machine slept.
        for k in list(state.setdefault("_stall", {})):
            prev_flagged = state["_stall"][k].get("flagged", False)
            state["_stall"][k] = {"since": _now_i, "flagged": prev_flagged, "last": _now_i}
        for _sn in SESSIONS():
            _jl = find_jsonl(_sn)
            if _jl:
                try:
                    state[_sn] = os.path.getsize(_jl)
                except Exception:
                    pass
        LOG.info("machine sleep/lid-close detected (gap %ss) — rebaselined STALL timers + sizes; PRESERVED stalled flags (no re-alert)", _now_i - _last_scan)
    _latest_ver, _latest_src = (None, "unavailable")
    try:
        if _fv:
            _latest_ver, _latest_src = _fv.latest_version()
    except Exception:
        pass
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
        # CLI-VERSION: report a session running behind the newest installed version, once per
        # version it is seen on. Throttled hard because reading it costs a process lookup, and
        # the point is drift awareness, not immediacy. Silent when it cannot tell, never a guess.
        if _fv:
            vs = state.setdefault("_version", {})
            ven = vs.setdefault(sn, {"version": None, "checked_at": 0, "reported": None})
            if now_t - ven["checked_at"] >= 300:
                ven["checked_at"] = now_t
                try:
                    ver, _vsrc = _fv.running_version(sn)
                except Exception:
                    ver = None
                ven["version"] = ver
                if ver and _latest_ver and ver != _latest_ver and ven.get("reported") != ver:
                    events.append((sn, f"VERSION-BEHIND-{sn.upper()}: running {ver}, "
                                       f"newest installed is {_latest_ver}"))
                    ven["reported"] = ver
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
            _st[sn], _quiet_for = stall_step(_prev, _now_i, grew=(cur > size or size == 0),
                                             alive=alive)
            if _quiet_for is not None and _manager_owns(sn):
                LOG.info("STALL for %s left to its manager (quiet %ss)", sn, _quiet_for)
            elif _quiet_for is not None:
                events.append((sn, f"STALL-{sn.upper()}: no new transcript output for {_quiet_for}s (alive, not producing) — check in on this session"))
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
                # Human user messages surface as queue-operation / queued_command-attachment typed
                # turns (remote-view feedback), NOT role:user text — capture them too (2026-09-10).
                user_txt = _user_text(obj)
                if user_txt.strip():
                    userturns.setdefault(sn, []).append(user_txt)
                if not txt:
                    continue
                if role == "user":
                    userturns.setdefault(sn, []).append(txt)   # delivery-ACK: user turns = what was actually landable
                if role != "assistant":
                    continue
                modeltext.setdefault(sn, []).append(txt)
                for p in pats:
                    if is_sentinel_pattern(p.pattern):
                        # tokens: EMISSION only (a line of its own), never a mention
                        tok = standalone_token(txt, p)
                    else:
                        _m = p.search(txt)
                        tok = _m.group(0) if _m else None
                    if not tok:
                        continue
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
                    events.append((sn, tok))
                    matched.setdefault(sn, set()).add(tok)
                    LOG.info("MATCH %s | %s", sn, tok)
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
    return events, matched, userturns, modeltext


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
    buffer of what each session was actually told (real user-role text turns — remote feedback,
    dispatcher pastes, scheduled sweeps), so 'what did this session get told?' has ONE authoritative
    answer instead of hand-parsing a giant jsonl. Pane-swallowed dispatches never land here, which is
    itself the delivery truth."""
    if not userturns:
        return
    now = time.time()
    ring = _load_ring()
    for sn, texts in userturns.items():
        last = ring[-1]["text"] if ring and ring[-1].get("session") == sn else ""
        for t in texts:
            if t == last:
                continue   # dedupe: same msg appears as queue-operations (enqueue/remove) + attachment
            ring.append({"at": now, "session": sn, "text": t[:3000]})
            last = t
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


def _backfill_ring():
    """On daemon START, preload each registered session's ring with its most recent user messages so a
    FRESH daemon still knows the last RING_MINUTES of what was sent (requirement: even on startup it should read
    them... always be able to know the last 5 sent'). Reuses _user_text across all shapes."""
    import datetime as _dt
    reg = []
    try:
        reg = json.load(open(os.path.expanduser(REGISTRY_FILE))).get("sessions", [])
    except Exception:
        return
    now = time.time(); cutoff = now - RING_MINUTES * 60
    ring = _load_ring()
    for s in reg:
        cd = _harness.resolve(s).get("config_dir")
        if not cd:
            continue
        base = os.path.expanduser(cd) + "/projects"
        u = s.get("uuid", ""); name = s.get("name")
        if not u or not name:
            continue
        for jl in glob.glob(os.path.join(base, "*", f"{u}*.jsonl")):
            try:
                for line in open(jl, errors="replace"):
                    try: o = json.loads(line)
                    except Exception: continue
                    ts = o.get("timestamp", "")
                    if not ts:
                        continue
                    try:
                        t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if t < cutoff:
                        continue
                    uu = _user_text(o)
                    if uu.strip():
                        ring.append({"at": int(t), "session": name, "text": uu.strip()[:3000]})
            except Exception:
                continue
    ring = [e for e in sorted(ring, key=lambda e: e["at"]) if now - e["at"] <= RING_MINUTES * 60][-RING_CAP:]
    dedupe = []
    for e in ring:
        if dedupe and dedupe[-1]["session"] == e["session"] and dedupe[-1]["text"] == e["text"]:
            continue
        dedupe.append(e)
    if dedupe:
        _save_ring(dedupe)
    LOG.info("ring backfill: %d user msgs across %d sessions", len(dedupe), len({e["session"] for e in dedupe}))


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


_SENTINEL_PAT = re.compile(r"(?:DONE|NEEDS-INPUT|ERROR|PROGRESS)-")


def is_sentinel_pattern(pattern_text):
    """True for token patterns (DONE-/NEEDS-INPUT-/ERROR-/PROGRESS-). Non-token patterns
    (the bare `traceback` probe) keep substring semantics — a real traceback is a block
    of text, never a line of its own."""
    return bool(_SENTINEL_PAT.search(pattern_text or ""))


def _standalone_lines(txt):
    """Every non-empty line of model text, markdown/quote decoration stripped."""
    for ln in (txt or "").splitlines():
        s = ln.strip().strip("`*_ \t").strip().strip("`*_ \t")
        if s:
            yield s


def _is_standalone(txt, token):
    """True when `token` is a LINE of its own in `txt`."""
    return any(s == token for s in _standalone_lines(txt))


def standalone_token(txt, pat):
    """The sentinel token `txt` EMITS, or None — emission only, never a mention.

    A contract hands a session a token and says emit EXACTLY that line. Substring
    matching cannot tell an emission from a QUOTE. Live case (2026-09-22): the assistant
    line "I am **not** emitting `DONE-ow-230922v`" matched `\\bDONE-...\\b`, fired a MATCH,
    and satisfied the armed watch — retiring it for a contract the session had explicitly
    DECLINED (real status: blocked on a classifier denial, work unfinished). The
    overwatch then had no outstanding sentinel on that session.

    Strictness is the safe direction, so it is the only direction. A MISSED emission
    fires SENTINEL-MISSED — a check-in. A FALSE emission retires the watch on unfinished
    work and reports a completion that never happened. Exact line equality, nothing else.
    """
    for s in _standalone_lines(txt):
        if pat.fullmatch(s):
            return pat.search(s).group(0)
    return None


def token_emitted(sname, token):
    """True when the session's transcript ALREADY carries this token on a MODEL line.

    Guards the arm-after-emit race: the daemon satisfies a watch only against tokens seen in
    the CURRENT tick, so a token that landed before the watch was created can never match and
    the deadline fires a SENTINEL-MISSED for work that is already done — a false alarm that
    costs the operator a check-in. Role-filtered (rg for the hit, then parse) because the
    dispatcher's own 'reply with EXACTLY this token' instruction is a user line and must not
    count as an emission. Fails open (arm as usual) if the transcript or rg is unavailable.
    """
    p = find_jsonl(sname)
    if not p:
        LOG.warning("token pre-check: no transcript for %s", sname)
        return False
    try:
        r = subprocess.run(["rg", "-F", "--no-line-number", "-e", token, p],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        LOG.warning("token pre-check failed (%s): %r", sname, e)
        return False
    if r.returncode != 0:                      # 1 = no match, 2 = error -> fail open
        return False
    for line in r.stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        # Same role rule scan() uses: message.role wins, else the line type. Kept identical
        # on purpose — a guard that disagrees with the daemon on what counts as an emission
        # would either block real arms or wave through the false-fire it exists to stop.
        role = (obj.get("message") or {}).get("role") or obj.get("type")
        if role == "assistant" and _is_standalone(extract_text(obj), token):
            return True
    return False


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
    pa.add_argument("--alt-token", default="", dest="alt_token",
                    help="a second token that also SATISFIES this watch. A contract that can "
                         "legitimately END BLOCKED names a NEEDS-INPUT-<slug> alternative, and a "
                         "watch armed on the done token alone expires on a session that took the "
                         "route the payload offered -- a false SENTINEL-MISSED.")
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
        _named = " / ".join(t for t in (a.token, a.alt_token) if t)
        if token_emitted(a.session, a.token) or (a.alt_token and token_emitted(a.session, a.alt_token)):
            print(f"NOT ARMED: {_named} is ALREADY on a model line in {a.session} — "
                  f"a watch here can only false-fire SENTINEL-MISSED at the deadline")
            return
        ws = _load_watches(); wid = secrets.token_hex(3)
        # One watch per (session, token): re-arming the same contract REPLACES the existing
        # entry instead of appending a duplicate. Two entries for one token carry two
        # deadlines and the daemon fires SENTINEL-MISSED on whichever passes first, so a
        # re-arm produced a spurious urgent event minutes before the live deadline.
        _dropped = [w for w in ws if w.get("session") == a.session and w.get("token") == a.token]
        ws = [w for w in ws if w not in _dropped]
        ws.append({"id": wid, "session": a.session, "token": a.token,
                   "alt": a.alt_token,
                   "deadline": int(time.time()) + int(a.deadline_min * 60),
                   "created": int(time.time()), "note": a.note})
        _save_watches(ws)
        if _dropped:
            print(f"replaced {len(_dropped)} existing watch(es) for {a.session} {a.token}")
        _altnote = f" alt={a.alt_token}" if a.alt_token else ""
        print(f"WATCH ARMED id={wid} session={a.session} token={a.token}{_altnote} "
              f"deadline={a.deadline_min}min")
    elif a.cmd == "list":
        ws = _load_watches()
        print("\n".join(f"{w['id']} {w.get('session')} {w.get('token')} dl={w.get('deadline')} note={w.get('note','')}" for w in ws) if ws else "no active watches")
        # Acks live in a separate control file; without this they armed invisibly and the only
        # way to find an id to cancel was to read fleet_watch_acks.json by hand.
        aks = _load_acks()
        if aks:
            print("\n".join(f"{k['id']} {k.get('session')} ACK:{k.get('marker')} dl={k.get('deadline')} note={k.get('note','')}" for k in aks))
    elif a.cmd == "cancel":
        ws = [w for w in _load_watches() if w.get("id") != a.id]; _save_watches(ws)
        print(f"cancelled {a.id}")
    elif a.cmd == "ack":
        acks = _load_acks(); aid = secrets.token_hex(3)
        # Same rule as watch add: one ack per (session, marker), re-arm replaces.
        _dropped = [k for k in acks if k.get("session") == a.session and k.get("marker") == a.marker]
        acks = [k for k in acks if k not in _dropped]
        acks.append({"id": aid, "session": a.session, "marker": a.marker,
                     "deadline": int(time.time()) + int(a.deadline_min * 60),
                     "created": int(time.time()), "note": a.note})
        _save_acks(acks)
        if _dropped:
            print(f"replaced {len(_dropped)} existing ack(s) for {a.session} {a.marker}")
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
        _backfill_ring()          # startup: preload last hour of user msgs so a fresh daemon knows them
        ensure_gate()             # the escalation gate is this daemon's dependency, so it starts it
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
            now = time.time()
            ready = [e for e in _pending if e.get("not_before", 0) <= now]
            if not ready:
                return
            batch = ready[:]
            _pending[:] = [e for e in _pending if e not in batch]
            # No persist here: the file still holds the batch until delivery resolves, so a crash
            # mid-send redelivers rather than loses the page.
            _pending.extend(deliver_batch(batch, now))
            _persist()                            # failed events survive a restart, with their retry state
        while True:
            try:
                try:
                    import usage_meter
                    usage_meter.tick()        # rate limited inside; never raises
                    _ask_usage_setup()
                except Exception:
                    pass
                added = False
                scan_events, matched, userturns, modeltext = scan(scan_pats)
                ingest_user_msgs(userturns)                       # rolling 1h user-message buffer
                # Sentinel watches: satisfy on a seen token; fire SENTINEL-MISSED on expiry.
                # Delivery-ACKs: confirm a dispatch landed as a USER turn; fire ACK-MISSED if not.
                scan_events = scan_events + process_watches(matched, modeltext) + process_acks(userturns)
                scope = delivery_scope()          # re-read every tick: arming changes without a restart
                expecting_now = sessions_expecting()   # who owes a token, i.e. who is mid-task
                for sn, match in scan_events:
                    if is_bookkeeping(match):     # lane 2, local fast path, no model call
                        LOG.debug("bookkeeping event not delivered: %s | %s", sn, match)
                        post_fleet_channel(sn, match)    # fleet channel only, never Telegram
                        continue
                    hr = hier_route(sn, match)    # None for every flat session
                    if hr is not None and hr["action"] == "deliver" and \
                            (hr["kind"] in ("classifier_block", "choice_ui") or DELIVER_ALL
                             or (scope is not None and sn in scope)):
                        # Reached the top level: a gate event rises through every level and is
                        # shown, never absorbed or judged by a model.
                        LOG.info("HIER deliver %s via %s | %s", sn, " > ".join(hr["trail"]), match[:90])
                        _pending.append({"session": sn,
                                         "match": "%s [via %s]" % (match, " > ".join(hr["trail"])),
                                         "at": int(time.time()), "urgent": True})
                        added = True
                        continue
                    urgent_hint = (match.startswith("NEEDS-INPUT-") or "traceback" in match.lower()
                                   or match.startswith("SENTINEL-MISSED-") or match.startswith("ACK-MISSED-")
                                   or match.startswith("SESSION-DEAD-") or match.startswith("USAGE-LIMIT-"))
                    if not DELIVER_ALL:
                        if scope is None and not urgent_hint:
                            LOG.debug("no armed scope, non-urgent event held: %s | %s", sn, match)
                            continue
                        if scope is not None and sn not in scope:
                            LOG.debug("session not armed for overwatch, event not delivered: %s | %s", sn, match)
                            continue
                    # Lane 1 (structural) and lane 3 (classified) live in the gate. A gate
                    # that cannot be reached shows the event rather than swallowing it.
                    display = match
                    if match.startswith(("NEEDS-INPUT-", "ERROR-")):
                        ask = _ask_snippet((modeltext or {}).get(sn))
                        if ask:
                            display = f"{match} -- {ask}"
                    nudges=None
                    if match.startswith("STALL"):
                        try:
                            stopped_v = int(re.search(r"(\d+)s", match).group(1))
                        except Exception:
                            stopped_v = None
                        if stopped_v is not None:
                            nudges = _maybe_nudge(sn, stopped_v)
                    verdict = gate_verdict(sn, "", display,
                                           context=event_context(sn, modeltext),
                                           expecting=sn in expecting_now,
                                           state=_session_state(sn),
                                           nudges=nudges)
                    if verdict is None or verdict.get("lane") == 1:
                        urgent = True
                    elif verdict.get("escalate"):
                        urgent = False            # classified as worth knowing: batched, not immediate
                    else:
                        LOG.info("gate held %s | rule=%s conf=%s | %s", sn,
                                 verdict.get("rule"), verdict.get("confidence"), match[:90])
                        if verdict.get("lane") == 2:
                            post_fleet_channel(sn, match)    # lane 2: fleet channel only
                        continue
                    _pending.append({"session": sn, "match": display,
                                     "at": int(time.time()), "urgent": urgent})
                    added = True
                # The gate itself, polled rather than only probed at startup. Its own outage goes
                # straight to the operator: it is not an event about a session, so neither the
                # overwatch scope nor the gate (which is the thing that is down) may filter it.
                for hs, ht in hier_escalations():
                    _pending.append({"session": hs, "match": ht, "at": int(time.time()), "urgent": True})
                    added = True
                for ev in gate_watch():
                    _pending.append({**ev, "at": int(time.time())})
                    added = True
                if added:
                    _persist()                    # durable even if we crash inside the window
                now = time.time()
                if any(e["urgent"] and e.get("not_before", 0) <= now for e in _pending):
                    _flush()                      # urgent -> flush now (still one summarized digest)
                elif _in_quiet_hours():
                    pass                          # quiet hours: hold routine events, one digest after they end
                elif _pending and (now - _pending[0]["at"] >= DEBOUNCE or len(_pending) >= MAX_BATCH):
                    _flush()                      # routine -> debounce into ONE batched digest
            except Exception as e:
                LOG.exception("daemon scan/send loop error")
            time.sleep(a.interval)
        return

    # monitor_script mode: print matches so a changed output wakes the cron agent.
    events, _, _, _ = scan(a.patterns)
    if events:
        print("\n".join(f"{sn}: {match}" for sn, match in events))


if __name__ == "__main__":
    main()
