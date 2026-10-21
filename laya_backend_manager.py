"""Backend selection for the Laya gate.

Picks a scoring backend at startup (override, then hardware, then the rung ladder), falls DOWN the
ladder on a load failure or a scoring failure, and records every step in `fallback_chain`. Nothing
falls back silently: each step is logged at WARNING and appended to the chain with its reason.

When no rung is left the manager is model-less. Lanes 1 and 2 of the gate are rule code and never
call the manager; lane 3 gets BackendRuntimeError and fails open toward showing the event.
"""
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import laya_backends as lb

log = logging.getLogger("laya.backend")

ENV_VAR = "LAYA_BACKEND"
VALID_OVERRIDES = ("auto", "mlx", "cuda", "rocm", "legacy", "vulkan", "cpu", "remote", "stub", "none")

# Override name -> the ladder rung it sits on (explicit choices fall down from there, never up).
_OVERRIDE_RUNG = {"mlx": "mlx", "cuda": "cuda/rocm", "rocm": "cuda/rocm", "legacy": "legacy-gpu",
                  "vulkan": "vulkan", "cpu": "cpu", "remote": "remote"}


def read_config_backend(path=None):
    """`laya.backend` from the Hermes config.yaml, or None. Never raises."""
    path = path or os.path.join(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")),
                                "config.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        value = (cfg or {}).get("laya", {}).get("backend")
    except Exception:
        return None
    return str(value) if value is not None else None


def read_config_options(path=None):
    """`laya.options` mapping from the Hermes config.yaml, or {}. Never raises."""
    path = path or os.path.join(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")),
                                "config.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        value = ((cfg or {}).get("laya") or {}).get("options")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def resolve_override(env=None, config_value=None):
    """(value, source, error). Env beats config. An invalid value yields ('auto', source, message)."""
    env = os.environ if env is None else env
    raw, source = None, "default"
    if str(env.get(ENV_VAR, "")).strip():
        raw, source = str(env[ENV_VAR]), "env"
    elif config_value is not None and str(config_value).strip():
        raw, source = str(config_value), "config"
    if raw is None:
        return "auto", source, None
    value = raw.strip().lower()
    if value in VALID_OVERRIDES:
        return value, source, None
    err = (f"invalid laya backend {raw!r} from {source}; valid values: "
           f"{', '.join(VALID_OVERRIDES)}. Using auto.")
    return "auto", source, err


def _skip_reason(rung, hw):
    """Why a rung is clearly pointless on this hardware, or None."""
    if hw is None:
        return None
    if rung == "mlx" and not hw.apple_silicon:
        return f"no Apple Silicon (system={hw.system or '?'}, machine={hw.machine or '?'})"
    if rung in ("cuda/rocm", "cuda", "rocm") and not (hw.nvidia.available or hw.rocm.available):
        return (f"no NVIDIA or ROCm device (nvidia: {hw.nvidia.reason or 'absent'}; "
                f"rocm: {hw.rocm.reason or 'absent'})")
    if rung == "legacy-gpu" and not (hw.nvidia.available or hw.rocm.available or hw.vulkan.available):
        return "no GPU found (nvidia, rocm, vulkan all absent)"
    if rung == "vulkan" and not hw.vulkan.available:
        return f"no Vulkan device ({hw.vulkan.reason or 'absent'})"
    return None


@dataclass
class BackendStatus:
    backend: Optional[str] = None
    fallback_chain: List[dict] = field(default_factory=list)
    load_ms: Optional[int] = None
    model_less: bool = True
    override: str = "auto"
    override_source: str = "default"
    override_error: Optional[str] = None
    hardware_error: Optional[str] = None

    def to_dict(self):
        return asdict(self)


class _Adapter:
    """What the gate calls: `.predict(state, question)`, scored through the manager so demotion applies."""

    def __init__(self, manager):
        self._manager = manager

    def predict(self, state, question):
        return self._manager.score([{"state": state, "question": question}])[0]


class BackendManager:
    def __init__(self, hw=None, detect=None, env=None, config_value=None, rungs=None,
                 backends_by_name=None, model=None, options=None):
        self._hw_given = hw
        self._detect = detect
        self._env = env
        self._config_value = config_value
        self._rungs = list(rungs) if rungs is not None else list(lb.RUNGS)
        self._by_name = backends_by_name if backends_by_name is not None else lb.BACKENDS_BY_NAME
        self.model = model
        self._options = options
        self._lock = threading.RLock()
        self._hw = None
        self._queue = []                 # rung classes not yet tried, in order
        self._active = None
        self._explicit_next = False
        self._status = BackendStatus()
        self._started = False
        self.adapter = _Adapter(self)

    # ---- startup -------------------------------------------------------------------------
    def start(self):
        with self._lock:
            if self._started:
                return self._status
            self._started = True
            st = self._status
            config_value = self._config_value if self._config_value is not None else (
                read_config_backend() if self._env is None else None)
            st.override, st.override_source, st.override_error = resolve_override(self._env, config_value)
            if st.override_error:
                log.error(st.override_error)
            self._hw = self._probe_hardware()
            if st.override == "none":
                log.warning("laya backend override 'none' (from %s): running model-less", st.override_source)
                st.fallback_chain.append({"rung": "none", "status": "skipped",
                                          "reason": f"override 'none' from {st.override_source}"})
                return st
            self._queue = self._plan(st.override)
            self._explicit_next = st.override != "auto"      # only the user's own pick skips the hardware check
            self._advance()
            return st

    def _probe_hardware(self):
        if self._hw_given is not None:
            return self._hw_given
        try:
            detect = self._detect
            if detect is None:
                from laya_hardware import detect_hardware as detect
            return detect()
        except Exception as e:
            self._status.hardware_error = f"{type(e).__name__}: {e}"
            log.warning("laya hardware detection failed (%s); trying every rung", self._status.hardware_error)
            return None

    def _plan(self, override):
        """Rung classes to try, in order. Explicit choices ignore hardware; what falls below them does not."""
        if override == "auto":
            return list(self._rungs)
        explicit = self._by_name[override]
        if override == "stub":
            return [explicit]
        names = [c.name for c in self._rungs]
        pos = names.index(_OVERRIDE_RUNG[override]) if _OVERRIDE_RUNG[override] in names else len(names)
        return [explicit] + self._rungs[pos + 1:]

    def _instantiate(self, cls):
        if self._options is None:
            self._options = lb.BackendOptions.resolve(
                self._env, read_config_options() if self._env is None else None)
        if issubclass(cls, lb.MlxBackend):
            return cls(model=self.model, options=self._options)
        return cls(options=self._options)

    def _advance(self):
        """Load the next viable rung off the queue. Leaves model-less if none loads."""
        st = self._status
        while self._queue:
            cls = self._queue.pop(0)
            name = cls.name
            explicit, self._explicit_next = self._explicit_next, False
            skip = None if explicit else _skip_reason(name, self._hw)
            if skip:
                log.warning("laya backend %s skipped: %s", name, skip)
                st.fallback_chain.append({"rung": name, "status": "skipped", "reason": skip})
                continue
            t0 = time.monotonic()
            backend = self._instantiate(cls)
            probe = backend.probe()
            if not probe.available:
                reason = f"unavailable: {probe.reason or 'probe failed'}"
                log.warning("laya backend %s %s; not loading", name, reason)
                st.fallback_chain.append({"rung": name, "status": "skipped", "reason": reason})
                continue
            try:
                result = backend.load()
            except lb.BackendLoadError as e:        # OOM, unavailable, bad checkpoint: next rung
                if isinstance(e, lb.BackendOOMError):
                    kind = "out of memory"
                elif isinstance(e, lb.BackendUnavailable):
                    kind = "unavailable"
                else:
                    kind = "load failed"
                reason = f"{kind}: {type(e).__name__}: {e}"
                log.warning("laya backend %s %s; falling down the ladder", name, reason)
                st.fallback_chain.append({"rung": name, "status": "failed", "reason": reason})
                continue
            self._active = backend
            st.backend = name
            st.model_less = False
            seconds = getattr(result, "load_seconds", None)
            if seconds is None:
                seconds = time.monotonic() - t0
            st.load_ms = int(seconds * 1000)
            log.info("laya backend %s loaded in %.3fs", name, seconds)
            st.fallback_chain.append({"rung": name, "status": "loaded", "reason": ""})
            return True
        self._active = None
        st.backend = None
        st.model_less = True
        st.load_ms = None
        log.warning("laya has no usable backend: model-less mode (lanes 1 and 2 still run by rule)")
        return False

    # ---- scoring -------------------------------------------------------------------------
    def score(self, events):
        with self._lock:
            if not self._started:
                self.start()
            while True:
                if self._active is None:
                    raise lb.BackendRuntimeError("laya is model-less: no backend loaded")
                try:
                    return self._active.score(events)
                except Exception as e:
                    name = self._status.backend
                    reason = f"score failed: {type(e).__name__}: {e}"
                    log.warning("laya backend %s demoted: %s", name, reason)
                    self._status.fallback_chain.append({"rung": name, "status": "demoted", "reason": reason})
                    self._advance()

    def status(self):
        with self._lock:
            return self._status
