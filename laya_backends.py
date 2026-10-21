"""Backend abstraction for the Laya gate.

A backend loads a model once (`load`) and scores a batch of events (`score`). The gate walks
RUNGS in order and uses the first backend whose load succeeds; `backend_for` resolves an
explicit override name instead.

Backend contract (every rung honours it, so the manager can fall back without special cases):

    probe() / is_available()  Cheap. Imports and device queries only, never loads weights. Returns
                              Probe(available, reason). A backend that is not available is skipped
                              without load() being called.
    load()                    Loads weights. Returns LoadResult(load_seconds, detail); the manager
                              logs it. Raises BackendOOMError when the device runs out of memory and
                              BackendLoadError for any other load failure (BackendUnavailable when the
                              backend turns out not to run here). All three mean "try the next rung".
                              Any other exception is a bug and propagates.
    score(events)             One raw output per event. Raises BackendRuntimeError on failure.

Backends receive a BackendOptions (quant_level, batch_size, num_threads, max_seq_len) at
construction. Defaults suit a weak machine; override with LAYA_QUANT_LEVEL, LAYA_BATCH_SIZE,
LAYA_NUM_THREADS, LAYA_MAX_SEQ_LEN, or `laya.options` in config.yaml (env wins).

An event is a dict with `state` and `question`. `score` returns one raw model output per event,
in the shape laya_mlx's `Agent.predict` returns:
    {"answers": {"action": {"probabilities": {label: p}, "confidence": p}}}
"""
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields

import laya_gpu

log = logging.getLogger("laya.backend")

DEFAULT_MODEL = os.environ.get("LAYA_GATE_MODEL", "aac6fef/laya-mlx")


class BackendLoadError(Exception):
    """The backend could not be made ready (missing package, bad checkpoint, no device)."""


class BackendUnavailable(BackendLoadError):
    """The backend cannot run on this machine. Raised by a load() that finds that out late; probe()
    reports the same thing up front without loading."""


class BackendOOMError(BackendLoadError):
    """The device ran out of memory. Kept a load error so a ladder walk treats it as 'try the next rung'."""


class BackendRuntimeError(Exception):
    """Scoring failed after a successful load."""


@dataclass(frozen=True)
class Probe:
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class LoadResult:
    load_seconds: float = 0.0
    detail: str = ""


ENV_OPTION_VARS = {"quant_level": "LAYA_QUANT_LEVEL", "batch_size": "LAYA_BATCH_SIZE",
                   "num_threads": "LAYA_NUM_THREADS", "max_seq_len": "LAYA_MAX_SEQ_LEN"}
QUANT_LEVELS = ("auto", "none", "8bit", "4bit")


@dataclass(frozen=True)
class BackendOptions:
    """Knobs for a weak machine. num_threads 0 means 'let the backend choose'."""
    quant_level: str = "auto"
    batch_size: int = 8
    num_threads: int = 0
    max_seq_len: int = 512

    @classmethod
    def resolve(cls, env=None, config=None):
        """Defaults, then config (`laya.options`), then env. A bad value is logged and ignored."""
        env = os.environ if env is None else env
        config = config if isinstance(config, dict) else {}
        defaults = cls()
        values = {}
        for f in fields(cls):
            values[f.name] = getattr(defaults, f.name)
            for source, raw in (("config", config.get(f.name)), ("env", env.get(ENV_OPTION_VARS[f.name]))):
                if raw is None or not str(raw).strip():
                    continue
                try:
                    values[f.name] = cls._coerce(f.name, raw)
                except ValueError as e:
                    log.warning("laya option %s from %s ignored: %s", f.name, source, e)
        return cls(**values)

    @staticmethod
    def _coerce(name, raw):
        if name == "quant_level":
            v = str(raw).strip().lower()
            if v not in QUANT_LEVELS:
                raise ValueError(f"{raw!r} is not one of {', '.join(QUANT_LEVELS)}")
            return v
        v = int(str(raw).strip())
        if v < (0 if name == "num_threads" else 1):
            raise ValueError(f"{raw!r} is out of range")
        return v


class LayaBackend(ABC):
    """See the module docstring for the contract."""
    name = ""

    def __init__(self, options=None):
        self.options = options or BackendOptions()

    def probe(self):
        """Cheap availability check; never loads weights. Default: available."""
        return Probe(True)

    def is_available(self):
        return self.probe().available

    @abstractmethod
    def load(self):
        """Load the model. Return LoadResult. Raise BackendOOMError / BackendLoadError on failure."""

    @abstractmethod
    def score(self, events):
        """Return one raw model output per event. Raise BackendRuntimeError on failure."""


_OOM_MARKERS = ("out of memory", "failed to allocate", "attempting to allocate", "insufficient memory",
                "metal::malloc", "kiogpucommandbuffercallbackerroroutofmemory")


def _is_oom(exc):
    if isinstance(exc, MemoryError):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _OOM_MARKERS)


class MlxBackend(LayaBackend):
    """The existing laya_mlx path: `laya_mlx.load(model)` then `.predict(state, question)`."""
    name = "mlx"

    def __init__(self, model=None, options=None):
        super().__init__(options)
        self.model = model or DEFAULT_MODEL
        self._agent = None

    def probe(self):
        if "laya_mlx" in sys.modules:
            return Probe(True)
        try:
            found = importlib.util.find_spec("laya_mlx") is not None
        except Exception as e:                       # a broken parent package
            return Probe(False, f"{type(e).__name__}: {e}")
        return Probe(found, "" if found else "laya_mlx is not installed")

    def load(self):
        t0 = time.monotonic()
        try:
            import laya_mlx
        except Exception as e:                       # ImportError, or a broken native extension
            raise BackendUnavailable(f"{type(e).__name__}: {e}") from e
        try:
            self._agent = laya_mlx.load(self.model)
        except Exception as e:
            if _is_oom(e):
                raise BackendOOMError(f"{type(e).__name__} loading {self.model}: {e}") from e
            raise BackendLoadError(f"{type(e).__name__} loading {self.model}: {e}") from e
        return LoadResult(time.monotonic() - t0, self.model)

    def score(self, events):
        if self._agent is None:
            raise BackendRuntimeError("mlx backend scored before load()")
        try:
            return [self._agent.predict(e["state"], e["question"]) for e in events]
        except Exception as e:
            raise BackendRuntimeError(f"{type(e).__name__}: {e}") from e


class StubBackend(LayaBackend):
    """Deterministic fake scores for dry-run mode: a stable hash of the event picks the probability."""
    name = "stub"

    def load(self):
        return LoadResult(0.0, "stub")

    def score(self, events):
        out = []
        for e in events:
            digest = hashlib.sha256(f"{e['state']}\x00{e['question']}".encode("utf-8")).digest()
            p = round(int.from_bytes(digest[:4], "big") / 0xFFFFFFFF, 6)
            out.append({"answers": {"action": {
                "probabilities": {"interrupt her now": p, "hold": round(1.0 - p, 6)},
                "confidence": p}}})
        return out


def make_fake_backend(name="fake", available=True, reason="", load_exc=None, oom=False,
                      load_seconds=0.0, score_fn=None):
    """A test rung. `oom=True` fails load() with BackendOOMError; `load_exc` raises that instead.
    The class counts its load() calls in `.loads`, so a test can prove an unavailable rung never loaded."""
    class FakeBackend(LayaBackend):
        loads = 0

        def probe(self):
            return Probe(available, reason or ("" if available else "fake: unavailable"))

        def load(self):
            type(self).loads += 1
            if oom:
                raise BackendOOMError("fake: simulated out of memory")
            if load_exc is not None:
                raise load_exc
            return LoadResult(load_seconds, "fake")

        def score(self, events):
            if score_fn is not None:
                return score_fn(events)
            return [{"answers": {"action": {"probabilities": {"interrupt her now": 0.5},
                                            "confidence": 0.5}}, "from": name} for _ in events]
    FakeBackend.name = name
    FakeBackend.__name__ = f"Fake_{name}"
    return FakeBackend


class _Unimplemented(LayaBackend):
    """Placeholder rung. A later task replaces probe/load/score."""

    def probe(self):
        return Probe(False, "not implemented")

    def load(self):
        raise BackendLoadError("not implemented")

    def score(self, events):
        raise BackendRuntimeError("not implemented")


CPU_QUANT_ARTIFACTS = {"auto": "laya-q8_0", "8bit": "laya-q8_0", "none": "laya-f16", "4bit": "laya-q4_k_m"}
CPU_WEAK_QUANTS = ("4bit",)            # recorded as failing parity on the sample set; opt-in only
CPU_RAM_HEADROOM = 1.25                # file size x this, plus the context buffers, must be free
_CPU_OOM_MARKERS = _OOM_MARKERS + ("cannot allocate memory", "std::bad_alloc", "unable to allocate",
                                   "ggml_backend_cpu_buffer_type_alloc_buffer")
_HERE = os.path.dirname(os.path.realpath(__file__))


def _laya_cache_root():
    env = os.environ.get("LAYA_CACHE")
    if env:
        return os.path.expanduser(env)
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Caches/thos/laya")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "thos", "laya")


def _free_ram_bytes():
    """Memory a new process can use without swapping, or None when unknown. Cheap."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        elif sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(out.split("page size of")[1].split("bytes")[0])
            pages = {}
            for line in out.splitlines()[1:]:
                k, _, v = line.partition(":")
                v = v.strip().rstrip(".")
                if v.isdigit():
                    pages[k.strip()] = int(v)
            usable = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
                      + pages.get("Pages speculative", 0) + pages.get("Pages purgeable", 0))
            return usable * page
    except Exception:
        return None
    return None


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _rss_bytes(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        return int(out) * 1024 if out else 0
    except Exception:
        return 0


class LlamaServerBackend(LayaBackend):
    """Shared base for every rung that runs a pinned `llama-server` on loopback (CPU and the GPU rungs).

    Subclasses set QUANT_ARTIFACTS, GPU_LAYERS and OOM_MARKERS and may override `_binary`,
    `_probe_device`, `_memory_shortfall`, `_server_env` and `_server_args`.

    The CPU rung is documented below; the original docstring follows.

    Quantized CPU rung: a pinned `llama-server` on loopback serving a GGUF from the artifact cache.

    quant_level picks the file (auto/8bit -> Q8_0, none -> F16, 4bit -> Q4_K_M), whose sha256 is checked
    against laya_artifacts.json before the server starts. num_threads is passed as -t/-tb, max_seq_len as
    the context size and ubatch, and batch_size is how many events go to the server per round. Binary
    lookup: LAYA_LLAMA_SERVER, then the cache build tree, then PATH.
    """
    name = "llama-server"
    LABEL_WAIT_SECONDS = 180
    QUANT_ARTIFACTS = CPU_QUANT_ARTIFACTS
    GPU_LAYERS = 0
    OOM_MARKERS = _CPU_OOM_MARKERS
    PROBE_CHECKS_MEMORY = True

    def __init__(self, options=None, manifest_path=None, cache_root=None, server_bin=None):
        super().__init__(options)
        self.manifest_path = manifest_path or os.path.join(_HERE, "laya_artifacts.json")
        self.cache_root = cache_root or _laya_cache_root()
        self._server_bin = server_bin
        self._proc = None
        self._port = None
        self._log_path = None
        self.artifact = None
        self.peak_rss = 0
        self.load_seconds = 0.0
        self.threads_effective = None

    # -- resolution -------------------------------------------------------------------------
    def _binary(self):
        cands = [self._server_bin, os.environ.get("LAYA_LLAMA_SERVER"),
                 os.path.join(self.cache_root, "build", "llama.cpp", "build", "bin", "llama-server"),
                 shutil.which("llama-server")]
        for c in cands:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    def _entry(self):
        """Manifest entry for the selected quant level."""
        want = self.QUANT_ARTIFACTS[self.options.quant_level]
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            raise BackendLoadError(f"cannot read artifact manifest {self.manifest_path}: {e}") from e
        for a in manifest.get("artifacts", []):
            if a.get("name") == want:
                return a
        raise BackendLoadError(f"artifact {want} is not in {self.manifest_path}")

    def _artifact_path(self, entry):
        rel = entry.get("cache_path") or f"blobs/{entry['sha256']}/{entry['file']}"
        return os.path.join(self.cache_root, rel)

    def _need_bytes(self, entry):
        return int(entry["size_bytes"] * CPU_RAM_HEADROOM) + 64 * 1024 * 1024

    # -- hooks ------------------------------------------------------------------------------
    def _probe_device(self):
        """Probe for the device this rung needs. None means no device is needed (CPU)."""
        return None

    def _memory_shortfall(self, entry):
        """A message when the model will not fit in the memory it runs from, else None. Cheap."""
        free, need = _free_ram_bytes(), self._need_bytes(entry)
        if free is not None and free < need:
            return f"{entry['name']} needs about {need >> 20} MiB of RAM, {free >> 20} MiB free"
        return None

    def _server_env(self):
        return None

    def _server_args(self):
        return []

    def _device_detail(self):
        return ""

    # -- contract ---------------------------------------------------------------------------
    def probe(self):
        dev = self._probe_device()
        if dev is not None and not dev.available:
            return dev
        if self._binary() is None:
            return Probe(False, "llama-server not found (set LAYA_LLAMA_SERVER or build the cache)")
        try:
            entry = self._entry()
        except BackendLoadError as e:
            return Probe(False, str(e))
        if not os.path.isfile(self._artifact_path(entry)):
            return Probe(False, f"{entry['name']} is not in the cache {self.cache_root}")
        if self.PROBE_CHECKS_MEMORY:
            short = self._memory_shortfall(entry)
            if short:
                return Probe(False, f"not enough free RAM: {short}")
        return Probe(True)

    def _free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def load(self):
        t0 = time.monotonic()
        binary = self._binary()
        if binary is None:
            raise BackendUnavailable("llama-server not found (set LAYA_LLAMA_SERVER)")
        entry = self._entry()
        path = self._artifact_path(entry)
        if not os.path.isfile(path):
            raise BackendLoadError(f"{entry['name']} missing from cache: {path}")
        short = self._memory_shortfall(entry)
        if short:
            raise BackendOOMError(short)
        if os.path.getsize(path) != entry["size_bytes"]:
            raise BackendLoadError(f"{path}: size {os.path.getsize(path)} != manifest {entry['size_bytes']}")
        actual = _file_sha256(path)
        if actual != entry["sha256"]:
            raise BackendLoadError(f"{path}: sha256 {actual} does not match manifest {entry['sha256']}")
        if self.options.quant_level in CPU_WEAK_QUANTS:
            log.warning("laya %s: %s failed parity on the sample set; selected explicitly", self.name, entry["name"])
        self.artifact = dict(entry, path=path)
        self.close()
        ctx = self.options.max_seq_len
        threads = self.options.num_threads
        self._port = self._free_port()
        cmd = [binary, "-m", path, "--host", "127.0.0.1", "--port", str(self._port), "-ngl", str(self.GPU_LAYERS),
               "-c", str(ctx), "-b", str(ctx), "-ub", str(ctx), "-np", "1", "--no-webui"]
        if threads:
            cmd += ["-t", str(threads), "-tb", str(threads)]
        cmd += self._server_args()
        fd, self._log_path = tempfile.mkstemp(prefix="laya-cpu-", suffix=".log")
        try:
            self._proc = subprocess.Popen(cmd, stdout=fd, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                          env=self._server_env())
        except MemoryError as e:
            raise BackendOOMError(f"spawning llama-server: {e}") from e
        except OSError as e:
            if _is_oom(e):
                raise BackendOOMError(f"spawning llama-server: {e}") from e
            raise BackendLoadError(f"cannot start {binary}: {e}") from e
        finally:
            os.close(fd)
        self._wait_ready()
        self.load_seconds = time.monotonic() - t0
        self.threads_effective = self._logged_threads()
        self.peak_rss = max(self.peak_rss, _rss_bytes(self._proc.pid))
        detail = (f"{entry['name']} sha256 ok, {self._device_detail()}threads={self.threads_effective or threads or 'auto'}, "
                  f"ctx={ctx}, batch={self.options.batch_size}, rss={self.peak_rss >> 20} MiB")
        log.info("laya %s loaded: %s in %.2fs", self.name, detail, self.load_seconds)
        return LoadResult(self.load_seconds, detail)

    def _log_text(self):
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, TypeError):
            return ""

    def _logged_threads(self):
        import re
        m = re.search(r"n_threads\s*=\s*(\d+)", self._log_text())
        return int(m.group(1)) if m else None

    def _wait_ready(self):
        deadline = time.monotonic() + self.LABEL_WAIT_SECONDS
        url = f"http://127.0.0.1:{self._port}/health"
        while time.monotonic() < deadline:
            rc = self._proc.poll()
            if rc is not None:
                text = self._log_text()
                tail = " | ".join(text.strip().splitlines()[-3:])[:400]
                self.close()
                if any(m in text.lower() for m in self.OOM_MARKERS) or rc in (-9, 137):
                    raise BackendOOMError(f"llama-server died loading the model (exit {rc}): {tail}")
                raise BackendLoadError(f"llama-server exited {rc} while loading: {tail}")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.1)
        self.close()
        raise BackendLoadError(f"llama-server not ready after {self.LABEL_WAIT_SECONDS}s")

    # -- scoring ----------------------------------------------------------------------------
    @staticmethod
    def _wire_question(question):
        """laya_mlx accepts criteria as a list; llama-server wants an object."""
        out = {}
        for k, q in question.items():
            q = dict(q)
            if isinstance(q.get("criteria"), list):
                q["criteria"] = {c: None for c in q["criteria"]}
            out[k] = q
        return out

    def _score_one(self, event):
        body = json.dumps({"state": event["state"], "questions": self._wire_question(event["question"])}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self._port}/v1/systemone", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
        answers = res.get("answers")
        if not isinstance(answers, dict):
            raise BackendRuntimeError("llama-server answered without 'answers'")
        for name, ans in answers.items():
            probs = ans.get("probabilities") if isinstance(ans, dict) else None
            if not isinstance(probs, dict) or not probs:
                raise BackendRuntimeError(f"llama-server answer {name!r} has no probabilities")
        return {"answers": answers}

    def score(self, events):
        if self._proc is None or self._proc.poll() is not None:
            raise BackendRuntimeError(f"{self.name} backend scored without a running server (call load())")
        out = []
        try:
            step = self.options.batch_size
            for i in range(0, len(events), step):
                out.extend(self._score_one(e) for e in events[i:i + step])
                self.peak_rss = max(self.peak_rss, _rss_bytes(self._proc.pid))
        except BackendRuntimeError:
            raise
        except Exception as e:
            raise BackendRuntimeError(f"{type(e).__name__}: {e}") from e
        return out

    def close(self):
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            self.peak_rss = max(self.peak_rss, _rss_bytes(proc.pid))
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        elif proc is not None:
            proc.wait()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class CpuBackend(LlamaServerBackend):
    """The quantized CPU rung. See LlamaServerBackend."""
    name = "cpu"


# ---- GPU rungs ----------------------------------------------------------------------------------
# One pinned llama-server per rung, offloading every layer (-ngl 99). The binary is NOT taken from PATH
# (a PATH llama-server is usually the CPU build, and a CPU build on a GPU rung would pass the probe and
# report the wrong rung). Lookup: the rung's env var, then <cache>/runtimes/<variant>/llama-server.
# Fetching those runtimes belongs to the shipping work, not to this module. Support tables and the
# supported-generation statement live in laya_gpu.py.
GPU_FP16_QUANTS = {"auto": "laya-f16", "none": "laya-f16", "8bit": "laya-q8_0", "4bit": "laya-q4_k_m"}
GPU_LOW_MEMORY_QUANTS = {"auto": "laya-q8_0", "8bit": "laya-q8_0", "none": "laya-f16", "4bit": "laya-q4_k_m"}
_GPU_OOM_MARKERS = _CPU_OOM_MARKERS + tuple(m for m in laya_gpu.GPU_OOM_MARKERS if m not in _CPU_OOM_MARKERS)


class GpuLlamaBackend(LlamaServerBackend):
    """Base of the GPU rungs. Subclasses set VARIANT, ENV_VAR, VISIBLE_ENV, QUANT_ARTIFACTS, `_scan`
    and `_qualify` (a reason string when a device is below the supported generation, else None).

    probe() runs one vendor tool and checks the binary and artifact exist; it loads nothing. load() takes
    a fresh VRAM reading and raises BackendOOMError when the model cannot fit, and maps an allocation
    failure printed by the runtime to the same error."""
    VARIANT = ""
    ENV_VAR = ""
    VISIBLE_ENV = ""
    SUPPORT = ""                       # one-line statement of the supported generations
    GPU_LAYERS = 99
    OOM_MARKERS = _GPU_OOM_MARKERS
    PROBE_CHECKS_MEMORY = False        # a full card at probe time is a load-time OOM, not 'unavailable'

    def __init__(self, options=None, which=None, runner=None, **kw):
        super().__init__(options, **kw)
        self._which = which or shutil.which
        self._runner = runner or laya_gpu._default_runner
        self.device = None

    def _binary(self):
        exe = "llama-server.exe" if sys.platform == "win32" else "llama-server"
        for c in (self._server_bin, os.environ.get(self.ENV_VAR),
                  os.path.join(self.cache_root, "runtimes", self.VARIANT, exe)):
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    def _scan(self):
        raise NotImplementedError

    def _qualify(self, dev):
        raise NotImplementedError

    def _pick(self):
        """(device, reason). The qualifying device with the most free VRAM, or the reason there is none."""
        scan = self._scan()
        if not scan.devices:
            return None, f"{scan.reason or 'no device'}; {self.SUPPORT}"
        good, why = [], []
        for d in scan.devices:
            r = self._qualify(d)
            (why if r else good).append(r or d)
        if not good:
            return None, f"{'; '.join(why)}; {self.SUPPORT}"
        return max(good, key=lambda d: d.vram_free_mib or 0), ""

    def _probe_device(self):
        self.device, reason = self._pick()
        return Probe(False, reason) if self.device is None else None

    def load(self):
        t0 = time.monotonic()
        self.device, reason = self._pick()          # fresh VRAM reading
        if self.device is None:
            raise BackendUnavailable(reason)
        res = super().load()
        return LoadResult(time.monotonic() - t0, res.detail)

    def _memory_shortfall(self, entry):
        dev = self.device
        if dev is None or dev.vram_free_mib is None:
            return None                              # unknown VRAM: let the runtime decide
        need = self._need_bytes(entry)
        if dev.vram_free_mib * 1024 * 1024 < need:
            return (f"{entry['name']} needs about {need >> 20} MiB of VRAM, {dev.vram_free_mib} MiB free "
                    f"on {dev.name}")
        return None

    def _server_env(self):
        env = dict(os.environ)
        if self.VISIBLE_ENV and self.device is not None:
            env[self.VISIBLE_ENV] = str(self.device.index)
        return env

    def _device_detail(self):
        d = self.device
        return f"device={d.name!r} " if d else ""


class CudaBackend(GpuLlamaBackend):
    """Modern NVIDIA: compute capability 7.5 and newer, the CUDA 13.4 build, FP16 artifact by default.
    NOT HARDWARE-VERIFIED."""
    name = "cuda"
    VARIANT = "cuda"
    ENV_VAR = "LAYA_LLAMA_SERVER_CUDA"
    VISIBLE_ENV = "CUDA_VISIBLE_DEVICES"
    QUANT_ARTIFACTS = GPU_FP16_QUANTS
    MIN_CC = laya_gpu.CUDA_MODERN_MIN_CC
    SUPPORT = (f"supported: NVIDIA compute capability {laya_gpu.cc_str(laya_gpu.CUDA_MODERN_MIN_CC)}+ "
               f"(Turing and newer)")

    def _scan(self):
        return laya_gpu.scan_nvidia(self._which, self._runner)

    def _qualify(self, dev):
        if dev.compute_cap is None:
            return f"{dev.name}: compute capability unreadable (driver too old for nvidia-smi compute_cap)"
        if dev.compute_cap < self.MIN_CC:
            return f"{dev.name}: compute capability {laya_gpu.cc_str(dev.compute_cap)} is below {laya_gpu.cc_str(self.MIN_CC)}"
        return None


class LegacyGpuBackend(CudaBackend):
    """Legacy NVIDIA: compute capability 5.0 to 7.4 (Maxwell, Pascal, Volta) on the CUDA 12.8 build, which
    carries PTX for sm_50 and up. Lower-memory path: the Q8_0 artifact by default (no FP32 GGUF exists;
    the source is FP16, so FP32 would only add memory). A newer card is accepted as a second attempt after
    the modern rung failed. AMD has no legacy rung: GCN and Vega have no prebuilt runtime and fall to
    Vulkan. NOT HARDWARE-VERIFIED."""
    name = "legacy-gpu"
    VARIANT = "cuda-legacy"
    ENV_VAR = "LAYA_LLAMA_SERVER_CUDA_LEGACY"
    QUANT_ARTIFACTS = GPU_LOW_MEMORY_QUANTS
    MIN_CC = laya_gpu.CUDA_LEGACY_MIN_CC
    SUPPORT = (f"supported: NVIDIA compute capability {laya_gpu.cc_str(laya_gpu.CUDA_LEGACY_MIN_CC)}+ "
               f"(Maxwell and newer); AMD GCN/Vega has no prebuilt runtime, use the Vulkan rung")


class RocmBackend(GpuLlamaBackend):
    """AMD ROCm: gfx908, gfx90a, gfx942, gfx950 and gfx1010 (RDNA 1) and newer, FP16 artifact by default.
    GCN and Vega (gfx8xx, gfx900, gfx906) are not supported. NOT HARDWARE-VERIFIED."""
    name = "rocm"
    VARIANT = "rocm"
    ENV_VAR = "LAYA_LLAMA_SERVER_ROCM"
    VISIBLE_ENV = "HIP_VISIBLE_DEVICES"
    QUANT_ARTIFACTS = GPU_FP16_QUANTS
    SUPPORT = "supported: AMD gfx908, gfx90a, gfx942, gfx950 and gfx1010+ (RDNA 1 and newer); GCN/Vega use Vulkan"

    def _scan(self):
        return laya_gpu.scan_rocm(self._which, self._runner)

    def _qualify(self, dev):
        if not laya_gpu.rocm_gfx_supported(dev.gfx):
            return f"{dev.name} ({dev.gfx}) is not in the prebuilt ROCm target list"
        return None


class VulkanBackend(GpuLlamaBackend):
    """Any hardware Vulkan device (software renderers excluded) on Linux or Windows, Q8_0 by default.
    Whether Laya's graph runs on Mesa drivers (RADV, ANV, NVK) is unknown. NOT HARDWARE-VERIFIED."""
    name = "vulkan"
    VARIANT = "vulkan"
    ENV_VAR = "LAYA_LLAMA_SERVER_VULKAN"
    VISIBLE_ENV = "GGML_VK_VISIBLE_DEVICES"
    QUANT_ARTIFACTS = GPU_LOW_MEMORY_QUANTS
    SUPPORT = "supported: a hardware Vulkan device on Linux or Windows (not llvmpipe, lavapipe or SwiftShader)"

    def _scan(self):
        return laya_gpu.scan_vulkan(self._which, self._runner)

    def _qualify(self, dev):
        return None


class CudaRocmBackend(LayaBackend):
    """The 'modern GPU' rung: CUDA when an NVIDIA device qualifies, else ROCm. Delegates everything."""
    name = "cuda/rocm"
    DELEGATES = (CudaBackend, RocmBackend)

    def __init__(self, options=None, **kw):
        super().__init__(options)
        self._kw = kw
        self._delegate = None

    def _choose(self):
        reasons = []
        for cls in self.DELEGATES:
            b = cls(self.options, **self._kw)
            p = b.probe()
            if p.available:
                return b, ""
            reasons.append(f"{cls.name}: {p.reason}")
        return None, " | ".join(reasons)

    def probe(self):
        b, reason = self._choose()
        if b is None:
            return Probe(False, reason)
        self._delegate = b
        return Probe(True)

    def load(self):
        b, reason = self._choose()
        if b is None:
            raise BackendUnavailable(reason)
        self._delegate = b
        res = b.load()
        return LoadResult(res.load_seconds, f"{b.name}: {res.detail}")

    def score(self, events):
        if self._delegate is None:
            raise BackendRuntimeError("cuda/rocm backend scored before load()")
        return self._delegate.score(events)

    def close(self):
        if self._delegate is not None:
            self._delegate.close()


class RemoteBackend(_Unimplemented):
    name = "remote"


# Ladder order, first to last.
RUNGS = [MlxBackend, CudaRocmBackend, LegacyGpuBackend, VulkanBackend, CpuBackend, RemoteBackend]

# Override names (config / env) -> class.
BACKENDS_BY_NAME = {
    "mlx": MlxBackend,
    "cuda": CudaBackend,
    "rocm": RocmBackend,
    "legacy": LegacyGpuBackend,
    "vulkan": VulkanBackend,
    "cpu": CpuBackend,
    "remote": RemoteBackend,
    "stub": StubBackend,
}


def backend_for(name):
    """Class for an override name; KeyError if unknown."""
    return BACKENDS_BY_NAME[str(name).strip().lower()]
