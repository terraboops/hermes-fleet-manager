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
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, fields

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


class CudaBackend(_Unimplemented):
    name = "cuda"


class RocmBackend(_Unimplemented):
    name = "rocm"


class LegacyGpuBackend(_Unimplemented):
    name = "legacy-gpu"


class VulkanBackend(_Unimplemented):
    name = "vulkan"


class CpuBackend(_Unimplemented):
    name = "cpu"


class RemoteBackend(_Unimplemented):
    name = "remote"


class CudaRocmBackend(_Unimplemented):
    """The cuda/rocm rung. Resolves to CUDA or ROCm once implemented."""
    name = "cuda/rocm"


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
