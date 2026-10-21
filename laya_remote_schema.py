"""Wire contract for the opt-in remote Laya server (`laya serve`) and its client (RemoteBackend).

Shared by both sides so they cannot drift. Pure data and validation: no network, no I/O.
Contract text: docs/laya-remote.md. Design source: docs/laya-runtime-decision.md section 7.

Rules enforced here:
  * Requests reject unknown fields, so a client can never send data the contract does not name.
  * Responses are parsed strictly: a missing criterion, probabilities that do not sum to 1, a
    wrong request_id or a wrong model pin raises SchemaError. A response is never a score
    unless it validates (a missing key must never read as 0.0 = "stay silent").
  * Responses ignore unknown fields (additive changes within v1).
  * Error messages are fixed text and never echo the request.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL = 1
API_PREFIX = "/v1"
PATH_HEALTHZ = "/healthz"
PATH_HEALTH = "/health"
PATH_INFO = "/v1/info"
PATH_SCORE = "/v1/score"

MAX_ITEMS = 16
MAX_BODY_BYTES = 65536
MAX_TOKENS = 512
SUM_TOLERANCE = 1e-6

AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"
TOKEN_PATTERN = re.compile(r"^laya_[A-Za-z0-9]{4,16}_[A-Za-z0-9_-]{32,}$")

QUESTION_TYPES = ("choice",)
STATUS_OK, STATUS_LOADING, STATUS_DEGRADED, STATUS_DOWN = "ok", "loading", "degraded", "unavailable"
HEALTH_STATUSES = (STATUS_OK, STATUS_LOADING, STATUS_DEGRADED, STATUS_DOWN)


class SchemaError(ValueError):
    """A body does not match the contract. The message names the field, never its value."""


# ---------------------------------------------------------------- errors

@dataclass(frozen=True)
class ErrorSpec:
    http: int
    code: str
    message: str            # fixed text; never echoes the request
    retryable: bool = False


ERRORS: Dict[str, ErrorSpec] = {e.code: e for e in (
    ErrorSpec(400, "bad_request", "The request is not valid."),
    ErrorSpec(401, "unauthorized", "Authentication failed."),
    ErrorSpec(403, "forbidden", "This token may not do that."),
    ErrorSpec(404, "not_found", "Unknown path."),
    ErrorSpec(409, "revision_mismatch", "The server model does not match the expected model."),
    ErrorSpec(413, "too_large", "The request is too large."),
    ErrorSpec(415, "unsupported_media_type", "Content type must be application/json."),
    ErrorSpec(422, "invalid_question", "The question or request fields are not valid."),
    ErrorSpec(426, "protocol_unsupported", "Protocol version not served."),
    ErrorSpec(429, "rate_limited", "Too many requests.", True),
    ErrorSpec(500, "internal", "Server fault.", True),
    ErrorSpec(503, "model_loading", "The model is not ready.", True),
    ErrorSpec(503, "overloaded", "The server is overloaded.", True),
    ErrorSpec(503, "backend_unavailable", "No scoring backend is available.", True),
)}


def error_body(code: str) -> dict:
    spec = ERRORS[code]
    return {"error": {"code": spec.code, "message": spec.message, "retryable": spec.retryable}}


def parse_error(http_status: int, body: Any) -> "ErrorSpec":
    """Client side: map a non-2xx body to an ErrorSpec. Unknown shapes become a generic spec for
    the HTTP status; the body text is never carried through."""
    code = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        c = body["error"].get("code")
        code = c if isinstance(c, str) else None
    spec = ERRORS.get(code) if code else None
    if spec is not None and spec.http == http_status:
        return spec
    for s in ERRORS.values():
        if s.http == http_status:
            return s
    return ErrorSpec(http_status, "internal" if http_status >= 500 else "bad_request",
                     "Unrecognised error.", http_status >= 500)


def is_auth_failure(http_status: int) -> bool:
    return http_status in (401, 403)


# ---------------------------------------------------------------- helpers

def _req(d: dict, key: str, typ, where: str):
    if not isinstance(d, dict):
        raise SchemaError(f"{where}: not an object")
    if key not in d:
        raise SchemaError(f"{where}.{key}: missing")
    v = d[key]
    if isinstance(v, bool) and typ is not bool:
        raise SchemaError(f"{where}.{key}: wrong type")
    if not isinstance(v, typ):
        raise SchemaError(f"{where}.{key}: wrong type")
    return v


def _no_extra(d: dict, allowed, where: str):
    extra = set(d) - set(allowed)
    if extra:
        raise SchemaError(f"{where}: unknown field")


def new_request_id() -> str:
    """Random, never derived from event data."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------- score request

@dataclass(frozen=True)
class Question:
    type: str
    instructions: str
    criteria: tuple

    def to_dict(self) -> dict:
        return {"type": self.type, "instructions": self.instructions, "criteria": list(self.criteria)}

    @classmethod
    def from_dict(cls, d: Any, where: str = "question") -> "Question":
        if not isinstance(d, dict):
            raise SchemaError(f"{where}: not an object")
        _no_extra(d, ("type", "instructions", "criteria"), where)
        t = _req(d, "type", str, where)
        if t not in QUESTION_TYPES:
            raise SchemaError(f"{where}.type: unknown")
        ins = _req(d, "instructions", str, where)
        crit = _req(d, "criteria", list, where)
        if not crit or not all(isinstance(c, str) and c for c in crit) or len(set(crit)) != len(crit):
            raise SchemaError(f"{where}.criteria: must be non-empty, unique strings")
        return cls(t, ins, tuple(crit))

    @classmethod
    def from_gate(cls, gate_question: dict) -> "Question":
        """The gate's QUESTION constants wrap the body under an 'action' key; the wire omits it."""
        return cls.from_dict(gate_question["action"])


@dataclass(frozen=True)
class ScoreItem:
    id: str
    state: str              # OPERATOR EVENT TEXT (redacted before it reaches here)
    question: Question

    def to_dict(self) -> dict:
        return {"id": self.id, "state": self.state, "question": self.question.to_dict()}

    @classmethod
    def from_dict(cls, d: Any, where: str = "items[]") -> "ScoreItem":
        if not isinstance(d, dict):
            raise SchemaError(f"{where}: not an object")
        _no_extra(d, ("id", "state", "question"), where)
        return cls(_req(d, "id", str, where), _req(d, "state", str, where),
                   Question.from_dict(_req(d, "question", dict, where), f"{where}.question"))


@dataclass(frozen=True)
class ExpectPin:
    model_revision: str
    weights_sha256: str

    def to_dict(self) -> dict:
        return {"model_revision": self.model_revision, "weights_sha256": self.weights_sha256}

    @classmethod
    def from_dict(cls, d: Any) -> "ExpectPin":
        if not isinstance(d, dict):
            raise SchemaError("expect: not an object")
        _no_extra(d, ("model_revision", "weights_sha256"), "expect")
        return cls(_req(d, "model_revision", str, "expect"), _req(d, "weights_sha256", str, "expect"))


@dataclass(frozen=True)
class ScoreRequest:
    items: tuple
    expect: ExpectPin
    request_id: str = field(default_factory=new_request_id)
    protocol: int = PROTOCOL

    def to_dict(self) -> dict:
        return {"protocol": self.protocol, "request_id": self.request_id,
                "expect": self.expect.to_dict(), "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, d: Any) -> "ScoreRequest":
        if not isinstance(d, dict):
            raise SchemaError("request: not an object")
        _no_extra(d, ("protocol", "request_id", "expect", "items"), "request")
        proto = _req(d, "protocol", int, "request")
        rid = _req(d, "request_id", str, "request")
        items = _req(d, "items", list, "request")
        if not items:
            raise SchemaError("request.items: empty")
        if len(items) > MAX_ITEMS:
            raise SchemaError("request.items: too many")
        parsed = tuple(ScoreItem.from_dict(i) for i in items)
        if len({i.id for i in parsed}) != len(parsed):
            raise SchemaError("request.items: duplicate id")
        return cls(parsed, ExpectPin.from_dict(_req(d, "expect", dict, "request")), rid, proto)


# ---------------------------------------------------------------- score response

@dataclass(frozen=True)
class ScoreResultItem:
    id: str
    probabilities: Dict[str, float]
    tokens: int
    truncated: bool
    ms: float

    def to_dict(self) -> dict:
        return {"id": self.id, "probabilities": dict(self.probabilities), "tokens": self.tokens,
                "truncated": self.truncated, "ms": self.ms}

    @classmethod
    def from_dict(cls, d: Any) -> "ScoreResultItem":
        w = "response.items[]"
        probs = _req(d, "probabilities", dict, w)
        clean = {}
        for k, v in probs.items():
            if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)):
                raise SchemaError(f"{w}.probabilities: bad entry")
            if not 0.0 <= float(v) <= 1.0:
                raise SchemaError(f"{w}.probabilities: out of range")
            clean[k] = float(v)
        ms = _req(d, "ms", (int, float), w)
        return cls(_req(d, "id", str, w), clean, _req(d, "tokens", int, w),
                   _req(d, "truncated", bool, w), float(ms))


@dataclass(frozen=True)
class ScoreResponse:
    request_id: str
    model_revision: str
    weights_sha256: str
    runtime: str
    server_ms: float
    queue_ms: float
    items: tuple
    protocol: int = PROTOCOL

    def to_dict(self) -> dict:
        return {"protocol": self.protocol, "request_id": self.request_id,
                "model_revision": self.model_revision, "weights_sha256": self.weights_sha256,
                "runtime": self.runtime, "server_ms": self.server_ms, "queue_ms": self.queue_ms,
                "items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, d: Any) -> "ScoreResponse":
        """Lenient about unknown fields, strict about the ones that carry the score."""
        if not isinstance(d, dict):
            raise SchemaError("response: not an object")
        items = _req(d, "items", list, "response")
        return cls(_req(d, "request_id", str, "response"), _req(d, "model_revision", str, "response"),
                   _req(d, "weights_sha256", str, "response"), _req(d, "runtime", str, "response"),
                   float(_req(d, "server_ms", (int, float), "response")),
                   float(_req(d, "queue_ms", (int, float), "response")),
                   tuple(ScoreResultItem.from_dict(i) for i in items),
                   _req(d, "protocol", int, "response"))

    def validate_against(self, request: ScoreRequest) -> "ScoreResponse":
        """Client-side check. Any failure means the 200 is a failure, never a score."""
        if self.protocol != request.protocol:
            raise SchemaError("response.protocol: mismatch")
        if self.request_id != request.request_id:
            raise SchemaError("response.request_id: mismatch")
        if (self.model_revision, self.weights_sha256) != (request.expect.model_revision,
                                                          request.expect.weights_sha256):
            raise SchemaError("response.model pin: mismatch")
        if [i.id for i in self.items] != [i.id for i in request.items]:
            raise SchemaError("response.items: ids differ from request")
        for sent, got in zip(request.items, self.items):
            if set(got.probabilities) != set(sent.question.criteria):
                raise SchemaError("response.probabilities: criteria differ from question")
            if abs(sum(got.probabilities.values()) - 1.0) > SUM_TOLERANCE:
                raise SchemaError("response.probabilities: do not sum to 1")
        return self


# ---------------------------------------------------------------- health and info

@dataclass(frozen=True)
class Healthz:
    """GET /healthz, no auth. Reveals nothing but liveness."""
    ok: bool
    model_loaded: bool
    protocol: int = PROTOCOL

    def to_dict(self) -> dict:
        return {"ok": self.ok, "model_loaded": self.model_loaded, "protocol": self.protocol}

    @classmethod
    def from_dict(cls, d: Any) -> "Healthz":
        return cls(_req(d, "ok", bool, "healthz"), _req(d, "model_loaded", bool, "healthz"),
                   _req(d, "protocol", int, "healthz"))


@dataclass(frozen=True)
class Health:
    """GET /health, bearer auth. Reports which backend serves scores.

    backend: name of the LayaBackend in use (e.g. "mlx", "cuda"), or None when model-less.
    status:  one of HEALTH_STATUSES. "ok" only when a backend is loaded and usable.
    """
    status: str
    backend: Optional[str]
    version: str
    protocol: int = PROTOCOL
    model_loaded: bool = False
    fallback_chain: tuple = ()

    def __post_init__(self):
        if self.status not in HEALTH_STATUSES:
            raise SchemaError("health.status: unknown")

    def to_dict(self) -> dict:
        return {"status": self.status, "backend": self.backend, "version": self.version,
                "protocol": self.protocol, "model_loaded": self.model_loaded,
                "fallback_chain": list(self.fallback_chain)}

    @classmethod
    def from_dict(cls, d: Any) -> "Health":
        b = d.get("backend") if isinstance(d, dict) else None
        if b is not None and not isinstance(b, str):
            raise SchemaError("health.backend: wrong type")
        chain = _req(d, "fallback_chain", list, "health")
        if not all(isinstance(c, str) for c in chain):
            raise SchemaError("health.fallback_chain: wrong type")
        if "backend" not in d:
            raise SchemaError("health.backend: missing")
        return cls(_req(d, "status", str, "health"), b, _req(d, "version", str, "health"),
                   _req(d, "protocol", int, "health"), _req(d, "model_loaded", bool, "health"),
                   tuple(chain))


@dataclass(frozen=True)
class Info:
    """GET /v1/info, bearer auth. Model identity the client pins against."""
    model: str
    model_revision: str
    weights_sha256: str
    runtime: str
    rung: str
    dtype: str
    temperature_digest: str
    golden_digest: str
    max_items: int = MAX_ITEMS
    max_body_bytes: int = MAX_BODY_BYTES
    max_tokens: int = MAX_TOKENS
    protocol: int = PROTOCOL

    def to_dict(self) -> dict:
        return {"protocol": self.protocol, "model": self.model, "model_revision": self.model_revision,
                "weights_sha256": self.weights_sha256, "runtime": self.runtime, "rung": self.rung,
                "dtype": self.dtype, "temperature_digest": self.temperature_digest,
                "golden_digest": self.golden_digest,
                "limits": {"max_items": self.max_items, "max_body_bytes": self.max_body_bytes,
                           "max_tokens": self.max_tokens}}

    @classmethod
    def from_dict(cls, d: Any) -> "Info":
        lim = _req(d, "limits", dict, "info")
        s = lambda k: _req(d, k, str, "info")
        return cls(s("model"), s("model_revision"), s("weights_sha256"), s("runtime"), s("rung"),
                   s("dtype"), s("temperature_digest"), s("golden_digest"),
                   _req(lim, "max_items", int, "info.limits"),
                   _req(lim, "max_body_bytes", int, "info.limits"),
                   _req(lim, "max_tokens", int, "info.limits"),
                   _req(d, "protocol", int, "info"))


# ---------------------------------------------------------------- auth

def parse_bearer(header_value: Optional[str]) -> Optional[str]:
    """Token from an 'Authorization: Bearer <token>' value, or None if absent or malformed."""
    if not header_value:
        return None
    parts = header_value.split(" ", 1)
    if len(parts) != 2 or parts[0] != AUTH_SCHEME or not parts[1].strip():
        return None
    return parts[1].strip()


def auth_header(token: str) -> Dict[str, str]:
    return {AUTH_HEADER: f"{AUTH_SCHEME} {token}"}


# ---------------------------------------------------------------- opt-in config

DEFAULT_CONNECT_TIMEOUT_S = 0.5
DEFAULT_TOTAL_TIMEOUT_S = {"decide": 1.5, "respond": 2.0, "affirm": 2.0}
DEFAULT_REDACT = "R2"
REDACT_LEVELS = ("R1", "R2", "R3")           # R0 is local-only and is never valid for remote
QUESTION_NAMES = ("decide", "respond", "affirm")
DEFAULT_TOKEN_FILE = "~/.hermes/secrets/laya-remote.token"
DEFAULT_FALLBACK = "local-then-standing"
ENV_VARS = {
    "backend": "LAYA_GATE_BACKEND", "url": "LAYA_GATE_REMOTE_URL",
    "token_file": "LAYA_GATE_REMOTE_TOKEN_FILE", "pin": "LAYA_GATE_REMOTE_PIN",
    "redact": "LAYA_GATE_REMOTE_REDACT", "questions": "LAYA_GATE_REMOTE_QUESTIONS",
    "ack": "LAYA_GATE_REMOTE_ACK", "fallback": "LAYA_GATE_REMOTE_FALLBACK",
    "connect_timeout": "LAYA_GATE_REMOTE_CONNECT_TIMEOUT", "total_timeout": "LAYA_GATE_REMOTE_TOTAL_TIMEOUT",
}


@dataclass(frozen=True)
class RemoteConfig:
    """Resolved opt-in. `enabled` is False unless every requirement holds; `problems` says why not.

    No token value lives here: only the path. The client reads the file at call time."""
    enabled: bool = False
    url: Optional[str] = None
    token_file: Optional[str] = None
    pin: tuple = ()
    redact: str = DEFAULT_REDACT
    questions: tuple = ("decide",)
    ack: Optional[str] = None
    fallback: str = DEFAULT_FALLBACK
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    total_timeout_s: Optional[float] = None       # None: per-question default
    problems: tuple = ()

    def total_timeout_for(self, question: str) -> float:
        return self.total_timeout_s if self.total_timeout_s is not None else DEFAULT_TOTAL_TIMEOUT_S[question]

    @classmethod
    def resolve(cls, env: Any = None, config: Any = None) -> "RemoteConfig":
        """Config (`remote:` block) then env, env wins. Anything wrong or missing leaves remote
        OFF and records why. The default, with no input at all, is local-only."""
        env = os.environ if env is None else env
        cfg = config if isinstance(config, dict) else {}

        def pick(key, cfg_key=None):
            v = env.get(ENV_VARS[key])
            if v is not None and str(v).strip():
                return str(v).strip()
            c = cfg.get(cfg_key or key)
            return None if c is None or not str(c).strip() else str(c).strip()

        problems: List[str] = []
        env_backend = env.get(ENV_VARS["backend"])
        if env_backend is not None and str(env_backend).strip():
            wanted = str(env_backend).strip().lower() == "remote"
        else:
            wanted = cfg.get("enabled") is True
        if not wanted:
            return cls()
        url = pick("url")
        if not url:
            problems.append("url is not set")
        elif not re.match(r"^https://", url) and not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:|/|$)", url):
            problems.append("url must be https unless it is loopback")
        # Inline tokens are refused on purpose: only a file path is accepted.
        if cfg.get("token") is not None:
            problems.append("inline token is not allowed; use token_file")
        token_file = pick("token_file") or DEFAULT_TOKEN_FILE
        redact = (pick("redact") or DEFAULT_REDACT).upper()
        if redact not in REDACT_LEVELS:
            problems.append("redact level must be R1, R2 or R3")
        raw_q = pick("questions") or "decide"
        questions = tuple(q.strip().lower() for q in raw_q.split(",") if q.strip())
        if not questions or any(q not in QUESTION_NAMES for q in questions):
            problems.append("questions must be a subset of decide, respond, affirm")
        ack = pick("ack")
        if not ack:
            problems.append("consent stamp (ack) is not set")
        fallback = pick("fallback") or DEFAULT_FALLBACK
        pin = tuple(p.strip() for p in (pick("pin") or "").split(",") if p.strip())
        ct, tt = DEFAULT_CONNECT_TIMEOUT_S, None
        for key in ("connect_timeout", "total_timeout"):
            raw = pick(key)
            if raw is None:
                continue
            try:
                v = float(raw)
                if v <= 0:
                    raise ValueError
            except ValueError:
                problems.append(f"{key} must be a positive number")
                continue
            if key == "connect_timeout":
                ct = v
            else:
                tt = v
        if problems:
            return cls(False, url, token_file, pin, redact, questions, ack, fallback, ct, tt, tuple(problems))
        return cls(True, url, token_file, pin, redact, questions, ack, fallback, ct, tt, ())
