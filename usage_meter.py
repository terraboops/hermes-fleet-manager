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


# ----------------------------------------------------------------------------- estimator

class Estimator:
    """theta . (components in millions) = fraction of the limit used. Limit events pin it to 1."""

    def __init__(self, scale=None):
        s = scale if scale is not None else 0.05
        self.theta = [s * w for w in PRIOR_WEIGHTS]
        self.P = [(PRIOR_SPREAD * t) ** 2 for t in self.theta]       # diagonal covariance
        self.events = []              # components of each limit event used in the fit
        self.total_events = 0         # all events seen, including the ones rejected as noise

    @staticmethod
    def millions(comps):
        return [c / 1e6 for c in comps]

    def fraction(self, comps):
        return sum(t * x for t, x in zip(self.theta, self.millions(comps)))

    def seed_scale(self, events):
        """Pick the scale that fits the events best under the prior ratios, so RLS starts close."""
        ratios = []
        for comps in events:
            x = self.millions(comps)
            base = sum(w * v for w, v in zip(PRIOR_WEIGHTS, x))
            if base > 0:
                ratios.append(1.0 / base)
        if not ratios:
            return
        ratios.sort()
        s = ratios[len(ratios) // 2]
        self.theta = [s * w for w in PRIOR_WEIGHTS]
        self.P = [(PRIOR_SPREAD * t) ** 2 for t in self.theta]

    def update(self, comps):
        """One limit event: the fraction used was 1.0."""
        x = self.millions(comps)
        px = [p * v for p, v in zip(self.P, x)]
        s = sum(v * q for v, q in zip(x, px)) + EVENT_NOISE
        if s <= 0:
            return
        err = 1.0 - sum(t * v for t, v in zip(self.theta, x))
        k = [q / s for q in px]
        self.theta = [max(1e-9, t + ki * err) for t, ki in zip(self.theta, k)]
        self.P = [max(1e-12, p - ki * q) for p, ki, q in zip(self.P, k, px)]
        self.events.append(list(comps))

    def lower_bound(self, comps):
        """A window that ended with no hit: usage at the end was below the limit. Scale down if the
        estimate says it was above, never up."""
        f = self.fraction(comps)
        if f > 1.0:
            self.theta = [t / f for t in self.theta]

    def spread(self):
        """Coefficient of variation of the fraction each event implies after fitting: how well the
        weights explain the events. Zero when they agree."""
        fr = [self.fraction(c) for c in self.events]
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
        return {"theta": self.theta, "P": self.P, "events": self.events, "total_events": self.total_events}

    @classmethod
    def from_json(cls, d):
        e = cls()
        if d:
            e.theta, e.P, e.events = d["theta"], d["P"], d["events"]
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
    cs = [e["comps"] for e in events if e["scope"] == scope]
    est = Estimator()
    est.total_events = len(cs)
    if not cs:
        return est
    pw = sorted(sum(w * v / 1e6 for w, v in zip(PRIOR_WEIGHTS, c)) for c in cs)
    med = pw[len(pw) // 2]
    inliers = [c for c in cs
               if med > 0 and INLIER_LO <= sum(w * v / 1e6 for w, v in zip(PRIOR_WEIGHTS, c)) / med <= INLIER_HI]
    est.seed_scale(inliers)
    for c in inliers:
        est.update(c)
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
    return glob.glob(os.path.join(profile_dir, "projects", "*", "*.jsonl")) + \
        glob.glob(os.path.join(profile_dir, "projects", "*", "*", "subagents", "*.jsonl"))


class Meter:
    def __init__(self, directory=None, profiles=None):
        self.dir = directory or state_dir()
        self.profiles = {}
        self.est = {}                 # (profile, scope) -> Estimator
        self.ctl = {}                 # profile -> Controller
        self.paths = {os.path.basename(p): p for p in (profiles or profile_dirs())}
        self.load()

    # persistence
    def load(self):
        st = _read_json(os.path.join(self.dir, "state.json"), {})
        for name in self.paths:
            self.profiles[name] = Profile.from_json(name, (st.get("profiles") or {}).get(name, {}))
        cal = _read_json(os.path.join(self.dir, "calibration.json"), {"events": {}})
        self.calibration = cal["events"]
        for name in self.paths:
            for scope in ("session", "weekly"):
                evs = [e for e in self.calibration.get(name, []) if e["scope"] == scope]
                self.est[(name, scope)] = fit(evs, scope)
            self.ctl[name] = Controller.from_json((st.get("controllers") or {}).get(name))

    def save(self):
        _write_json(os.path.join(self.dir, "state.json"), {
            "profiles": {n: p.to_json() for n, p in self.profiles.items()},
            "controllers": {n: c.to_json() for n, c in self.ctl.items()}})
        _write_json(os.path.join(self.dir, "calibration.json"), {"events": self.calibration})

    # scanning
    def scan(self, now=None):
        now = now or time.time()
        new_events = 0
        for name, pdir in self.paths.items():
            prof = self.profiles[name]
            for path in transcripts(pdir):
                prof.scan_file(path)
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
            out[name] = row
        return out

    def tick(self, now=None):
        """One daemon pass: scan, step each controller, persist. Returns the posture per profile."""
        now = now or time.time()
        self.scan(now)
        postures = {}
        for name, row in self.status(now).items():
            if "projected" not in row:
                postures[name] = {"posture": "steady", "u": 0.0, **row}
                continue
            hit = any(row["window_start"] <= e["ts"] < row["window_end"]
                      for e in self.calibration.get(name, []) if e["scope"] == "session")
            posture, u = self.ctl[name].step(row["window_start"], row["projected"],
                                             row["trusted"], hit, now)
            postures[name] = {"posture": posture, "u": u, **row}
        self.save()
        _write_json(os.path.join(self.dir, "posture.json"), {"at": now, "profiles": postures})
        return postures

    def backfill(self, now=None):
        """Replay every transcript from byte zero. Slow, once."""
        for prof in self.profiles.values():
            prof.offsets, prof.buckets, prof.seen, prof.limits = {}, {}, {}, []
        self.calibration = {}
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
    try:
        if _METER is None:
            _METER = Meter()
        return _METER.tick(now)
    except Exception:
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
    if cmd == "status":
        for name, row in m.status().items():
            print("%s: events=%d/%d agreeing, spread=%s trusted=%s" % (
                name, row["events"], row["total_events"], "n/a" if row["spread"] is None else "%.2f" % row["spread"], row["trusted"]))
            print("  weights per M tokens (fraction of limit): " + ", ".join(
                "%s=%.4f" % (k, v) for k, v in row["weights"].items()))
            if "used" in row:
                print("  window used=%.0f%% burn=%.2f%%/min projected=%.0f%%" % (
                    row["used"] * 100, row["burn_per_min"] * 100, row["projected"] * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
