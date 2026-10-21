# Laya remote: wire contract, auth and data egress

Status: contract for `laya serve` (server) and RemoteBackend (client). Code: `laya_remote_schema.py` (shared by both; tests in `tests/test_laya_remote_schema.py`). Design source: `laya-runtime-decision.md` section 7 and `laya-research/04-remote-design.md`. Where this file differs from them, section 9 says so.

## 1. Rules

1. Default is local only. No configuration means no network call.
2. The server returns probabilities and nothing else. Lanes, thresholds, quiet hours and every decision stay in the local gate.
3. An unreachable or untrusted remote never swallows an event.

## 2. Endpoints

HTTP/1.1, JSON, UTF-8, no streaming. No other path exists: no admin, upload or config write.

| Method and path | Auth | Returns |
|---|---|---|
| `GET /healthz` | none | `{ok, model_loaded, protocol}` only. Liveness and breaker probe. |
| `GET /health` | bearer | `{status, backend, version, protocol, model_loaded, fallback_chain}` |
| `GET /v1/info` | bearer | model identity and limits (`Info`) |
| `POST /v1/score` | bearer | scores for 1 to 16 items |

`/health` fields: `status` is `ok`, `loading`, `degraded` or `unavailable`. `backend` is the `LayaBackend.name` in use (`mlx`, `cuda`, ...) or `null` when model-less. `version` is the server version. `ok` means a backend is loaded and usable. A model-less server answers `unavailable`, not `ok`.

## 3. POST /v1/score

Request (`ScoreRequest`; unknown fields anywhere are rejected with 422):

```json
{"protocol": 1, "request_id": "<random uuid4>",
 "expect": {"model_revision": "...", "weights_sha256": "..."},
 "items": [{"id": "0", "state": "...",
            "question": {"type": "choice", "instructions": "...", "criteria": ["a", "b"]}}]}
```

- `request_id` is random, never derived from event data. A retry reuses it.
- `expect` pins the calibrated weights. A mismatch returns 409 and scores nothing.
- `state` and `question` are the exact strings the local gate would pass to `predict`. The wire `question` is the gate's `QUESTION["action"]` object (the `action` wrapper is dropped on the wire and re-added by the server).
- 1 to 16 items, unique `id`s, body at most 65536 bytes, at most 512 tokens per item. The live path sends one item.

Response 200 (`ScoreResponse`; the client ignores unknown fields):

```json
{"protocol": 1, "request_id": "...", "model_revision": "...", "weights_sha256": "...",
 "runtime": "...", "server_ms": 41, "queue_ms": 2,
 "items": [{"id": "0", "probabilities": {"a": 0.61, "b": 0.39}, "tokens": 318,
            "truncated": false, "ms": 38}]}
```

It never carries a decision, threshold or escalate flag. The client treats a 200 as a failure, not a score, if: a required field is missing, `request_id` differs, the model pin differs, item ids differ in order, the probability keys differ from the question's criteria, a probability is outside 0..1, or the sum is not 1 within 1e-6 (`ScoreResponse.validate_against`). A missing key must never read as 0.0.

## 4. Errors

Body for every non-2xx: `{"error": {"code", "message", "retryable"}}`. The message is fixed text and never echoes the request.

| HTTP | code | Meaning | Client |
|---|---|---|---|
| 400 | `bad_request` | not JSON, wrong type, missing field | fail, no retry |
| 401 | `unauthorized` | missing, wrong or expired token | auth failure: latch |
| 403 | `forbidden` | valid token, not allowed this question | auth failure: latch |
| 404 | `not_found` | unknown path | fail |
| 409 | `revision_mismatch` | `expect` differs from server weights | latch, alert |
| 413 | `too_large` | body over 64 KiB or over 16 items | fail, no retry |
| 415 | `unsupported_media_type` | not `application/json` | fail |
| 422 | `invalid_question` | bad criteria, unknown type, unknown field | fail, no retry |
| 426 | `protocol_unsupported` | `protocol` not served | latch |
| 429 | `rate_limited` | `Retry-After` header | fail now, counts toward breaker |
| 500 | `internal` | server fault | fail, one retry |
| 503 | `model_loading`, `overloaded`, `backend_unavailable` | not ready or no usable backend. `Retry-After` header | fail, one retry only if Retry-After <= 0.5 s |

Malformed request: 400, 413, 415 or 422. Bad or missing token: 401 (missing, malformed, unknown) or 403 (known token, not permitted). Backend unavailable (probe false, load failed, `BackendUnavailable`, `BackendRuntimeError` on every rung): 503 `backend_unavailable`. A scoring fault on a loaded backend: 500.

## 5. Auth

- Header `Authorization: Bearer <token>`. Never in a URL, body or log.
- Token form `laya_<kid>_<secret>`, 32 random bytes. Server stores SHA-256 hashes only and compares in constant time. Two tokens may be valid during rotation (90 days, or on suspected leak). The gate never writes the token file.
- Client reads the token from a file (default `~/.hermes/secrets/laya-remote.token`, mode 0600). Remote mode refuses to start if the file is group or world readable. Inline tokens in config are refused (`RemoteConfig` reports a problem).
- `/healthz` is the only unauthenticated path.
- Transport: `https://` (TLS 1.2+) for every host except loopback, which may use `http://`. No redirects followed. No verify-off flag. SPKI pin (`sha256/<base64>`, current plus next) alongside the bearer.
- Auth, pin, revision or TLS failure latches the remote state until restart or reconfigure, sends no further event data, and raises one `GATE-DOWN:` notice naming the cause class and no secret.

## 6. Opt-in configuration and timeouts

Remote is off unless the owner turns it on for that box. Env wins over config. Any invalid value leaves remote off and records the reason (`RemoteConfig.problems`).

| Env var | Config key (`remote:`) | Default | Meaning |
|---|---|---|---|
| `LAYA_GATE_BACKEND` | `enabled: true` | `local` | `remote` enables. Anything else is local. Env `local` overrides config `enabled`. |
| `LAYA_GATE_REMOTE_URL` | `url` | unset | Required. |
| `LAYA_GATE_REMOTE_TOKEN_FILE` | `token_file` | `~/.hermes/secrets/laya-remote.token` | Path only. |
| `LAYA_GATE_REMOTE_PIN` | `pin` | unset | SPKI pin(s), comma separated. |
| `LAYA_GATE_REMOTE_REDACT` | `redact` | `R2` | `R1` to `R3`. `R0` is never valid for remote. |
| `LAYA_GATE_REMOTE_QUESTIONS` | `questions` | `decide` | Subset of `decide,respond,affirm`. |
| `LAYA_GATE_REMOTE_ACK` | `ack` | unset | Required. Consent stamp `sha256(url_host \| pin \| redact_level \| questions)[:16]`; changing any input voids it. |
| `LAYA_GATE_REMOTE_FALLBACK` | `fallback` | `local-then-standing` | Failure policy. |
| `LAYA_GATE_REMOTE_CONNECT_TIMEOUT` | `connect_timeout` | 0.5 s | Positive number. |
| `LAYA_GATE_REMOTE_TOTAL_TIMEOUT` | `total_timeout` | 1.5 s decide, 2.0 s respond and affirm | Overrides all questions. |

Enabling needs URL, a valid redact level, a valid question set and a matching ack. Retries: at most one, only for connection reset, 500, or 503 with Retry-After <= 0.5 s, only if budget remains, never on 4xx. Breaker: 3 consecutive failures open it for 60 s, then a `/healthz` probe. `FLEET_GATE_TIMEOUT` should be 5 s when remote is on (3 s today).

The consent stamp check itself (and `--remote-ack`) is gate work, not part of the schema module. The schema module only requires that an `ack` value is present.

## 7. Data sent to a remote server (exhaustive)

Everything below travels inside the `state` or `question` strings of `POST /v1/score`, plus the three protocol fields. The request has no other operator data. "Event text" marks fields that contain operator or session text. Redaction levels apply in the gate before the wire (R1 secret scrub, `~` home prefix, client-name blocklist; R2 adds last-component cwd, basename paths, pseudonyms, masked emails and hosts; R3 removes free text).

Every question (`decide`, `respond`, `affirm`):

| Field | Event text | Notes |
|---|---|---|
| Doctrine text (`DOCTRINE`, `RESPOND_DOCTRINE`, `AFFIRM_DOCTRINE`) | no | static policy text |
| Question object (instructions, criteria) | no | static |
| `protocol`, `request_id`, `expect` pin, item `id` | no | `request_id` random |
| Bearer token | no | header only |

`decide` (lane 3), inside `state`:

| Field | Event text | Redaction |
|---|---|---|
| `text` (the `Event:` line), clipped to 600 chars | YES | R3: event family only |
| `kind` | only if `text` is empty | |
| context: session short name | YES | R2 pseudonym; R3 dropped |
| context: profile name | YES | R2 pseudonym; R3 dropped |
| context: cwd | YES | R1 `~`; R2 last component; R3 dropped |
| context: last 2 output lines (280 chars) | YES, raw session output, highest risk | R1 scrub; R2 path/host/email scrub; R3 dropped |
| context: seconds quiet, stall window | no | numbers |
| context: completion token name, due seconds | token name YES | R2 `<token>`; R3 dropped |
| 24-hour same-kind count | no | integer |

`respond` and `affirm`, only when separately opted in, add: session name, profile, cwd (as above); standing ruling (1200 chars, YES, high sensitivity, R3 dropped); computed state (one word); last messages tail (1200, YES, R3 dropped); latest inbound instruction (800, YES, R3 dropped); in-flight watch count; prior decision count (24 h); context (800, YES); the text or action under decision (600, YES, may be a command line, R1 scrub mandatory).

Never sent under any configuration: the `session` request field as a field; `kind` unless `text` is empty; `expecting`; decision ids and timestamps; the three local decision logs; registry, armed and watch files and transcripts beyond the fields above; thresholds, enforce flags, quiet hours, repeat windows; lane 1 and 2 events (no model is called, no request exists), including everything held in quiet hours; verdicts, `lane`, `rule`, `escalate`; the bearer token in any log or error; environment variables, secret files and `.env`.

Audit (local decision log, additive): `backend`, `remote_host`, `redact_level`, `egress_bytes`, `egress_sha256` (hash of the exact `state`), `model_revision`, `truncated`, `remote_ms`.

## 8. Server obligations

No logging of `state`, `question` or `probabilities`. Metadata-only logs kept 14 days. No core dumps or swap holding request memory. No forwarding, training or persistent cache. Unprivileged user. Third-party hosted inference is out of scope and needs its own privacy review.

## 9. Deviations from the design docs

- Added `GET /health` (bearer). The design has only `/healthz` (unauth, three fields) and `/v1/info`. `/health` reports backend name, version and status, matching the local gate's `/health` backend fields. It is authenticated so the unauthenticated surface stays at the design's three fields.
- Added the 503 code `backend_unavailable` to the design's `model_loading` and `overloaded`, so the client can tell a backend with no usable rung from a load in progress.
- Added `LAYA_GATE_REMOTE_CONNECT_TIMEOUT` and `LAYA_GATE_REMOTE_TOTAL_TIMEOUT` (and config keys). The design fixes the timeouts and has no override.
- Config-file opt-in (`remote.enabled: true` and keys) is added next to the design's env-only variables. Env wins. The consent stamp is still required either way.
- Token source is a file path only. Env-var token values are not accepted (the design has no token env var either); inline config tokens are refused.
- Wire `question` omits the gate's `action` wrapper, as the design's examples do. Schema validation of the server's 422 for unknown fields is implemented by `ScoreRequest.from_dict`.
- Not covered here: consent-stamp computation, the 0600 file check, redaction, the breaker and TLS pinning. Those belong to the client and gate implementations; this contract fixes only what they exchange.

## 10. Running the server: `laya serve`

```
LAYA_SERVE_TOKEN=laya_<kid>_<secret> scripts/laya.py serve --backend mlx --port 8765 \
    --model-revision <rev> --weights-sha256 <sha>
```

- `--host` defaults to `127.0.0.1`. `--backend` is any local override (`auto`, `mlx`, `cuda`, `rocm`, `legacy`, `vulkan`, `cpu`, `stub`, `none`); `remote` is refused, so a server cannot host a client of itself. The backend is loaded by the same `BackendManager` as the gate, so the fallback ladder and demotion apply.
- Token: read from the env var named by `--token-env` (default `LAYA_SERVE_TOKEN`), never from argv. Comma separated values are all valid (rotation). A value that does not match `laya_<kid>_<secret>` stops startup. Only SHA-256 digests are kept; comparison is constant time.
- No token means no start (exit 2), unless `--insecure-no-auth` is given. That flag logs a warning at startup, and is refused on any non-loopback host.
- `--tls-cert` and `--tls-key` serve TLS 1.2+. A non-loopback bind without TLS logs a warning; terminate TLS in front of it.
- `--model-revision` and `--weights-sha256` set the pin clients must send in `expect`. A mismatch is 409. Unset, both read `unknown`, so only a client that pins `unknown` can score: set them.
- Loading runs in a thread. While it runs, `/healthz` answers `ok: false`, `/health` reports `loading` and `/v1/score` returns 503 `model_loading`.
- `/health` status: `ok` (backend loaded, no failed or demoted rung), `degraded` (loaded after a failed or demoted rung), `unavailable` (no backend), `loading`.
- Scoring is serialised; `queue_ms` is the wait for the previous call.
- A backend answer whose labels differ from the question's criteria, or whose probabilities are outside 0..1 or sum further than 1e-3 from 1, returns 500 `internal`; it is never passed on as a score. Sums within 1e-3 are renormalised.
- Log lines carry request id (only when it looks like a UUID), route, status and milliseconds. Backend exception text is cut to the exception type, since it can echo the event.

### Decisions on points the contract left open

- `/health` needs the bearer token (section 2) and `/healthz` does not. `/health` names the backend and fallback chain, which the unauthenticated surface should not reveal.
- There is no per-token permission yet, so the server never returns 403; the code stays defined for later.
- Wrong method on a known path returns 404 (the contract defines no 405).
- Backends do not report token counts. `tokens` is `0` and `truncated` is `false` unless the backend output supplies them, and the 512-token limit is not enforced beyond the 64 KiB body cap.
- `/v1/info` fills `temperature_digest` and `golden_digest` with `unknown` until the backends expose them.


## 11. Remote on the backend ladder

The gate walks a ladder of rungs and uses the first one that loads. The built-in order is `mlx`, `cuda/rocm`, `legacy-gpu`, `vulkan`, `cpu`, `remote`. Remote is a rung like the others, with three differences: it is opt-in, it can sit anywhere, and a failed remote rung comes back after a cooldown.

### Config syntax (`~/.hermes/config.yaml`)

```yaml
laya:
  # Optional. Omit it for the built-in order. Names: mlx, cuda/rocm (or cuda, rocm), legacy-gpu,
  # vulkan, cpu, remote. A bad name is dropped and reported as ladder_error in /health.
  ladder: [mlx, remote, cpu]          # remote tried second, cpu is the local safety net
  remote:                             # section 6 keys; same block for a bare "remote" entry
    enabled: true                     # nothing below is used unless this is true
    url: https://laya.example.net:8765
    token_file: ~/.hermes/secrets/laya-remote.token   # mode 0600
    ack: <consent stamp>
    total_timeout: 1.5
```

A second remote, or a remote with its own settings, is a mapping entry. It inherits the `remote:` block and overrides it key by key. Each entry must end up enabled with its own `url` and `ack`:

```yaml
laya:
  ladder:
    - remote: {url: https://laya-a.example.net:8765, ack: <stamp a>}
    - remote: {url: https://laya-b.example.net:8765, ack: <stamp b>, token_file: ~/.hermes/secrets/b.token}
    - mlx
    - cpu
  remote:
    enabled: true
```

`LAYA_GATE_BACKEND=local` turns every remote entry off. `LAYA_GATE_BACKEND=remote` enables the bare `remote` entry; mapping entries need their own `enabled`. `LAYA_BACKEND=remote` (or `laya.backend: remote`) starts the ladder at the first remote entry and falls down from there.

### Opt-in

With the default config no remote class is built and no socket is opened. A remote entry that is configured but not enabled (or enabled with a problem, such as a missing ack) is skipped with its reason, a WARNING is logged, and the notice appears in `/health` under `backend_status.notices`. The ladder then continues local-only.

### Fallback and cooldown

`RemoteUnreachable`, `RemoteAuthError`, `RemoteTimeout` (and any other remote failure, at load or at scoring time) move the gate to the next rung. If no rung is left, the gate is model-less and uses the existing path: lanes 1 and 2 run by rule, lane 3 fails open toward showing the event.

Decision: a cooldown, not a per-request decision. A failing remote costs up to its total timeout (1.5 s for `decide`) on every call, and the gate's own timeout is 5 s. So a failed remote rung is skipped for 60 s (`REMOTE_COOLDOWN_S`, the same as the contract's breaker), with no network traffic in that window. After the cooldown the next score call tries it again; if it loads it takes over again, and the local backend it displaced is kept loaded so a second failure does not reload it. Auth, pin and protocol failures latch instead: no retry until restart, as section 5 requires. Local rungs keep the older rule: demoted for good.

### /health

`GET /health` on the gate shows the latest state, not history:

- `backend`: the active rung name, `active_label` its detail (`remote https://host:port`). Both are null when model-less.
- `skipped`: one item per rung that is not serving, with `rung`, `status` (`skipped`, `failed`, `demoted`), `reason`, and for remote rungs `retry_in_s` (seconds left in the cooldown, null when latched). Rungs below the active one that were never tried are not listed.
- `fallback_chain`: the full history, as before.
- `backend_status.notices` and `backend_status.ladder`.

Remote reasons name the rung and the cause, for example `remote http://10.0.0.5:8765: auth failed (401)`, `remote http://10.0.0.5:8765: timeout after 1.5s`, `remote http://10.0.0.5:8765: connection refused`. They hold only scheme, host and port: never the token, the path, or text sent by the server.
