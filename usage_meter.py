#!/usr/bin/env python3
"""Subscription usage meter: count tokens from the session transcripts, learn the limit, pace the fleet.

Three parts, one file:

  1. METER      reads Claude Code transcripts incrementally (byte offsets per file), dedupes
                streamed messages by message id + request id, and keeps 5 minute buckets of the four
                token components per profile. Limit messages ("You've hit your session limit") are
                real calibration events.
  2. ESTIMATOR  every limit event says: usage at that moment was 100 percent. Usage is a weighted sum
                of the four components. Recursive least squares learns the weights, seeded with the
                price ratios. A window that ended with no limit hit is only a lower bound, so it can
                lower the learned scale but never raise it.
  3. CONTROLLER a PI loop on projected end-of-window usage against a target. Its output is a posture
                (push / steady / hold / observe) that the daemon turns into a nudge interval factor.
                It stays in observe until the estimate is trustworthy and backs off hard after a hit.

Pure functions where possible; the daemon calls tick() and nudge_factor(). CLI:

    python3 usage_meter.py backfill      # replay all history, learn the weights (minutes)
    python3 usage_meter.py status        # per profile: window, usage, ceiling, posture
"""
import glob
import json
import logging
import math
import os
import re
import sys
import time

COMPONENTS = ("input", "output", "cache_write", "cache_read")
BUCKET = 300                      # seconds per bucket
SESSION_WINDOW = 5 * 3600
WEEK_WINDOW = 7 * 86400
KEEP = 8 * 86400                  # buckets older than this are dropped from live state
# Price ratios (input : output : cache write : cache read) seed the weights. They are a prior, not
# a claim about how the limit weighs tokens; the estimator moves them.
PRIOR_WEIGHTS = (1.0, 5.0, 1.25, 0.1)
PRIOR_SPREAD = 0.5                # prior sd of each weight, as a share of its value
EVENT_NOISE = 0.01                # variance of a limit event's "usage was 1.0"
MIN_EVENTS = 3                    # below this the controller only observes
MAX_CV = 0.5                      # implied ceilings spread wider than this: only observe
INLIER_LO, INLIER_HI = 0.5, 2.0   # an event within this factor of the median counts as agreeing
MIN_INLIER_SHARE = 0.5            # fewer agreeing events than this share of all: the signal is noise
TARGET = 0.95                     # end-of-window usage the controller aims for
KP, KI = 1.5, 0.05
POSTURE_PUSH, POSTURE_HOLD = 0.25, -0.25

READING_REL_NOISE = 0.1           # sd of a reading the operator typed in, as a share of the reading
ADVICE_WEEKLY_ROLLING = 0.85      # no weekly reset known: advise once the trailing 7 days pass this share

_LIMIT_RE = re.compile(r"You've hit your (session|weekly) limit", re.I)


def state_dir():
    return os.environ.get("USAGE_METER_DIR") or os.path.expanduser("~/.hermes/cache/usage-meter")


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- parsing

def _epoch(ts):
    """ISO8601 with a Z or offset to epoch seconds, or None."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_line(line):
    """One transcript line -> ('usage', ts, key, comps) | ('limit', ts, scope) | None."""
    if '"usage"' not in line and "hit your" not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if d.get("type") != "assistant":
        return None
    ts = _epoch(d.get("timestamp") or "")
    msg = d.get("message") or {}
    if ts is None:
        return None
    if d.get("isApiErrorMessage") and msg.get("model") == "<synthetic>":
        content = msg.get("content")
        text = content[0].get("text", "") if isinstance(content, list) and content else ""
        if "spend limit" in text:
            return None               # an org spend cap is money, not a usage window
        m = _LIMIT_RE.search(text)
        if m:
            return ("limit", ts, m.group(1).lower())
        return None
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None
    comps = (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
             int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0))
    if not any(comps):
        return None
    key = "%s:%s" % (msg.get("id") or d.get("uuid"), d.get("requestId") or "")
    return ("usage", ts, key, comps)


class Profile:
    """Buckets and dedupe state for one profile directory."""

    def __init__(self, name):
        self.name = name
        self.buckets = {}             # bucket start (int) -> [in, out, cw, cr]
        self.seen = {}                # dedupe key -> (bucket start, comps)
        self.offsets = {}             # transcript path -> byte offset
        self.limits = []              # [(ts, scope)] not yet turned into calibration events

    def add(self, ts, key, comps):
        b = int(ts // BUCKET * BUCKET)
        prev = self.seen.get(key)
        if prev is not None:          # a streamed message rewritten: keep the larger counts
            pb, pc = prev
            merged = tuple(max(a, c) for a, c in zip(pc, comps))
            if merged == pc:
                return
            slot = self.buckets.setdefault(pb, [0, 0, 0, 0])
            for i in range(4):
                slot[i] += merged[i] - pc[i]
            self.seen[key] = (pb, merged)
            return
        slot = self.buckets.setdefault(b, [0, 0, 0, 0])
        for i in range(4):
            slot[i] += comps[i]
        self.seen[key] = (b, comps)

    def prune(self, now):
        cut = now - KEEP
        for b in [b for b in self.buckets if b < cut]:
            del self.buckets[b]
        for k in [k for k, (b, _) in self.seen.items() if b < cut]:
            del self.seen[k]

    def to_json(self):
        return {"buckets": {str(k): v for k, v in self.buckets.items()},
                "seen": {k: [b, list(c)] for k, (b, c) in self.seen.items()},
                "offsets": self.offsets, "limits": self.limits}

    @classmethod
    def from_json(cls, name, d):
        p = cls(name)
        p.buckets = {int(k): list(v) for k, v in (d.get("buckets") or {}).items()}
        p.seen = {k: (v[0], tuple(v[1])) for k, v in (d.get("seen") or {}).items()}
        p.offsets = dict(d.get("offsets") or {})
        p.limits = [tuple(x) for x in d.get("limits") or []]
        return p

    def scan_file(self, path):
        """Read what the file gained since the last call. Returns the number of records read."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0
        off = self.offsets.get(path, 0)
        if size < off:
            off = 0                   # truncated or replaced: start over, dedupe protects us
        if size == off:
            return 0
        n = 0
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return 0                  # a half-written last line: wait for the newline
        for raw in data[:end + 1].splitlines():
            r = parse_line(raw.decode("utf-8", "replace"))
            if r is None:
                continue
            n += 1
            if r[0] == "usage":
                self.add(r[1], r[2], r[3])
            else:
                self.limits.append((r[1], r[2]))
        self.offsets[path] = off + end + 1
        return n


# ----------------------------------------------------------------------------- windows

def blocks(buckets):
    """Session blocks by the same rule the usage tools use: a block starts at the hour of its first
    activity and lasts five hours; a gap of five hours or more also starts a new one.
    Returns [(start, end, [bucket starts])] in time order."""
    out = []
    cur = None
    last = None
    for b in sorted(buckets):
        if cur is None or b >= cur[1] or (last is not None and b - last >= SESSION_WINDOW):
            start = b // 3600 * 3600
            cur = [start, start + SESSION_WINDOW, []]
            out.append(cur)
        cur[2].append(b)
        last = b
    return [tuple(x) for x in out]


def comps_between(buckets, lo, hi):
    """Sum of components over buckets in [lo, hi]."""
    t = [0, 0, 0, 0]
    for b, v in buckets.items():
        if lo <= b <= hi:
            for i in range(4):
                t[i] += v[i]
    return t


def usage_at(buckets, ts, scope):
    """Components used in the window that holds ts, up to ts."""
    if scope == "weekly":
        return comps_between(buckets, ts - WEEK_WINDOW, ts)
    for start, end, members in blocks(buckets):
        if start <= ts < end:
            return comps_between(buckets, start, ts)
    return [0, 0, 0, 0]


# ----------------------------------------------------------------------------- readings

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_reset(text, now=None):
    """When a window resets, from the words on the usage page. Accepts 'in 2 hr 53 min', '2h53m',
    '11:00 PM', 'Fri 2:00 AM'. Returns epoch seconds of the next such moment, or raises ValueError."""
    import datetime
    now = time.time() if now is None else now
    t = (text or "").strip().lower()
    rel = re.fullmatch(r"(?:in\s+)?(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?\s*(?:(\d+)\s*(?:m|min|mins|minute|minutes))?", t)
    if t and rel and (rel.group(1) or rel.group(2)):
        return now + int(rel.group(1) or 0) * 3600 + int(rel.group(2) or 0) * 60
    m = re.fullmatch(r"(?:(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise ValueError("cannot read a reset time from %r" % text)
    day, hh, mm, ap = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    if ap:
        hh = hh % 12 + (12 if ap == "pm" else 0)
    if hh > 23 or mm > 59:
        raise ValueError("cannot read a reset time from %r" % text)
    base = datetime.datetime.fromtimestamp(now)
    cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if day:
        cand += datetime.timedelta(days=(_DAYS.index(day) - cand.weekday()) % 7)
    while cand.timestamp() <= now:
        cand += datetime.timedelta(days=7 if day else 1)
    return cand.timestamp()


# ----------------------------------------------------------------------------- estimator

class Estimator:
    """theta . (components in millions) = fraction of the limit used. Limit events pin it to 1."""

    def __init__(self, scale=None):
        s = scale if scale is not None else 0.05
        self.theta = [s * w for w in PRIOR_WEIGHTS]
        self.P = [(PRIOR_SPREAD * t) ** 2 for t in self.theta]       # diagonal covariance
        self.events = []              # components of each event used in the fit
        self.targets = []             # fraction of the limit each event said was used (1.0 for a limit hit)
        self.total_events = 0         # all events seen, including the ones rejected as noise

    @staticmethod
    def millions(comps):
        return [c / 1e6 for c in comps]

    def fraction(self, comps):
        return sum(t * x for t, x in zip(self.theta, self.millions(comps)))

    def seed_scale(self, events, targets=None):
        """Pick the scale that fits the events best under the prior ratios, so RLS starts close."""
        ratios = []
        for i, comps in enumerate(events):
            x = self.millions(comps)
            base = sum(w * v for w, v in zip(PRIOR_WEIGHTS, x))
            if base > 0:
                ratios.append((targets[i] if targets else 1.0) / base)
        if not ratios:
            return
        ratios.sort()
        s = ratios[len(ratios) // 2]
        self.theta = [s * w for w in PRIOR_WEIGHTS]
        self.P = [(PRIOR_SPREAD * t) ** 2 for t in self.theta]

    def update(self, comps, target=1.0):
        """One event: the fraction used was `target` (1.0 for a limit hit, the typed-in share for a
        reading). A reading is noisy in proportion to itself, so a small one pulls gently."""
        x = self.millions(comps)
        px = [p * v for p, v in zip(self.P, x)]
        noise = max(1e-6, (READING_REL_NOISE * target + (0.005 if target < 1.0 else 0.0)) ** 2)
        s = sum(v * q for v, q in zip(x, px)) + noise
        if s <= 0:
            return
        err = target - sum(t * v for t, v in zip(self.theta, x))
        k = [q / s for q in px]
        self.theta = [max(1e-9, t + ki * err) for t, ki in zip(self.theta, k)]
        self.P = [max(1e-12, p - ki * q) for p, ki, q in zip(self.P, k, px)]
        self.events.append(list(comps))
        self.targets.append(target)

    def lower_bound(self, comps):
        """A window that ended with no hit: usage at the end was below the limit. Scale down if the
        estimate says it was above, never up."""
        f = self.fraction(comps)
        if f > 1.0:
            self.theta = [t / f for t in self.theta]

    def spread(self):
        """Coefficient of variation of the fraction each event implies after fitting: how well the
        weights explain the events. Zero when they agree."""
        tg = self.targets if len(self.targets) == len(self.events) else [1.0] * len(self.events)
        fr = [self.fraction(c) / t for c, t in zip(self.events, tg) if t > 0]
        if len(fr) < 2:
            return None
        m = sum(fr) / len(fr)
        var = sum((f - m) ** 2 for f in fr) / len(fr)
        return math.sqrt(var) / m if m else None

    def trusted(self):
        sp = self.spread()
        share = len(self.events) / self.total_events if getattr(self, "total_events", 0) else 1.0
        return (len(self.events) >= MIN_EVENTS and sp is not None and sp <= MAX_CV
                and share >= MIN_INLIER_SHARE)

    def to_json(self):
        return {"theta": self.theta, "P": self.P, "events": self.events, "targets": self.targets,
                "total_events": self.total_events}

    @classmethod
    def from_json(cls, d):
        e = cls()
        if d:
            e.theta, e.P, e.events = d["theta"], d["P"], d["events"]
            e.targets = d.get("targets") or [1.0] * len(e.events)
            e.total_events = d.get("total_events", len(e.events))
        return e


def calibration_events(profile):
    """Turn recorded limit messages into one event per window. Repeated messages in one window
    (every retry prints another) collapse into the first."""
    done = set()
    out = []
    for ts, scope in sorted(profile.limits):
        if scope == "weekly":
            wid = ("w", int(ts // 86400))
            if any(k[0] == "w" and abs(k[1] - wid[1]) < 3 for k in done):
                continue
        else:
            win = next(((s, e) for s, e, _ in blocks(profile.buckets) if s <= ts < e), None)
            if win is None:
                continue
            wid = ("s", win[0])
            if wid in done:
                continue
        done.add(wid)
        comps = usage_at(profile.buckets, ts, scope)
        if sum(comps) > 0:
            out.append({"ts": ts, "scope": scope, "comps": comps})
    return out


def fit(events, scope):
    """Fit the weights on the events that agree with each other.

    Transcripts see only part of the account's usage (other devices, claude.ai, other profiles on
    the same login), and a window rebuilt from timestamps can start earlier than the real one. So a
    limit event is a noisy reading of the ceiling, not an exact one, and the noise runs both ways:
    the real replay of two profiles shows weighted usage at a hit spanning more than two orders of
    magnitude. The robust centre is the median; events within a factor of two of it are inliers, the
    rest are reported but never fitted."""
    pairs = [(e["comps"], float(e.get("frac") or 1.0)) for e in events if e["scope"] == scope]
    readings = [(e["comps"], float(e["frac"])) for e in events
                if e["scope"] == scope and e.get("source") == "reading"]
    est = Estimator()
    if readings:
        # A reading carries its own reset time, so its window is exact; a limit event's window is
        # rebuilt from activity and counts tokens the account may never have billed. Where the two
        # disagree by an order of magnitude the reading is the one that saw the real page.
        est.total_events = len(readings)
        est.seed_scale([c for c, _ in readings], [f for _, f in readings])
        for c, f in readings:
            est.update(c, f)
        return est
    est.total_events = len(pairs)
    if not pairs:
        return est

    def implied(c, f):
        """Weighted usage under the prior ratios that would have meant 100 percent."""
        return sum(w * v / 1e6 for w, v in zip(PRIOR_WEIGHTS, c)) / f

    pw = sorted(implied(c, f) for c, f in pairs)
    med = pw[len(pw) // 2]
    inliers = [(c, f) for c, f in pairs if med > 0 and INLIER_LO <= implied(c, f) / med <= INLIER_HI]
    est.seed_scale([c for c, _ in inliers], [f for _, f in inliers])
    for c, f in inliers:
        est.update(c, f)
    return est


# ----------------------------------------------------------------------------- controller

class Controller:
    """PI on projected end-of-window usage. error > 0 means the window is on course to finish under
    the target, so push; error < 0 means over, so hold."""

    def __init__(self):
        self.integral = 0.0
        self.window = None
        self.hit = False
        self.last = None

    def new_window(self, wid):
        if wid != self.window:
            self.integral *= 0.5      # carry half of a chronic under-use into the next window
            self.window = wid
            self.hit = False

    def step(self, wid, projected, trusted, hit, now):
        self.new_window(wid)
        dt = 0 if self.last is None else min(now - self.last, 600)
        self.last = now
        if hit:
            self.hit = True
            self.integral *= 0.25
        if not trusted:
            return "observe", 0.0
        if self.hit:
            return "hold", -1.0       # at the limit: nothing more fits in this window
        err = TARGET - projected
        self.integral = max(-2.0, min(2.0, self.integral + err * dt / 3600.0))
        u = max(-1.0, min(1.0, KP * err + KI * self.integral / max(1e-9, 1.0)))
        if u > POSTURE_PUSH:
            return "push", u
        if u < POSTURE_HOLD:
            return "hold", u
        return "steady", u

    def to_json(self):
        return {"integral": self.integral, "window": self.window, "hit": self.hit, "last": self.last}

    @classmethod
    def from_json(cls, d):
        c = cls()
        for k in ("integral", "window", "hit", "last"):
            if d and k in d:
                setattr(c, k, d[k])
        return c


def burn_per_min(buckets, est, now, span=1800):
    """Recent fraction-per-minute from the last half hour of buckets."""
    comps = comps_between(buckets, now - span, now)
    return est.fraction(comps) / (span / 60.0)


# ----------------------------------------------------------------------------- the meter

def profile_dirs():
    base = os.environ.get("USAGE_METER_PROFILES")
    if base:
        return sorted(base.split(":"))
    return sorted(glob.glob(os.path.expanduser("~/.claude-*")))


def transcripts(profile_dir):
    """Every transcript under the profile: main sessions, subagents and workflow subagents, at any
    depth. Agents spawned by agents all bill the same account, so none may be skipped."""
    return glob.glob(os.path.join(profile_dir, "projects", "**", "*.jsonl"), recursive=True)


# ----------------------------------------------------------------------------- Hermes agents

SETTING_KEY = "hermes_usage_profile"
SETTING_PLUGIN = "fleet-platform"
UNCOUNTED = "uncounted-boops"     # the explicit "do not count Hermes' own agents" choice


def hermes_setting():
    """(state, profile dir name). state is 'unset' (empty or never chosen: not counted, and the
    operator should be asked), 'uncounted' (deliberately not counted), or 'counted'. The value is
    the plugin setting `plugins.entries.fleet-platform.settings.hermes_usage_profile`, edited in
    the plugin's settings form. USAGE_METER_HERMES_PROFILE overrides it, for tests."""
    raw = os.environ.get("USAGE_METER_HERMES_PROFILE")
    if raw is None:
        try:
            import yaml
            home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
            with open(os.path.join(home, "config.yaml"), encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            raw = (((cfg.get("plugins") or {}).get("entries") or {}).get(SETTING_PLUGIN) or {}) \
                .get("settings", {}).get(SETTING_KEY)
        except Exception:
            raw = None
    raw = (raw or "").strip()
    if not raw:
        return "unset", None
    if raw == UNCOUNTED:
        return "uncounted", None
    return "counted", raw if raw.startswith(".claude-") else ".claude-" + raw


class HermesSource:
    """Hermes' own agents (kanban workers, cron jobs, chat) call the API directly, so their usage is
    in Hermes' session table, not in any Claude Code transcript. When the operator says which
    profile's account those credentials belong to, their tokens are added to that profile.

    The table holds running totals per session, so each read adds only the change since the last
    read, at the session's last activity. That is coarse inside one long session, exact in total."""

    def __init__(self, db_path=None):
        self.db = db_path or os.path.expanduser("~/.hermes/state.db")
        self.seen = {}                # session id -> totals already counted
        self.cursor = 0.0

    def read(self, profile, now):
        import sqlite3
        if not os.path.exists(self.db):
            return 0
        con = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT id, COALESCE(last_activity_at, started_at), input_tokens, output_tokens, "
                "cache_write_tokens, cache_read_tokens FROM sessions "
                "WHERE billing_provider = 'anthropic' AND model LIKE 'claude%' "
                "AND COALESCE(last_activity_at, started_at) >= ?", (self.cursor - 3600,)).fetchall()
        finally:
            con.close()
        n = 0
        for sid, ts, i, o, cw, cr in rows:
            cur = (int(i or 0), int(o or 0), int(cw or 0), int(cr or 0))
            prev = self.seen.get(sid, (0, 0, 0, 0))
            delta = tuple(max(0, c - p) for c, p in zip(cur, prev))
            self.seen[sid] = tuple(max(c, p) for c, p in zip(cur, prev))
            if any(delta) and ts:
                b = int(ts // BUCKET * BUCKET)
                slot = profile.buckets.setdefault(b, [0, 0, 0, 0])
                for k in range(4):
                    slot[k] += delta[k]
                n += 1
        self.cursor = now
        return n

    def to_json(self):
        return {"seen": {k: list(v) for k, v in self.seen.items()}, "cursor": self.cursor}

    @classmethod
    def from_json(cls, d, db_path=None):
        h = cls(db_path)
        h.seen = {k: tuple(v) for k, v in ((d or {}).get("seen") or {}).items()}
        h.cursor = (d or {}).get("cursor", 0.0)
        return h


class Meter:
    def __init__(self, directory=None, profiles=None, hermes_db=None):
        self.dir = directory or state_dir()
        self.profiles = {}
        self.est = {}                 # (profile, scope) -> Estimator
        self.ctl = {}                 # profile -> Controller
        self.paths = {os.path.basename(p): p for p in (profiles or profile_dirs())}
        self.hermes_db = hermes_db
        self.load()

    # persistence
    def load(self):
        st = _read_json(os.path.join(self.dir, "state.json"), {})
        for name in self.paths:
            self.profiles[name] = Profile.from_json(name, (st.get("profiles") or {}).get(name, {}))
        self.hermes = HermesSource.from_json(st.get("hermes"), self.hermes_db)
        cal = _read_json(os.path.join(self.dir, "calibration.json"), {"events": {}})
        self.calibration = {n: [e for e in evs if e.get("source") != "reading"]
                            for n, evs in cal["events"].items()}
        self.resets = {}                               # profile -> {"weekly": a known reset, epoch}
        self.n_readings = -1
        self.refresh_readings(refit=False)
        for name in self.paths:
            for scope in ("session", "weekly"):
                evs = [e for e in self.calibration.get(name, []) if e["scope"] == scope]
                self.est[(name, scope)] = fit(evs, scope)
            self.ctl[name] = Controller.from_json((st.get("controllers") or {}).get(name))

    def save(self):
        _write_json(os.path.join(self.dir, "state.json"), {
            "profiles": {n: p.to_json() for n, p in self.profiles.items()},
            "controllers": {n: c.to_json() for n, c in self.ctl.items()},
            "hermes": self.hermes.to_json()})
        # Readings live in their own append-only file: the daemon holds a Meter in memory for days
        # and saves it every minute, so a reading typed in from the CLI would be overwritten here.
        _write_json(os.path.join(self.dir, "calibration.json"),
                    {"events": {n: [e for e in evs if e.get("source") != "reading"]
                                for n, evs in self.calibration.items()}})

    def hermes_profile(self):
        """Profile directory name whose account Hermes' own agents bill to, or None when they are
        not counted (unset, empty or the explicit uncounted choice)."""
        return hermes_setting()[1]

    # scanning
    def scan(self, now=None):
        now = now or time.time()
        new_events = 0
        for name, pdir in self.paths.items():
            prof = self.profiles[name]
            for path in transcripts(pdir):
                prof.scan_file(path)
            if self.hermes_profile() == name:
                self.hermes.read(prof, now)
            if prof.limits:
                known = {(e["scope"], e["ts"]) for e in self.calibration.get(name, [])}
                for ev in calibration_events(prof):
                    if (ev["scope"], ev["ts"]) in known:
                        continue
                    self.calibration.setdefault(name, []).append(ev)
                    self.est[(name, ev["scope"])] = fit(
                        [e for e in self.calibration[name] if e["scope"] == ev["scope"]], ev["scope"])
                    new_events += 1
                prof.limits = []
            prof.prune(now)
        return new_events

    # readings
    def weekly_window(self, name, now):
        """(start, end) of the weekly period that holds `now`, from one known reset; None if unknown."""
        anchor = (self.resets.get(name) or {}).get("weekly")
        if anchor is None:
            return None
        k = math.floor((now - anchor) / WEEK_WINDOW)
        start = anchor + k * WEEK_WINDOW
        return start, start + WEEK_WINDOW

    def add_reading(self, name, pct, scope, resets_at, now=None):
        """Record the percentage the usage page shows, and the reset time it shows beside it.

        The reset gives the real window start (end minus five hours or seven days), which a window
        rebuilt from activity cannot. Returns (fraction before, fraction after) under the weights."""
        now = time.time() if now is None else now
        if name not in self.profiles:
            raise KeyError("unknown profile %r (have %s)" % (name, ", ".join(sorted(self.profiles))))
        if scope not in ("session", "weekly"):
            raise ValueError("scope is session or weekly")
        if not 0 < pct <= 100:
            raise ValueError("a reading is a percentage above 0 and up to 100")
        span = SESSION_WINDOW if scope == "session" else WEEK_WINDOW
        comps = comps_between(self.profiles[name].buckets, resets_at - span, now)
        if sum(comps) <= 0:
            raise ValueError("no tokens counted for %s in that window; nothing to calibrate against" % name)
        before = self.est[(name, scope)].fraction(comps)
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "readings.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"profile": name, "ts": now, "scope": scope, "comps": comps,
                                "frac": pct / 100.0, "resets_at": resets_at}) + "\n")
        self.refresh_readings()
        return before, self.est[(name, scope)].fraction(comps)

    def refresh_readings(self, refit=True):
        """Merge the readings file into the calibration events. Cheap; the daemon calls it every tick
        so a reading typed in elsewhere takes effect within a minute, with no restart."""
        rows = []
        try:
            with open(os.path.join(self.dir, "readings.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        if len(rows) == self.n_readings:
            return
        self.n_readings = len(rows)
        for name in list(self.calibration):
            self.calibration[name] = [e for e in self.calibration[name] if e.get("source") != "reading"]
        self.resets = {}
        for r in rows:
            name = r.get("profile")
            if name not in self.paths:
                continue
            self.calibration.setdefault(name, []).append(
                {"ts": r["ts"], "scope": r["scope"], "comps": r["comps"], "frac": r["frac"], "source": "reading"})
            if r["scope"] == "weekly":
                self.resets.setdefault(name, {})["weekly"] = r["resets_at"]
        if refit:
            for name in self.paths:
                for scope in ("session", "weekly"):
                    self.est[(name, scope)] = fit(
                        [e for e in self.calibration.get(name, []) if e["scope"] == scope], scope)

    # reporting
    def status(self, now=None):
        now = now or time.time()
        out = {}
        for name, prof in self.profiles.items():
            est = self.est[(name, "session")]
            cur = next(((s, e) for s, e, _ in blocks(prof.buckets) if s <= now < e), None)
            row = {"events": len(est.events), "total_events": est.total_events, "spread": est.spread(), "trusted": est.trusted(),
                   "weights": dict(zip(COMPONENTS, est.theta))}
            if cur:
                comps = comps_between(prof.buckets, cur[0], now)
                used = est.fraction(comps)
                remaining = (cur[1] - now) / 60.0
                burn = burn_per_min(prof.buckets, est, now)
                row.update(window_start=cur[0], window_end=cur[1], components=comps, used=used,
                           burn_per_min=burn, projected=used + burn * remaining)
            est_w = self.est[(name, "weekly")]
            row.update(weekly_events=len(est_w.events), weekly_trusted=est_w.trusted())
            win = self.weekly_window(name, now)
            if win:
                comps_w = comps_between(prof.buckets, win[0], now)
                used_w = est_w.fraction(comps_w)
                rate = est_w.fraction(comps_between(prof.buckets, now - 86400, now)) / 1440.0   # per minute, trailing day
                row.update(weekly_start=win[0], weekly_end=win[1], weekly_used=used_w,
                           weekly_projected=used_w + rate * (win[1] - now) / 60.0,
                           weekly_burn_per_min=rate)
            else:
                row.update(weekly_rolling=est_w.fraction(comps_between(prof.buckets, now - WEEK_WINDOW, now)))
            out[name] = row
        return out

    def advice(self, status):
        """What to tell the operator: a profile on course to run out before its weekly reset, or one
        about to, with no reset known to say. Each item says how far to trust the estimate, because an
        uncalibrated one must ask for a reading, not issue a verdict."""
        out = []
        for name, row in status.items():
            trusted = row.get("weekly_trusted", False)
            base = {"profile": name, "trusted": trusted, "events": row.get("weekly_events", 0)}
            if "weekly_projected" in row:
                if row["weekly_projected"] >= 1.0:
                    rate = row.get("weekly_burn_per_min") or 0.0
                    left = max(0.0, 1.0 - row["weekly_used"])
                    eta = None if rate <= 0 else min(row["weekly_end"], time.time() + left / rate * 60.0)
                    out.append({**base, "kind": "will_run_out", "used": row["weekly_used"],
                                "projected": row["weekly_projected"], "eta": eta, "reset": row["weekly_end"]})
            elif row.get("weekly_rolling", 0.0) >= ADVICE_WEEKLY_ROLLING:
                out.append({**base, "kind": "weekly_reset_unknown", "used": row["weekly_rolling"]})
        return out

    def tick(self, now=None):
        """One daemon pass: scan, step each controller, persist. Returns the posture per profile."""
        now = now or time.time()
        self.refresh_readings()
        self.scan(now)
        postures = {}
        st = self.status(now)
        for name, row in st.items():
            if "projected" not in row:
                postures[name] = {"posture": "steady", "u": 0.0, **row}
                continue
            hit = any(row["window_start"] <= e["ts"] < row["window_end"]
                      for e in self.calibration.get(name, []) if e["scope"] == "session")
            posture, u = self.ctl[name].step(row["window_start"], row["projected"],
                                             row["trusted"], hit, now)
            postures[name] = {"posture": posture, "u": u, **row}
        self.save()
        _write_json(os.path.join(self.dir, "posture.json"),
                    {"at": now, "profiles": postures, "advice": self.advice(st),
                     "hermes": {"setting": hermes_setting()[0], "profile": hermes_setting()[1]}})
        return postures

    def backfill(self, now=None):
        """Replay every transcript from byte zero. Slow, once."""
        for prof in self.profiles.values():
            prof.offsets, prof.buckets, prof.seen, prof.limits = {}, {}, {}, []
        self.calibration = {}
        self.n_readings = -1                         # force the merge: the dict was just emptied
        self.refresh_readings()                      # readings are not re-derivable; they live in a file
        self.hermes = HermesSource(self.hermes_db)
        n = self.scan(now)
        self.save()
        return n


# ----------------------------------------------------------------------------- daemon hooks

_METER = None
_LAST_TICK = 0.0
TICK_EVERY = 60


def tick(now=None, force=False):
    """Rate-limited entry for the daemon loop. Never raises: usage pacing must not take the daemon down."""
    global _METER, _LAST_TICK
    now = now or time.time()
    if not force and now - _LAST_TICK < TICK_EVERY:
        return None
    _LAST_TICK = now
    log = logging.getLogger("fleetwatch.usage")
    try:
        t = time.time()
        if _METER is None:
            _METER = Meter()
        out = _METER.tick(now)
        dt = time.time() - t
        (log.warning if dt > 20 else log.debug)("usage tick %.1fs", dt)
        return out
    except Exception:
        log.exception("usage meter tick failed")
        return None


def nudge_factor(profile=None, directory=None):
    """Multiplier on the nudge interval for one profile: below 1 nudges sooner, above 1 later. 1.0 when
    unsure. Reads the last posture file, so a stale or missing file means no change."""
    p = _read_json(os.path.join(directory or state_dir(), "posture.json"), None)
    if not p or time.time() - p.get("at", 0) > 900:
        return 1.0
    row = (p.get("profiles") or {}).get(profile) if profile else None
    posture = row.get("posture") if row else None     # an unknown profile never changes anything
    return {"hold": 2.0, "push": 0.5}.get(posture, 1.0)


def hermes_recent(hours=24, db=None):
    """Weighted-free token totals Hermes' own agents used on the API in the last hours."""
    import sqlite3
    path = db or os.path.expanduser("~/.hermes/state.db")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_write_tokens),0), COALESCE(SUM(cache_read_tokens),0) FROM sessions "
            "WHERE billing_provider='anthropic' AND model LIKE 'claude%' AND "
            "COALESCE(last_activity_at, started_at) >= ?", (time.time() - hours * 3600,)).fetchone()
        con.close()
        return row
    except Exception:
        return None


def _cli_reading(m, args):
    """reading <profile> <percent> [--scope session|weekly] --resets 'Fri 2:00 AM' | --resets-in '2h53m'

    Type in what the usage page shows. The reset time is required: it fixes where the window began."""
    import argparse
    ap = argparse.ArgumentParser(prog="usage_meter.py reading")
    ap.add_argument("profile", help="work, personal, or the full directory name (.claude-work)")
    ap.add_argument("percent", type=float)
    ap.add_argument("--scope", default="session", choices=("session", "weekly"))
    ap.add_argument("--resets", help="the reset time as shown, for example 'Fri 2:00 AM' or '11:00 PM'")
    ap.add_argument("--resets-in", help="the countdown as shown, for example '2 hr 53 min'")
    ap.add_argument("--minutes-ago", type=float, default=0.0,
                    help="the reading was taken this long ago (a screenshot you are reading from later)")
    a = ap.parse_args(args)
    if bool(a.resets) == bool(a.resets_in):
        ap.error("give exactly one of --resets or --resets-in")
    taken = time.time() - a.minutes_ago * 60
    name = a.profile if a.profile.startswith(".claude-") else ".claude-" + a.profile
    try:
        before, after = m.add_reading(name, a.percent, a.scope, parse_reset(a.resets or a.resets_in, taken), taken)
    except (KeyError, ValueError) as e:
        print("not recorded: %s" % e)
        return 1
    print("recorded %s %s %.0f%%: the meter said %.1f%% before, %.1f%% after" % (
        name, a.scope, a.percent, before * 100, after * 100))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    m = Meter()
    if cmd == "backfill":
        t = time.time()
        n = m.backfill()
        print("backfill: %d calibration events in %.0fs" % (n, time.time() - t))
        cmd = "status"
    elif cmd == "tick":
        m.tick()
        cmd = "status"
    if cmd == "reading":
        return _cli_reading(m, argv[2:])
    if cmd == "status":
        st, hp = hermes_setting()
        h = hermes_recent()
        if h and h[0]:
            note = {"counted": "counted in %s" % hp, "uncounted": "deliberately not counted",
                    "unset": "NOT COUNTED: choose a profile in the fleet plugin settings"}[st]
            print("hermes agents, last 24h: %d sessions, %.1fM tokens (%s)" % (h[0], sum(h[1:]) / 1e6, note))
        for name, row in m.status().items():
            print("%s: events=%d/%d agreeing, spread=%s trusted=%s" % (
                name, row["events"], row["total_events"], "n/a" if row["spread"] is None else "%.2f" % row["spread"], row["trusted"]))
            print("  weights per M tokens (fraction of limit): " + ", ".join(
                "%s=%.4f" % (k, v) for k, v in row["weights"].items()))
            if "used" in row:
                print("  window used=%.0f%% burn=%.2f%%/min projected=%.0f%%" % (
                    row["used"] * 100, row["burn_per_min"] * 100, row["projected"] * 100))
            if "weekly_used" in row:
                print("  week used=%.0f%% projected=%.0f%% (readings=%d, trusted=%s)" % (
                    row["weekly_used"] * 100, row["weekly_projected"] * 100,
                    row["weekly_events"], row["weekly_trusted"]))
            elif row.get("weekly_rolling"):
                print("  week (reset unknown, trailing 7 days) used=%.0f%%" % (row["weekly_rolling"] * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
