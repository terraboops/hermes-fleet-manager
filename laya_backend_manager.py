"""Backend selection for the Laya gate.

Picks a scoring backend at startup (override, then hardware, then the rung ladder), falls DOWN the
ladder on a load failure or a scoring failure, and records every step in `fallback_chain`. Nothing
falls back silently: each step is logged at WARNING and appended to the chain with its reason.

A remote rung (laya_remote_backend.RemoteBackend) can sit at any position in the ladder. It is
strictly opt-in: with no enabled remote config it is never constructed and nothing touches the
network. Unlike local rungs, a failed remote rung is not demoted for good: it is skipped for a
cooldown (REMOTE_COOLDOWN_S) and tried again, except after a latching failure (auth, pin, protocol)
which waits for a restart. /health shows the active backend and the latest reason for every skipped
rung (`BackendStatus.skipped`). See docs/laya-remote.md, "Ladder configuration".

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
import laya_remote_schema as rs

log = logging.getLogger("laya.backend")

ENV_VAR = "LAYA_BACKEND"
REMOTE_COOLDOWN_S = 60.0         # matches the contract's breaker: a failed remote rung sits out this long
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


def read_config_laya(path=None):
    """The `laya:` mapping from the Hermes config.yaml, or {}. Never raises."""
    path = path or os.path.join(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")),
                                "config.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        value = (cfg or {}).get("laya")
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
    active_label: Optional[str] = None           # "remote http://host:port" for a remote rung
    ladder: List[str] = field(default_factory=list)
    ladder_source: str = "default"
    ladder_error: Optional[str] = None
    skipped: List[dict] = field(default_factory=list)    # latest state of every rung that is not serving
    notices: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class _Adapter:
    """What the gate calls: `.predict(state, question)`, scored through the manager so demotion applies."""

    def __init__(self, manager):
        self._manager = manager

    def predict(self, state, question):
        return self._manager.score([{"state": state, "question": question}])[0]


class _RemoteRung:
    """A remote entry on the ladder: a resolved RemoteConfig, nothing built, nothing connected."""
    name = "remote"

    def __init__(self, cfg, present):
        self.cfg = cfg
        self.present = present          # some remote config or env was given (even if not enabled)
        self.label = "remote " + display_url(cfg.url) if cfg.url else "remote"


def display_url(url):
    """scheme://host:port only: no path, query or userinfo ever reach /health."""
    try:
        import urllib.parse
        parts = urllib.parse.urlsplit(str(url))
        host = parts.hostname or "?"
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except Exception:
        return "?"


def remote_failure_text(exc, total_s=None):
    """Short, secret-free text for a remote failure. Never carries the token or server-sent text."""
    cause = getattr(exc, "remote_error", None) or exc
    if isinstance(cause, rs_errors()["auth"]):
        status = getattr(cause, "status", None)
        return f"auth failed ({status})" if status else f"auth failed: {cause.reason}"
    if isinstance(cause, rs_errors()["timeout"]):
        return f"timeout after {total_s:g}s" if total_s else "timeout"
    reason = getattr(cause, "reason", None) or str(cause)
    return reason[len("remote "):] if reason.startswith("remote ") else reason


def rs_errors():
    import laya_remote_backend as rb
    return {"auth": rb.RemoteAuthError, "timeout": rb.RemoteTimeout, "base": rb.RemoteError}


class BackendManager:
    def __init__(self, hw=None, detect=None, env=None, config_value=None, rungs=None,
                 backends_by_name=None, model=None, options=None, ladder=None, remote_config=None,
                 cooldown_s=REMOTE_COOLDOWN_S, clock=time.monotonic):
        self._hw_given = hw
        self._detect = detect
        self._env = env
        self._config_value = config_value
        self._rungs = list(rungs) if rungs is not None else list(lb.RUNGS)
        self._by_name = backends_by_name if backends_by_name is not None else lb.BACKENDS_BY_NAME
        self._ladder_given = ladder
        self._remote_given = remote_config
        self._cooldown_s = cooldown_s
        self._clock = clock
        self.model = model
        self._options = options
        self._lock = threading.RLock()
        self._hw = None
        self._entries = []               # the planned ladder: rung classes and _RemoteRung, in order
        self._queue = []                 # entry indexes not yet tried, in order
        self._active = None
        self._active_idx = None
        self._parked = {}                # idx -> loaded backend that a recovered remote rung displaced
        self._latest = {}                # idx -> latest {rung, status, reason} for a rung that is not serving
        self._down_until = {}            # idx -> monotonic time a failed remote rung may be tried again
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
            laya_cfg = {}
            if self._env is None and (self._config_value is None or self._ladder_given is None
                                      or self._remote_given is None):
                laya_cfg = read_config_laya()
            config_value = self._config_value if self._config_value is not None else (
                (str(laya_cfg["backend"]) if laya_cfg.get("backend") is not None else None)
                if self._env is None else None)
            st.override, st.override_source, st.override_error = resolve_override(self._env, config_value)
            if st.override_error:
                log.error(st.override_error)
            self._hw = self._probe_hardware()
            if st.override == "none":
                log.warning("laya backend override 'none' (from %s): running model-less", st.override_source)
                st.fallback_chain.append({"rung": "none", "status": "skipped",
                                          "reason": f"override 'none' from {st.override_source}"})
                return st
            self._entries = self._plan(st.override, laya_cfg)
            st.ladder = [e.label if isinstance(e, _RemoteRung) else e.name for e in self._entries]
            self._queue = list(range(len(self._entries)))
            self._explicit_next = st.override != "auto"      # only the user's own pick skips the hardware check
            self._advance()
            return self.status()

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

    # ---- the ladder ----------------------------------------------------------------------
    def _configured_rungs(self, laya_cfg):
        """The ladder to walk: `laya.ladder` (or the ladder argument) when given, else the built-in rungs.

        Entries are rung names ("mlx", "cuda/rocm", "cpu", "remote", ...) or a mapping
        {"remote": {url: ..., ack: ...}} for a remote with its own settings. Names may repeat only for
        remote. A bad entry is dropped and reported in `ladder_error`; no valid entry means the default."""
        st = self._status
        raw = self._ladder_given if self._ladder_given is not None else laya_cfg.get("ladder")
        if raw is None:
            return [("remote", None) if c is lb.RemoteBackend else c for c in self._rungs], None
        if self._ladder_given is not None:
            st.ladder_source = "argument"
        else:
            st.ladder_source = "config"
        if not isinstance(raw, (list, tuple)) or not raw:
            st.ladder_error = "laya.ladder must be a non-empty list; using the default ladder"
            log.error(st.ladder_error)
            st.ladder_source = "default"
            return [("remote", None) if c is lb.RemoteBackend else c for c in self._rungs], None
        by_rung = {c.name: c for c in self._rungs}
        out, errors = [], []
        for item in raw:
            if isinstance(item, dict) and len(item) == 1 and "remote" in item and isinstance(item["remote"], dict):
                out.append(("remote", item["remote"]))
                continue
            name = str(item).strip().lower() if isinstance(item, str) else None
            cls = by_rung.get(name) or (self._by_name.get(name) if name else None)
            if cls is None or name in ("stub", "none"):
                errors.append(f"unknown ladder entry {item!r}")
                continue
            if cls is lb.RemoteBackend:
                out.append(("remote", None))
            elif any(c is cls for c in out):
                errors.append(f"duplicate ladder entry {name!r}")
            else:
                out.append(cls)
        if errors:
            st.ladder_error = "; ".join(errors)
            log.error("laya.ladder: %s", st.ladder_error)
        if not out:
            st.ladder_source = "default"
            return [("remote", None) if c is lb.RemoteBackend else c for c in self._rungs], None
        return out, None

    def _remote_rung(self, entry_cfg, laya_cfg):
        """Resolve one remote entry. The result is a _RemoteRung whose cfg.enabled says whether it may run."""
        env = os.environ if self._env is None else self._env
        base = self._remote_given if self._remote_given is not None else laya_cfg.get("remote")
        base = base if isinstance(base, dict) else {}
        if entry_cfg is None:                       # bare "remote": the `remote:` block plus env
            cfg = rs.RemoteConfig.resolve(env, base)
            present = bool(base) or any(str(env.get(v, "")).strip() for v in rs.ENV_VARS.values())
            return _RemoteRung(cfg, present)
        merged = {**base, **entry_cfg}              # an entry has its own url; only the backend switch is global
        only_switch = {rs.ENV_VARS["backend"]: env.get(rs.ENV_VARS["backend"])}
        cfg = rs.RemoteConfig.resolve({k: v for k, v in only_switch.items() if v is not None}, merged)
        return _RemoteRung(cfg, True)

    def _plan(self, override, laya_cfg):
        """Entries to try, in order. Explicit choices ignore hardware; what falls below them does not."""
        base, _ = self._configured_rungs(laya_cfg)
        entries = [self._remote_rung(e[1], laya_cfg) if isinstance(e, tuple) else e for e in base]
        names = [e.name for e in entries]
        if override == "auto":
            pass
        elif override == "stub":
            entries = [self._by_name["stub"]]
        else:
            explicit = self._by_name[override]
            if explicit is lb.RemoteBackend:
                explicit = next((e for e in entries if isinstance(e, _RemoteRung)), None) \
                    or self._remote_rung(None, laya_cfg)
            at = _OVERRIDE_RUNG[override]
            pos = names.index(at) if at in names else len(names)
            entries = [explicit] + entries[pos + 1:]
        for e in entries:
            if isinstance(e, _RemoteRung) and not e.cfg.enabled and e.present:
                why = "; ".join(e.cfg.problems) or "not enabled"
                notice = f"remote is configured but not enabled ({why}); staying local-only"
                self._status.notices.append(notice)
                log.warning("laya: %s", notice)
        return entries

    def _instantiate(self, cls):
        if self._options is None:
            self._options = lb.BackendOptions.resolve(
                self._env, read_config_options() if self._env is None else None)
        if issubclass(cls, lb.MlxBackend):
            return cls(model=self.model, options=self._options)
        return cls(options=self._options)

    # ---- loading -------------------------------------------------------------------------
    def _record(self, idx, rung, status, reason, chain=True):
        entry = {"rung": rung, "status": status, "reason": reason}
        if chain:
            self._status.fallback_chain.append(dict(entry))
        if status == "loaded":
            self._latest.pop(idx, None)
        else:
            self._latest[idx] = entry

    def _try(self, idx, chain=True):
        """Try to bring entry `idx` up. True when it is now the active backend."""
        st = self._status
        entry = self._entries[idx]
        if isinstance(entry, _RemoteRung):
            return self._try_remote(idx, entry, chain)
        cls = entry
        name = cls.name
        explicit, self._explicit_next = self._explicit_next, False
        skip = None if explicit else _skip_reason(name, self._hw)
        if skip:
            log.warning("laya backend %s skipped: %s", name, skip)
            self._record(idx, name, "skipped", skip)
            return False
        parked = self._parked.pop(idx, None)
        if parked is not None:
            self._activate(idx, name, parked, name, 0.0)
            self._record(idx, name, "loaded", "reused after remote recovered", chain)
            return True
        t0 = time.monotonic()
        backend = self._instantiate(cls)
        probe = backend.probe()
        if not probe.available:
            reason = f"unavailable: {probe.reason or 'probe failed'}"
            log.warning("laya backend %s %s; not loading", name, reason)
            self._record(idx, name, "skipped", reason)
            return False
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
            self._record(idx, name, "failed", reason)
            return False
        seconds = getattr(result, "load_seconds", None)
        if seconds is None:
            seconds = time.monotonic() - t0
        self._activate(idx, name, backend, name, seconds)
        self._record(idx, name, "loaded", "")
        return True

    def _try_remote(self, idx, rung, chain):
        """Opt-in gate, then build and load. Nothing is constructed or contacted unless cfg.enabled."""
        cfg = rung.cfg
        if not cfg.enabled:
            reason = ("remote is not enabled: " + "; ".join(cfg.problems)) if cfg.problems \
                else "remote is not enabled (local-only)"
            log.log(logging.WARNING if rung.present else logging.INFO, "laya backend %s", reason)
            self._record(idx, "remote", "skipped", reason, chain)
            return False
        import laya_remote_backend as rb
        t0 = time.monotonic()
        try:
            backend = rb.RemoteBackend.from_config(cfg, "decide", options=self._options)
            result = backend.load()
        except rb.RemoteError as e:
            return self._remote_down(idx, rung, e, "unavailable", chain, cfg)
        except lb.BackendLoadError as e:
            return self._remote_down(idx, rung, e, "unavailable", chain, cfg)
        except ValueError:                      # bad url shape: a config fault, no retry
            self._record(idx, "remote", "skipped", f"{rung.label}: invalid remote config", chain)
            self._down_until[idx] = float("inf")
            return False
        seconds = getattr(result, "load_seconds", None) or (time.monotonic() - t0)
        self._activate(idx, "remote", backend, rung.label, seconds)
        self._down_until.pop(idx, None)
        self._record(idx, "remote", "loaded", "", chain)
        return True

    def _remote_down(self, idx, rung, exc, status, chain, cfg):
        text = remote_failure_text(exc, cfg.total_timeout_for("decide"))
        reason = f"{rung.label}: {text}"
        cause = getattr(exc, "remote_error", None) or exc
        latch = bool(getattr(cause, "latch", False))
        self._down_until[idx] = float("inf") if latch else self._clock() + self._cooldown_s
        if chain:
            log.warning("laya backend %s; falling down the ladder%s", reason,
                        " (latched until restart)" if latch else "")
        self._record(idx, "remote", "failed" if status == "unavailable" else status, reason, chain)
        return False

    def _activate(self, idx, name, backend, label, seconds):
        st = self._status
        self._active, self._active_idx = backend, idx
        st.backend, st.active_label = name, label
        st.model_less = False
        st.load_ms = int(seconds * 1000)
        log.info("laya backend %s loaded in %.3fs", label, seconds)

    def _advance(self):
        """Load the next viable rung off the queue. Leaves model-less if none loads."""
        st = self._status
        while self._queue:
            if self._try(self._queue.pop(0)):
                return True
        self._active, self._active_idx = None, None
        st.backend = st.active_label = None
        st.model_less = True
        st.load_ms = None
        log.warning("laya has no usable backend: model-less mode (lanes 1 and 2 still run by rule)")
        return False

    def _retry_cooled(self):
        """Remote rungs ranked above the active one get another try once their cooldown has passed."""
        now = self._clock()
        for idx in sorted(self._down_until):
            if self._down_until[idx] > now:
                continue
            if self._active_idx is not None and idx > self._active_idx:
                continue
            prev, prev_idx = self._active, self._active_idx
            if self._try(idx, chain=False):
                if prev is not None:
                    self._parked[prev_idx] = prev
                self._queue = list(range(idx + 1, len(self._entries)))
                return
            # a failed retry re-armed its cooldown inside _try; the chain stays as it was

    # ---- scoring -------------------------------------------------------------------------
    def score(self, events):
        with self._lock:
            if not self._started:
                self.start()
            self._retry_cooled()
            while True:
                if self._active is None:
                    raise lb.BackendRuntimeError("laya is model-less: no backend loaded")
                idx, entry = self._active_idx, self._entries[self._active_idx]
                try:
                    return self._active.score(events)
                except Exception as e:
                    name = self._status.backend
                    if isinstance(entry, _RemoteRung):
                        text = remote_failure_text(e, entry.cfg.total_timeout_for("decide"))
                        reason = f"{entry.label}: {text}"
                        latch = bool(getattr(e, "latch", False))
                        self._down_until[idx] = float("inf") if latch else self._clock() + self._cooldown_s
                    else:
                        reason = f"score failed: {type(e).__name__}: {e}"
                    log.warning("laya backend %s demoted: %s", name, reason)
                    self._record(idx, name, "demoted", reason)
                    self._advance()

    def status(self):
        with self._lock:
            st = self._status
            now = self._clock()
            skipped = []
            for idx in sorted(self._latest):
                if idx == self._active_idx:
                    continue
                item = dict(self._latest[idx])
                until = self._down_until.get(idx)
                if until is not None and until != float("inf"):
                    item["retry_in_s"] = max(0, int(until - now))
                elif until == float("inf"):
                    item["retry_in_s"] = None            # latched or misconfigured: waits for a restart
                skipped.append(item)
            st.skipped = skipped
            return st
