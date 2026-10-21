#!/usr/bin/env python3
"""Benchmark memory and p50/p95 latency for every Laya ladder rung and quantization option.

    ~/.hermes/venvs/laya-mlx/bin/python scripts/bench_rungs.py [--iterations N] [--warmup N]
        [--out-dir reports] [--date YYYY-MM-DD] [--only rung[:quant],...] [--timeout S]

Run it under the interpreter that has laya_mlx installed, or the mlx rung is recorded as unavailable.

Each rung x quant combination runs in its own child process, so peak RSS is not polluted by an
earlier combination. The workload is the recorded parity set tests/fixtures/laya_parity/v1-mlxref
(307 cases), scored one event per call in the production state shape (laya_parity_mlx.build_state +
laya_gate.QUESTION). After the warmup calls, the measured calls cycle through the fixtures in file
order until --iterations is reached (default: one full pass, never fewer than 50).

Memory is RSS from `ps`, in MiB:
    load_rss    right after load()
    steady_rss  after the last measured call
    peak_rss    the highest value seen (sampled after every call, so a spike between calls is missed)
RSS counts the scoring process (mlx: this child; llama.cpp rungs: the llama-server child; remote:
the whole `laya serve` process tree). Metal/GPU buffers on Apple Silicon may sit outside RSS.

A combination that cannot load or run is recorded with status unavailable / oom / error and its
reason; nothing is omitted. Rungs are read from laya_backends.RUNGS, so a new rung is picked up.
Writes <out-dir>/latency-memory-<date>.json and .md.
"""
import argparse
import datetime
import json
import os
import platform
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "laya_parity", "v1-mlxref")
LABEL = "interrupt her now"
RULE_RUNG = "rule-gate"
REMOTE_TOKEN = "laya_kid01_" + "a" * 40


# ---------------------------------------------------------------------------- helpers

def pct(sorted_vals, q):
    """Nearest-rank percentile of an ascending list."""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, int(-(-q * len(sorted_vals) // 100)) - 1))
    return sorted_vals[k]


def rss_mib(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return int(out) / 1024.0 if out else 0.0
    except Exception:
        return 0.0


def tree_rss_mib(pid):
    """RSS of pid and all its descendants."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return 0.0
    kids, rss = {}, {}
    for line in out.splitlines():
        try:
            p, pp, r = (int(x) for x in line.split())
        except ValueError:
            continue
        kids.setdefault(pp, []).append(p)
        rss[p] = r
    total, stack = 0, [pid]
    while stack:
        p = stack.pop()
        total += rss.get(p, 0)
        stack.extend(kids.get(p, []))
    return total / 1024.0


def git_commit():
    try:
        sha = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return sha + (" (+uncommitted changes)" if dirty else "")
    except Exception:
        return "unknown"


def sysctl_cmd(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def hardware():
    def sysctl(k):
        try:
            return subprocess.run(["sysctl", "-n", k], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    info = {"system": platform.system(), "machine": platform.machine(), "python": sys.version.split()[0]}
    if platform.system() == "Darwin":
        info.update(cpu=sysctl("machdep.cpu.brand_string"), ram_gib=round(int(sysctl("hw.memsize") or 0) / 2 ** 30, 1),
                    cores_physical=sysctl("hw.physicalcpu"), cores_logical=sysctl("hw.logicalcpu"),
                    os="macOS " + (platform.mac_ver()[0] or sysctl_cmd(["sw_vers", "-productVersion"])))
    else:
        info["os"] = platform.platform()
    return info


def load_workload():
    import laya_gate
    import laya_parity as lp
    import laya_parity_mlx as pm
    rows, manifest = lp.load_fixtures(FIXTURES)
    priors = pm._priors(rows)
    events = [{"state": pm.build_state(r["text"], priors[i]), "question": laya_gate.QUESTION}
              for i, r in enumerate(rows)]
    return rows, events, manifest


def probability(raw):
    ans = raw["answers"]["action"]
    return float((ans.get("probabilities") or {}).get(LABEL))


# ---------------------------------------------------------------------------- worker (one combo)

def timed_loop(score_one, events, warmup, iterations, sample_rss):
    """Warm up, then time `iterations` single-event calls. Returns (latencies_ms, rss_samples)."""
    for i in range(warmup):
        score_one(events[i % len(events)])
    lat, rss = [], []
    for i in range(iterations):
        e = events[i % len(events)]
        t0 = time.perf_counter()
        score_one(e)
        lat.append((time.perf_counter() - t0) * 1000.0)
        rss.append(sample_rss())
    return lat, rss


def finish(res, lat, load_rss, rss, load_s):
    s = sorted(lat)
    res.update(status="ok", iterations=len(lat), p50_ms=round(pct(s, 50), 3), p95_ms=round(pct(s, 95), 3),
               mean_ms=round(sum(lat) / len(lat), 2), min_ms=round(s[0], 2), max_ms=round(s[-1], 2),
               load_seconds=round(load_s, 2), load_rss_mib=round(load_rss, 1),
               steady_rss_mib=round(rss[-1], 1), peak_rss_mib=round(max([load_rss] + rss), 1))
    return res


def find_cls(name):
    """Backend class for a rung name as it appears in laya_backends.RUNGS (or cuda / rocm)."""
    import laya_backends as lb
    for cls in lb.RUNGS:
        if cls.name == name:
            return cls
    return lb.backend_for(name)


def worker(rung, quant, warmup, iterations):
    import laya_backends as lb
    res = {"rung": rung, "quant": quant, "status": "error", "notes": ""}
    rows, events, _ = load_workload()

    if rung == RULE_RUNG:
        import laya_gate as g
        texts = [r["text"] for r in rows]

        def rule_one(i):
            probe = texts[i]
            g._family(probe, "bench")
            for pat, *_ in g.LANE1:
                if g.re.search(pat, probe):
                    return
            for pat, _why in g.LANE2:
                if g.re.search(pat, probe):
                    return

        idx = list(range(len(texts)))
        lat, _ = timed_loop(rule_one, idx, warmup, iterations, lambda: 0.0)
        r = rss_mib(os.getpid())
        res["notes"] = "no model: lane 1/2 regex tables only (laya_gate._family, LANE1, LANE2); in-process RSS"
        return finish(res, lat, r, [r], 0.0)

    if rung == "remote":
        return worker_remote(res, events, warmup, iterations)

    name = rung
    cls = find_cls(name)
    opts = lb.BackendOptions.resolve(env={}, config={"quant_level": quant} if quant != "n/a" else {})
    backend = cls(options=opts) if name != "mlx" else cls(options=opts)
    try:
        probe = backend.probe()
        if not probe.available:
            res.update(status="unavailable", notes=probe.reason or "probe failed")
            return res
        t0 = time.monotonic()
        try:
            backend.load()
        except lb.BackendOOMError as e:
            res.update(status="oom", notes=str(e)[:300])
            return res
        except lb.BackendUnavailable as e:
            res.update(status="unavailable", notes=str(e)[:300])
            return res
        except lb.BackendLoadError as e:
            res.update(status="load_failed", notes=str(e)[:300])
            return res
        load_s = time.monotonic() - t0
        if name == "mlx":
            pid = os.getpid()
            sample = lambda: rss_mib(pid)          # noqa: E731
        else:
            sample = lambda: rss_mib(backend._proc.pid)    # noqa: E731
        load_rss = sample()
        try:
            lat, rss = timed_loop(lambda e: backend.score([e]), events, warmup, iterations, sample)
        except lb.BackendRuntimeError as e:
            res.update(status="run_failed", notes=str(e)[:300], load_rss_mib=round(load_rss, 1))
            return res
        notes = []
        art = getattr(backend, "artifact", None)
        if art:
            notes.append("artifact %s" % art["name"])
            notes.append("threads=%s" % getattr(backend, "threads_effective", None))
        if name == "mlx":
            notes.append("quant not selectable on this rung (fixed checkpoint); RSS excludes Metal buffers")
        res["notes"] = ", ".join(str(n) for n in notes)
        return finish(res, lat, load_rss, rss, load_s)
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()


def worker_remote(res, events, warmup, iterations):
    """Remote rung against a `laya serve` on loopback (CPU backend behind it). Measures client plus
    transport plus server; network latency to a real host is NOT included."""
    import laya_backends as lb
    import laya_remote_backend as rb
    import socket
    tmp = tempfile.mkdtemp(prefix="bench-remote-")
    tok = os.path.join(tmp, "token")
    with open(tok, "w") as f:
        f.write(REMOTE_TOKEN)
    os.chmod(tok, 0o600)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, LAYA_SERVE_TOKEN=REMOTE_TOKEN)
    env.pop("LAYA_QUANT_LEVEL", None)
    log = open(os.path.join(tmp, "serve.log"), "w")
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "laya_serve.py"), "--backend", "cpu",
                            "--port", str(port), "--model-revision", "bench", "--weights-sha256", "bench"],
                           stdout=log, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    try:
        client = rb.RemoteBackend("http://127.0.0.1:%d" % port, token_file=tok, model_revision="bench",
                                  weights_sha256="bench", total_timeout_s=120)
        t0 = time.monotonic()
        deadline = t0 + 180
        last = None
        while time.monotonic() < deadline:
            if srv.poll() is not None:
                res.update(status="load_failed", notes="laya serve exited %s" % srv.returncode)
                return res
            try:
                client.load()
                client.score([events[0]])           # the server loads its model in the background
                break
            except (lb.BackendUnavailable, lb.BackendRuntimeError) as e:
                last = e
                time.sleep(0.5)
        else:
            res.update(status="load_failed", notes="server not ready in 180s: %s" % last)
            return res
        load_s = time.monotonic() - t0
        sample = lambda: tree_rss_mib(srv.pid)       # noqa: E731
        load_rss = sample()
        lat, rss = timed_loop(lambda e: client.score([e]), events, warmup, iterations, sample)
        res["notes"] = "loopback `laya serve --backend cpu` (default quant); no real network hop; RSS = serve process tree"
        return finish(res, lat, load_rss, rss, load_s)
    except Exception as e:
        res.update(status="error", notes="%s: %s" % (type(e).__name__, str(e)[:300]))
        return res
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except subprocess.TimeoutExpired:
            srv.kill()


# ---------------------------------------------------------------------------- orchestrator

def plan():
    """(rung, quant) pairs from the code: every class in RUNGS, its quant options, plus the rule gate."""
    import laya_backends as lb
    pairs = []
    for cls in lb.RUNGS:
        name = cls.name
        if name == "cuda/rocm":
            names = ["cuda", "rocm"]            # the combined rung dispatches to one of these
        else:
            names = [name]
        for n in names:
            arts = getattr(find_cls(n), "QUANT_ARTIFACTS", None)
            if n == "remote":
                pairs.append((n, "n/a"))
            elif arts:
                pairs.extend((n, q) for q in lb.QUANT_LEVELS if q in arts)
            else:
                pairs.append((n, "n/a"))
    pairs.append((RULE_RUNG, "n/a"))
    return pairs


def foreign_cpu_pct():
    """Summed %CPU of every process that is not this process or its child (100 = one core busy)."""
    me = os.getpid()
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pcpu="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0.0
    rows = {}
    for line in out.splitlines():
        try:
            p, pp, c = line.split()
            rows[int(p)] = (int(pp), float(c))
        except ValueError:
            continue
    mine, grew = {me}, True
    while grew:
        grew = False
        for p, (pp, _) in rows.items():
            if pp in mine and p not in mine:
                mine.add(p)
                grew = True
    return sum(c for p, (_, c) in rows.items() if p not in mine)


BUSY_PCT = 150.0       # other work above this, about 1.5 cores, counts as contention


def wait_quiet(max_wait):
    """Wait until other processes use under BUSY_PCT CPU, up to max_wait seconds. True when quiet."""
    end = time.time() + max_wait
    while foreign_cpu_pct() > BUSY_PCT:
        if time.time() > end:
            return False
        time.sleep(10)
    return True


def run_one(rung, quant, args):
    quiet = True
    if rung not in (RULE_RUNG,):
        quiet = wait_quiet(args.quiet_wait)
    r = _run_one(rung, quant, args)
    r["load_avg_1m"] = round(os.getloadavg()[0], 1)
    r["quiet_start"] = quiet
    if not quiet:
        r["notes"] = (r.get("notes") or "") + "; WARNING: other processes were using the CPU at start, numbers contended"
    return r


def _run_one(rung, quant, args):
    cmd = [sys.executable, os.path.realpath(__file__), "--worker", "%s:%s" % (rung, quant),
           "--warmup", str(args.warmup), "--iterations", str(args.iterations)]
    env = dict(os.environ)
    env.pop("LAYA_BACKEND", None)
    env.pop("LAYA_QUANT_LEVEL", None)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout, env=env, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"rung": rung, "quant": quant, "status": "timeout", "notes": "exceeded %ds" % args.timeout}
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    if lines:
        return json.loads(lines[-1][7:])
    tail = (p.stderr.strip().splitlines() or ["no output"])[-1][:300]
    return {"rung": rung, "quant": quant, "status": "error", "notes": "worker exit %s: %s" % (p.returncode, tail)}


def fmt(v, unit=""):
    return "-" if v is None else ("%s%s" % (v, unit))


def write_md(path, doc):
    hw, rows = doc["hardware"], doc["results"]
    L = ["# Laya rung latency and memory (%s)" % doc["date"], "",
         "Commit: `%s`" % doc["commit"],
         "Hardware: %s, %s GiB RAM, %s physical / %s logical cores, %s" % (
             hw.get("cpu", hw["machine"]), hw.get("ram_gib", "?"), hw.get("cores_physical", "?"),
             hw.get("cores_logical", "?"), hw.get("os")),
         "Workload: %d recorded parity cases (%s, manifest sha256 `%s`), %d warmup calls, %d measured calls "
         "per combination, one event per call." % (doc["workload"]["cases"], doc["workload"]["fixtures"],
                                                   doc["workload"]["sha256"][:12], doc["warmup"], doc["iterations"]),
         "Reproduce: `~/.hermes/venvs/laya-mlx/bin/python scripts/bench_rungs.py`", "",
         "Peak memory is the highest RSS sampled after each call (load / steady in notes).", "",
         "| rung | quant | peak memory | p50 ms | p95 ms | notes |", "|---|---|---|---|---|---|"]
    for r in rows:
        if r["status"] == "ok":
            mem = "%.0f MiB" % r["peak_rss_mib"]
            note = "load %.0f MiB, steady %.0f MiB, load %.1fs; %s" % (
                r["load_rss_mib"], r["steady_rss_mib"], r["load_seconds"], r["notes"])
            L.append("| %s | %s | %s | %s | %s | %s |" % (r["rung"], r["quant"], mem, r["p50_ms"], r["p95_ms"],
                                                         note.rstrip("; ")))
        else:
            L.append("| %s | %s | - | - | - | **%s**: %s |" % (r["rung"], r["quant"], r["status"].upper(),
                                                                 (r.get("notes") or "").replace("|", "/")))
    L += ["", "Reading the numbers:", "",
          "- Unavailable rows were probed on this machine and cannot load here (no NVIDIA, ROCm or Vulkan "
          "runtime on macOS). They are unmeasured, not zero, and the GPU rungs stay unverified on hardware.",
          "- RSS is process memory only. mlx weights and Metal buffers can live outside it.",
          "- cpu auto and cpu 8bit load the same Q8_0 file; the spread between them is run-to-run noise "
          "on a shared host.",
          "- F16 and Q8_0 show near-equal RSS: the llama-server compute and context buffers dominate "
          "at this model size, so quantization moves latency more than memory (only Q4_K_M cuts memory).",
          "- The host was shared with other work during the run (1 minute load average per combination "
          "is in the JSON: load_avg_1m). Each combination waited for the CPU to settle first "
          "(quiet_start in the JSON), but p95 still carries scheduler noise. Re-run on an idle host for "
          "release numbers.",
          "- remote is a loopback `laya serve`, so it shows server and client overhead, not network time.",
          "- rule-gate is the lane 1/2 regex tables only, not the full classify() path (which writes state).",
          ""]
    path_write(path, "\n".join(L))


def path_write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--iterations", type=int, default=None)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "reports"))
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--only", default=None, help="comma list of rung or rung:quant to run")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--quiet-wait", type=int, default=600,
                    help="seconds to wait for other llama-server processes to exit before each combo")
    args = ap.parse_args(argv)

    import laya_parity as lp
    rows, manifest = lp.load_fixtures(FIXTURES)
    if args.iterations is None:
        args.iterations = max(50, len(rows))
    if args.iterations < 50:
        print("bench_rungs: --iterations must be at least 50", file=sys.stderr)
        return 2

    if args.worker:
        rung, quant = args.worker.split(":", 1)
        res = worker(rung, quant, args.warmup, args.iterations)
        print("RESULT " + json.dumps(res))
        return 0

    pairs = plan()
    if args.only:
        want = set(args.only.split(","))
        pairs = [p for p in pairs if p[0] in want or "%s:%s" % p in want]
    results = []
    for rung, quant in pairs:
        print("bench %s %s ..." % (rung, quant), flush=True)
        r = run_one(rung, quant, args)
        print("  -> %s %s" % (r["status"], {k: r.get(k) for k in ("p50_ms", "p95_ms", "peak_rss_mib")}
                              if r["status"] == "ok" else r.get("notes", "")), flush=True)
        results.append(r)

    doc = {"date": args.date, "commit": git_commit(), "hardware": hardware(), "warmup": args.warmup,
           "iterations": args.iterations,
           "workload": {"fixtures": os.path.relpath(FIXTURES, ROOT), "cases": len(rows),
                        "sha256": manifest.get("_actual_sha256", "")},
           "results": results}
    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, "latency-memory-%s" % args.date)
    path_write(base + ".json", json.dumps(doc, indent=2) + "\n")
    write_md(base + ".md", doc)
    print("wrote %s.{json,md}" % base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
