# Opt-in remote Laya server: protocol, auth, data egress

Status: PROPOSAL for owner approval. Nothing here is implemented.
Task: t_8348c766. Source read: `laya_gate.py` (1261 lines, `ESCALATE_IF_AT_LEAST` = 0.59 at line 104) and `fleet_watch.py` (`gate_verdict`, `event_context`). Model facts come from `01-architecture.md` in this folder.

Assumption: "rung" means a hardware rung, a tier of box that can run the model locally or cannot (for example an Apple Silicon box with MLX, a CUDA box, a CPU-only box, a small box with no usable runtime). The repo does not define the word. If it means something else, only section 4.4 changes.

## 0. Rules that do not move

1. Default is local only. With no configuration, no byte of event data leaves the machine. This is the shipped default at every rung.
2. Remote needs an explicit opt-in that names the server, the redaction level and the questions allowed. A changed host, redaction level or question set voids the opt-in.
3. The remote server computes probabilities and nothing else. Lanes, thresholds, repeat windows, quiet hours, the parked rule, logging and every decision stay in the local gate.
4. Lane 1 and lane 2 never call a model, local or remote. Nothing leaves the machine for them.
5. An unreachable or untrusted remote never causes an event to be swallowed (section 5).

## 1. What the gate gives the model today

The model (`laya_mlx`, `predict(state, questions)`) receives exactly two arguments per call: a text `state` and a `questions` dict. There are three call sites, all through `load_model().predict(...)`.

### 1.1 Lane 3 escalation, `classify()` (line 862-868)

```
state = DOCTRINE
        + "Situation: " + (context or "no further detail available")
        + "Event: " + probe                     # text, or kind if text is empty; NOT clipped
        + "Events of this kind on this session in the last 24 hours: " + N
questions = QUESTION   (static, 2 criteria)
```

`context` is built by the caller in `fleet_watch.event_context()`:

```
session <short name> on the <profile> profile, working in <cwd>;
its most recent output was <last 2 output lines, 280 chars>;
quiet for <N>s (the stall window is <N>s);
it owes the completion token <token> within <N>s
```

### 1.2 Response decision, `respond()` and affirmation, `affirm()` (via `_ask`, line 986)

```
state = RESPOND_DOCTRINE or AFFIRM_DOCTRINE + "\n\n" + situation(...)
```

`situation()` (line 916) assembles, clipped to `SITUATION_CHARS` = 4000 characters:

- session name, registry profile, registry cwd
- the operator's standing ruling for the session (`<session>_focus.md`, first 1200 chars)
- computed state (WORKING / IDLE / QUEUED / NEEDS-INPUT / DEAD)
- what the session last said (last 1200 chars of the last 3 messages, read from its transcript)
- the most recent inbound instruction that reached it (last 800 chars)
- count of armed watches or pending dispatches
- count of decisions made about this session in 24 hours
- caller context (first 800 chars)
- event or action under decision (first 600 chars)

Static policy text (never operator data): `DOCTRINE`, `RESPOND_DOCTRINE`, `AFFIRM_DOCTRINE`, and the three question dicts (`QUESTION`, `RESPOND_QUESTION`, `AFFIRM_QUESTION`).

### 1.3 What comes back and what the gate does with it

Local result: `answers.action.probabilities`, a map from criterion to a temperature-scaled 2-way (or 6-way) softmax probability.

- Lane 3: `conf = probs["interrupt her now"]`; escalate if `conf >= 0.59`.
- Respond: top probability; below 0.55 the standing policy for the state applies.
- Affirm: `risk = 1 - probs["safe to affirm"]`; affirm only below 0.20 (and only enforced when `LAYA_GATE_AFFIRM_ENFORCE` is on).

The model is the only thing the remote has to replace. Every number above stays in the gate.

## 2. Protocol

HTTP/1.1, JSON, UTF-8. No streaming, no websockets. Version in the path and the body. The server is a thin wrapper around `predict`.

### 2.1 Endpoints

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness. Reveals only `ok`, `model_loaded`, `protocol`. |
| `GET /v1/info` | bearer | Model identity, runtime, rung, limits, golden digest. |
| `POST /v1/score` | bearer | Score one or more state/question pairs. |

No other path. No admin path, no model upload, no config write.

`GET /healthz` response:

```json
{"ok": true, "model_loaded": true, "protocol": 1}
```

`GET /v1/info` response:

```json
{
  "protocol": 1,
  "model": "aac6fef/laya-mlx",
  "model_revision": "20aed815fc6acde75733882e7ec0e3f28aeb9717",
  "weights_sha256": "<sha256 of the weights file set>",
  "runtime": "laya-mlx 0.4.0 / mlx 0.32.3",
  "rung": "apple-silicon-mlx",
  "dtype": "float16",
  "temperature_digest": "<sha256 of rl_agent_config.json temperatures>",
  "golden_digest": "<sha256 of the golden-set probabilities, section 6.3>",
  "limits": {"max_items": 16, "max_body_bytes": 65536, "max_tokens": 512}
}
```

### 2.2 `POST /v1/score`

Request:

```json
{
  "protocol": 1,
  "request_id": "6f0c2a9e-0d4b-4c1e-9a55-3b7d1e2f8a10",
  "expect": {
    "model_revision": "20aed815fc6acde75733882e7ec0e3f28aeb9717",
    "weights_sha256": "<same value the gate was calibrated with>"
  },
  "items": [
    {
      "id": "0",
      "state": "The operator runs a fleet of coding agents. ...\n\nSituation: ...\nEvent: ...\nEvents of this kind on this session in the last 24 hours: 2.",
      "question": {
        "type": "choice",
        "instructions": "What is the right action for the operator here?",
        "criteria": ["interrupt her now", "log it and stay silent"]
      }
    }
  ]
}
```

- `request_id` is a fresh random UUID. It is NOT derived from the session or the event text (the local decision id is `sha1(at|session|text)` and does not leave the machine).
- `expect` is a hard pin. If the server's weights differ, it answers 409 and scores nothing. The thresholds were calibrated on one set of weights and one temperature table; a different build is a different classifier.
- `state` and `question` are the exact strings the local gate would pass to `predict`. The server must not rewrite, trim or template them. Identical input means identical token sequence.

Response, 200:

```json
{
  "protocol": 1,
  "request_id": "6f0c2a9e-0d4b-4c1e-9a55-3b7d1e2f8a10",
  "model_revision": "20aed815fc6acde75733882e7ec0e3f28aeb9717",
  "weights_sha256": "<...>",
  "runtime": "laya-mlx 0.4.0 / mlx 0.32.3",
  "server_ms": 41,
  "queue_ms": 2,
  "items": [
    {
      "id": "0",
      "probabilities": {"interrupt her now": 0.6123, "log it and stay silent": 0.3877},
      "tokens": 318,
      "truncated": false,
      "ms": 38
    }
  ]
}
```

- `probabilities` is the same quantity as local `answers.action.probabilities`: temperature-scaled softmax, every criterion present, sums to 1 within 1e-6. Full float precision, not rounded (the gate rounds only for its own verdict).
- `truncated` is true when the 512-token limit cut the state. `01-architecture.md` section 4 notes that truncation is from the right, so the `Event:` line is the first thing cut. The gate should log this and treat `truncated: true` on lane 3 as a data-quality flag (decision D9).
- The response never carries a decision, a threshold or an "escalate" flag.

### 2.3 Errors

Body for every non-2xx: `{"error": {"code": "<code>", "message": "<fixed text>", "retryable": false}}`. The message is fixed text and never echoes any part of the request.

| HTTP | `code` | Meaning | Gate behaviour |
|---|---|---|---|
| 400 | `bad_request` | Not JSON, wrong types, missing field | Failure, no retry |
| 401 | `unauthorized` | Missing, wrong or expired token | Section 3.4 |
| 403 | `forbidden` | Token valid but not allowed this question type | Failure, no retry |
| 404 | `not_found` | Unknown path | Failure |
| 409 | `revision_mismatch` | `expect` does not match server weights | Failure, latch (3.4 style), alert |
| 413 | `too_large` | Body over 64 KiB or over 16 items | Failure, no retry |
| 415 | `unsupported_media_type` | Not `application/json` | Failure |
| 422 | `invalid_question` | Criteria empty, duplicated, or `type` unknown | Failure |
| 426 | `protocol_unsupported` | `protocol` not served | Failure, latch |
| 429 | `rate_limited` | Too many requests; `Retry-After` header | Failure now; breaker (2.5) |
| 500 | `internal` | Server fault | Failure; one retry allowed (2.4) |
| 503 | `model_loading` or `overloaded` | Not ready; `Retry-After` header | Failure; one retry only if Retry-After <= 0.5s |

A 200 whose body fails client validation (missing criterion, probabilities not summing to 1, wrong `request_id`, wrong `model_revision`) is treated as a failure, never as a score. Note the local code does `probs.get("interrupt her now") or ans.get("confidence") or 0.0`. A missing key silently becomes 0.0 there, which reads as "stay silent". The remote client parser must be strict and raise on a missing key. This is the direction that costs ten times as much.

### 2.4 Timeouts and retries

The daemon calls the gate with `FLEET_GATE_TIMEOUT` = 3 s (`fleet_watch.py:209`). If the daemon gives up, it shows the event, so the remote call plus any fallback must fit inside that budget.

- Connect timeout 0.5 s. Total timeout for `decide` 1.5 s. For `respond` and `affirm` 2.0 s.
- At most one retry, only for connection reset, 500, or 503 with `Retry-After` <= 0.5 s, and only if time remains in the budget. Never retry 4xx.
- Retry reuses the same `request_id`; the server treats scoring as idempotent (pure function of input).

### 2.5 Circuit breaker

3 consecutive failures open the circuit for 60 s. While open, the gate goes straight to the failure path (section 5) without calling out. After 60 s one probe `GET /healthz`; success closes the circuit. State is in memory; a gate restart resets it.

### 2.6 Batching

Server accepts up to 16 items per request. The live path sends exactly one item per call, because events arrive one at a time and the gate holds per-session state between them. Batching exists for the parity and calibration runs (`evals/calibrate_laya.py`), which score hundreds of recorded situations. Items in a batch are scored independently, and a batch result must equal the same items scored one by one.

### 2.7 Versioning

- Path carries the major version (`/v1/`). Body carries `protocol: 1`.
- Within v1: additive changes only (new response fields, new optional request fields). The client ignores unknown response fields. The server rejects unknown request fields with 422 so a client can never send data the server contract does not name.
- A breaking change is `/v2/`; the server may serve both. `/v1/info` lists served protocols in `protocol` negotiation (client picks the highest it supports).
- Model identity is separate from protocol: `model_revision` + `weights_sha256` + `temperature_digest`. A change in any of them is a recalibration event, not a protocol event.

## 3. Auth and transport

### 3.1 Bearer token

- Format: `laya_<kid>_<secret>`. `kid` is 8 hex characters naming the key. `secret` is 32 random bytes, base64url.
- Client storage: a file, mode 0600, owner only, at `~/.hermes/secrets/laya-remote.token` (same directory that already holds `ebay-developer-creds.txt`). The config holds the file PATH, never the token value. Not in an environment variable, not in a config file that is committed, not in the decisions log. The gate refuses to start remote mode if the file is group- or world-readable.
- Server storage: only SHA-256 hashes of valid tokens, one per `kid`, in a file readable by the service user alone. A stolen server file does not yield a usable token.
- Comparison is constant-time.
- Sent as `Authorization: Bearer <token>`. Never in the URL or query string. Not logged anywhere, client or server.

### 3.2 Rotation

- Scheduled: every 90 days. Immediate: on any suspected leak, or when a machine that held the token is retired.
- Procedure: server adds the new `kid` hash (two valid tokens at once), client switches file contents, server removes the old `kid` after the first successful call with the new one. No downtime.
- The server logs `kid` per request, so an old token still in use is visible.
- Each rotation is the owner's act: the gate never rotates or writes the token file itself.

### 3.3 Transport

| Case | Rule |
|---|---|
| Loopback (`127.0.0.1`, `::1`) | Plain `http://` allowed. Needed for a local second process or an SSH tunnel. |
| Any other host | `https://` required. TLS 1.2 minimum, 1.3 preferred. Config load fails with a clear error on `http://` to a non-loopback host. No override flag. |
| Redirects | Never followed. A 3xx is a failure. |
| Tailnet IPs | Same as any non-loopback host: HTTPS required. WireGuard encryption is not a substitute, because the bearer token and the event text would otherwise sit in clear text inside the tunnel on a hop the gate cannot verify. A tailnet server gets a certificate via `tailscale cert` or a private CA. |
| Hostname check | Standard hostname/SAN verification against the system trust store, unless pinned. |

Certificate options, in order of recommendation:

1. **SPKI pin (recommended for v1).** Config `LAYA_GATE_REMOTE_PIN=sha256/<base64>` pins the server's public-key hash. Fits a private box with a self-signed or private-CA cert and removes any CA from the trust path. Support one current and one next pin so a certificate renewal does not lock the gate out. If a pin is set and does not match, the connection is refused and handled as an auth failure (3.4).
2. **System trust store + hostname verification.** Allowed when the server has a public CA certificate. No pin.
3. **mTLS.** Supported as an optional second factor (client cert file path in config, 0600). Not required in v1. For a single operator and one server, bearer + pin gives the same property (only the gate can call, only the right server can answer) with less to rotate. Listed as a decision (D5).
4. Skipping verification (`verify=False`, `-k`): not supported, no flag.

### 3.4 Auth and trust failure

Applies to 401, 403, 409, 426, a pin mismatch and a TLS verification failure.

- No data is sent again for that event. The score is not used.
- The failure is NOT retried with the same credentials (a bad token does not fix itself).
- The gate latches the remote state to `auth-failed` (or `untrusted` for pin/TLS/revision) until it is restarted or reconfigured, so it does not hammer the server or leak more events to a server it no longer trusts. Retry against `/healthz` only, which carries no event data.
- The event takes the failure path in section 5.
- One daemon notice per latch, in the existing style (`GATE-DOWN:` family), stating the cause class (auth, pin, revision) and no secret. It reaches the operator because it is a lane-1 event.
- Server side: 401 without any hint about which part was wrong; per-source-IP lockout after 5 failures in 60 s (429); the failure is logged with `kid` presented (if parseable) and source address, never the token.

## 4. Data egress

### 4.1 Opt-in configuration

Matches the gate's existing style (`LAYA_GATE_*` environment variables read at start). Local is the default; the absence of every variable below means local.

| Variable | Default | Meaning |
|---|---|---|
| `LAYA_GATE_BACKEND` | `local` | `local` or `remote`. Any other value, empty, or unparseable means `local` and is logged. |
| `LAYA_GATE_REMOTE_URL` | unset | Base URL, e.g. `https://laya.example:8443`. Required if backend is `remote`. |
| `LAYA_GATE_REMOTE_TOKEN_FILE` | unset | Path of the token file (3.1). |
| `LAYA_GATE_REMOTE_PIN` | unset | Optional SPKI pin(s), comma separated. |
| `LAYA_GATE_REMOTE_REDACT` | `R2` | Redaction level (4.3). Minimum accepted is `R1`. |
| `LAYA_GATE_REMOTE_QUESTIONS` | `decide` | Subset of `decide,respond,affirm`. `respond` and `affirm` carry far more text (1.2) and are separate opt-ins. |
| `LAYA_GATE_REMOTE_ACK` | unset | Consent stamp, 4.2. |
| `LAYA_GATE_REMOTE_FALLBACK` | `local-then-standing` | Failure behaviour, section 5. |

Per rung: the opt-in is per box, because each box has its own environment and its own gate process. A box at a rung that CAN run locally stays local unless its own environment sets the variables. A box at a rung that cannot run locally still defaults to local: with no model installed, the gate already reports "classifier unavailable" and takes the existing safe path (5.1). Remote there is an explicit step, not a silent upgrade.

### 4.2 Consent stamp

`LAYA_GATE_REMOTE_ACK` must equal `sha256(url_host | pin | redact_level | questions)[:16]` as printed by a new `laya_gate.py --remote-ack` command, which also prints the egress table for the chosen level. Remote mode starts only if the stamp matches the current configuration. Changing the host, pin, redaction level or question set invalidates the stamp, and the gate falls back to local until the owner re-acknowledges. This stops a stray environment edit from redirecting event text to a new server.

### 4.3 Redaction levels

Applied in the gate, on the assembled `state` and on every field before it is built, so a redaction bug cannot be fixed on the wrong side of the wire.

| Level | Applied | Cost |
|---|---|---|
| R0 | None. Not selectable remotely. Local only. | n/a |
| R1 | Secret scrub: bearer/Authorization values, `sk-`/`ghp_`/`xox`/`AKIA`/JWT-shaped strings, private-key blocks, any `KEY=`/`TOKEN=`/`SECRET=`/`PASSWORD=` value, runs of 32+ hex or base64 characters, URLs with credentials. Home directory prefix replaced with `~`. | Smallest score change. Minimum allowed for remote. |
| R2 (default for remote) | R1, plus: cwd reduced to its last path component; every other absolute path reduced to its basename; session names, profile names and project names replaced by stable local pseudonyms (`S1`, `S2`, ...); email addresses and hostnames replaced by `<email>` and `<host>`. Pseudonym map stays local. | Moderate. The model sees the same words and the same shape. |
| R3 | R2, plus free text removed: only event family, quiet seconds, counts, state and a fixed one-line description are sent; `its most recent output was ...`, standing ruling, last messages and inbound instruction are dropped. | Largest. Probably removes most of the signal that lane 3 uses, and is the only level suitable for `respond` or `affirm` over an untrusted path. |

Important: the client names in memory (the engagement, vendors) appear in paths, session names and output lines. R2 pseudonymizes session and project names and strips paths, but a client name inside free text (`its most recent output`) survives R2. Only R3 removes it. A local client-name blocklist (`~/.hermes/secrets/laya-redact-terms.txt`, one term per line, case-insensitive, replaced by `<term>`) is part of R1 and above. See D4.

Every level other than R0 changes the model's input, so every level needs its own parity and threshold check before it is used (6.3). The 0.59 threshold was calibrated on unredacted input only.

### 4.4 Field-by-field egress table

"Sent" means the field's content is inside the `state` or `question` strings of a `POST /v1/score`. Nothing else is sent: the request has no other operator data. Sources are in `laya_gate.py` unless marked `fw` for `fleet_watch.py`.

Per-call, applies to all three questions:

| # | Field | Where it comes from | Sent? | Notes and redaction |
|---|---|---|---|---|
| 1 | `DOCTRINE` / `RESPOND_DOCTRINE` / `AFFIRM_DOCTRINE` | static code (`DOCTRINE` line 77, `RESPOND_DOCTRINE` line 112, `AFFIRM_DOCTRINE` line 152) | Yes | Policy text, not operator data. Reveals the operator's escalation policy. Low sensitivity. |
| 2 | `QUESTION` / `RESPOND_QUESTION` / `AFFIRM_QUESTION` | static code | Yes | Same. |
| 3 | `protocol`, `request_id` (random), `expect` pin | client | Yes | `request_id` is random, not derived from data. |
| 4 | Bearer token | token file | Header only | Never in body or URL. |

Lane 3 (`decide`), from the `/decide` request and `classify()`:

| # | Field | Sent? | Notes and redaction |
|---|---|---|---|
| 5 | `session` (request field) | No, not as a field | Used only locally: family strip, repeat keys, history keys, held records, parked-rule transcript read. Its name can still appear INSIDE `text` and `context`, so R2 pseudonymizes it there (fields 6 and 7). |
| 6 | `kind` | Only if `text` is empty | `probe = text or kind`. Normally not sent. |
| 7 | `text` (becomes `probe`, the `Event:` line) | Yes | Unclipped in `classify` today. Remote path clips to 600 characters (same as `situation()`). Redacted per level. R3: family token only. |
| 8 | `context`: session short name | Yes | R2: pseudonym. R3: dropped. |
| 9 | `context`: profile name | Yes | R2: pseudonym. R3: dropped. |
| 10 | `context`: cwd | Yes | R1: `~` prefix. R2: last component only. R3: dropped. |
| 11 | `context`: last 2 output lines (280 chars) | Yes | This is raw session output: the field most likely to hold secrets, client names, code, file paths. R1 scrub + blocklist. R2 path/host/email scrub. R3: dropped. |
| 12 | `context`: seconds quiet, stall window | Yes | Numbers. No redaction. |
| 13 | `context`: completion token name and due seconds | Yes | The token name is a dispatch label, often containing a project or task slug. R2: replaced by `<token>`. R3: dropped. |
| 14 | `expecting` | No | Code-only input. Selects lane 2 before any model call. |
| 15 | `hist` count (24 h events of this kind) | Yes | Integer. |
| 16 | `_newest_message(session)` (transcript read by the parked rule) | No | Matched by regex locally. Never enters `state`. |
| 17 | Quiet-hours flag, repeat-window state, `_recent`, `_stall_alerted` | No | Code-only. |

`respond` and `affirm` additionally send (via `situation()`), only when that question is opted in:

| # | Field | Sent? | Notes and redaction |
|---|---|---|---|
| 18 | `session` name, registry `profile`, registry `cwd` | Yes | As 8-10. |
| 19 | Standing ruling (`<session>_focus.md`, 1200 chars) | Yes | The operator's own instructions for that session. High sensitivity. R1 scrub + blocklist. R2: same plus path scrub. R3: dropped. |
| 20 | Computed state (WORKING / IDLE / ...) | Yes | One word. |
| 21 | Last messages tail (1200 chars) | Yes | The session's own words. High sensitivity. R3: dropped. |
| 22 | Most recent inbound instruction (800 chars) | Yes | What was typed or dispatched into the session. High sensitivity. R3: dropped. |
| 23 | In-flight watch/dispatch count | Yes | Integer. |
| 24 | Prior decisions on this session in 24 h | Yes | Integer. |
| 25 | `context` (800 chars) | Yes | As 11, longer. |
| 26 | `text` / `action` under decision (600 chars) | Yes | The `affirm` action string can be a command line. R1 scrub is mandatory. |
| 27 | `state` argument, `choice_ui` flag | `state` yes (as 20); `choice_ui` no | `choice_ui` and the first-level shape rules decide lane 1 before any model call. |

Never leaves the machine under any configuration:

| # | Field | Why it stays |
|---|---|---|
| 28 | Decision ids (`sha1(at\|session\|text)`), timestamps | Link a call to a person and a time. |
| 29 | `~/.hermes/logs/laya-gate-decisions.jsonl`, `-labels.jsonl`, `-held.jsonl` | Full local record, including truncated situations. |
| 30 | The fleet registry, armed set, watch files, transcripts (beyond fields 11, 21, 22) | Whole-file data is never read into a request. |
| 31 | Thresholds (0.59, 0.55, 0.20), enforce flags, quiet hours, repeat windows | Policy stays local so the server cannot move it. |
| 32 | Lane 1 and lane 2 events | No model is called, so no request exists. This includes everything held in quiet hours and every repeat inside the repeat window. |
| 33 | Verdicts, `escalate`, `lane`, `rule` | The server returns probabilities only. |
| 34 | The bearer token value in any log or error message | Section 3.1. |
| 35 | Environment variables, secrets files, `.env` | Never read by the redactor input path. |

Coverage check: every variable that appears on the line that builds `state` in `classify` (lines 862-866: `DOCTRINE`, `context`, `probe`, `len(hist)`) is rows 1, 7-13, 15. Every argument to `situation()` (`session`, `probe`, `context`, `state`) and every part it builds is rows 18-27. `respond()` and `affirm()` take `session`, `probe`/`action`, `state`, `context`, `choice_ui`, `log`: rows 5, 7/26, 20, 25, 27; `log` is local. The three `/decide` request fields not in `state` (`session`, `kind`, `expecting`) are rows 5, 6, 14.

### 4.5 Egress audit trail (client side)

For every remote call the gate adds to the decision log record: `backend` (`remote`/`local`/`local-fallback`), `remote_host`, `redact_level`, `egress_bytes`, `egress_sha256` (hash of the exact `state` sent, not the text), `model_revision`, `truncated`, `remote_ms`. This makes "what left the machine and when" answerable from the existing log without storing a second copy of the text. The log already holds the (truncated) situation locally; that is unchanged.

### 4.6 Server expectations: logging and retention

Required of any server the gate is pointed at. Written so a server operator can check each line.

- Does not write `state`, `question` or `probabilities` to any log, file, cache, queue or metrics system. Inputs live in memory for the duration of the request only.
- Logs, per request, metadata only: timestamp, `kid`, source address, `request_id`, item count, token counts, HTTP status, `server_ms`, `model_revision`. 14 day retention, then deleted.
- No request body in access logs, error logs or traces. Debug logging of bodies is not available as a switch.
- Core dumps and swap disabled or encrypted for the service user; crash reports do not include request memory.
- No forwarding to any third party and no use of inputs for training, evaluation or caching.
- No persistent cache of scored inputs. (An in-memory cache keyed by hash of `state` is allowed only if it expires within 60 s.)
- Runs as an unprivileged user, listens only on the address in its config, no other services on the same port.
- The scope of this design is a server the owner controls. A third-party hosted inference service is out of scope here: it would send operator data to a company, which needs its own privacy and data-processing review (D8).

## 5. Failure semantics

"Fail closed" is ambiguous in this gate, so it is spelled out. For an escalation decision, closed (do nothing) means a swallowed event. The stated cost is that a swallowed event that mattered costs ten times an unnecessary interruption, so the safe direction is toward the operator. For an affirmation, safe is "not affirmed". For a response, safe is the standing policy. The gate already encodes this for a local model failure:

- `classify`: classifier unavailable gives `lane 3, escalate True, enforced False`.
- `respond`: standing policy for the state.
- `affirm`: `risk 1.0`, not affirmed.

### 5.1 Options when the remote cannot be used

(Unreachable, timeout, circuit open, 5xx, invalid body, auth/pin/revision failure.)

| Option | Behaviour | For | Against |
|---|---|---|---|
| A. Local fallback | Use the local rung if one is installed. Else option C. | Keeps scores consistent with calibration when the local runtime is the calibrated one. Silent recovery. | Needs the local model resident, which is exactly what low rungs lack. A different local runtime would shift scores (6.2). |
| B. Fail closed to silence | Treat as "stay silent". | Quiet. | Swallows events during any outage. Violates the ten-times cost rule. Rejected. |
| C. Existing unavailable path | Same as a local model failure today (escalate / standing policy / not affirmed). | Already built, already tested, correct direction per question. | During a long outage lane 3 escalates every non-repeat event: noisy. Bounded by the repeat window and the quiet-hours hold, both of which run before the model call. |
| D. Queue and retry later | Hold the event, rescore on recovery. | Accurate scores. | Delays the event, adds state, and the daemon's 3 s budget has already passed. Rejected for v1. |

### 5.2 Recommendation

Default `local-then-standing`: A if a calibrated local model is present on this box, otherwise C. Never B or D. Specifically:

1. Remote failure with a local model present: score locally, mark the verdict `backend: local-fallback` and log it. Do not resend the same event to the remote.
2. Remote failure with no local model: take the existing unavailable path, with `rule: "classifier unavailable (remote: <class>)"` where `<class>` is `timeout`, `auth`, `revision`, `down`, and so on, so the failure class is visible in the log.
3. After 3 consecutive failures (the breaker, 2.5), send one operator notice of the existing `GATE-DOWN:` kind naming the remote and the class. A remote that is down for hours is news.
4. The reverse is not automatic: a failing local model does NOT cause a send to a remote that was not opted in. A box with `LAYA_GATE_BACKEND=local` never contacts a server.
5. Auth, pin and revision failures (3.4) latch and do not retry events, but follow 1-2 above for the event itself.
6. Daemon budget: remote total 1.5 s plus a local fallback inference must fit inside `FLEET_GATE_TIMEOUT`. If local inference is slower than the remaining budget, the daemon's own 3 s timeout fires and it shows the event, which is also the safe direction. When remote mode is enabled, raise `FLEET_GATE_TIMEOUT` to 5 s (D7).

## 6. Keeping the gate consistent between local and remote

### 6.1 What does not change

- Lane tables (`LANE1`, `LANE2`), the parked rule, stall logic, quiet hours and repeat windows run before any model call, in the gate, so remote cannot affect them.
- Thresholds are read from the same environment variables: 0.59 (`LAYA_GATE_CHOICE_THRESHOLD`), 0.55 (respond), 0.20 (affirm risk). The server has no copy of them and returns no verdict.
- The compared value is the same quantity: `probabilities["interrupt her now"]` from the same softmax with the same temperature (1.9064 for `choice:2`).
- Prompt assembly (doctrine, situation, event line) is done by the same functions. Remote swaps only the call `load_model().predict(state, q)` for `backend.score(state, q)`.
- The existing `ENFORCE` flag still gates whether lane 3 is enforced or only logged.

### 6.2 What can drift, and the guard

- Weights or runtime: a GGUF, ONNX or PyTorch server (see `03-licences.md` for what may be built) is a numerical reimplementation. FP16 vs FP32, different kernels and different tokenizer builds all move probabilities slightly. A move of 0.01 near 0.59 flips decisions.
- Redaction (4.3) changes the input text, so it changes the score even on identical weights.
- Truncation at 512 tokens depends on tokenization of the redacted text.

### 6.3 Parity gate before a server may be used

1. A golden set of at least 200 recorded `decide` situations (assembled from the decision log and the evals under `evals/`), each stored with the LOCAL probabilities from the calibrated model. Stored locally, not on the server.
2. `/v1/info.golden_digest` is the server's digest of its own scores for the same set; `--remote-check` compares gate-side.
3. Pass criteria, proposed: maximum absolute probability difference <= 0.005; zero threshold flips at 0.59 inside the band 0.57 to 0.61; zero flips for respond (0.55) and affirm (0.20) on their sets. Numbers are proposals (D6).
4. Run once per (server build, redaction level). A level other than R1 that passes parity is still a different input distribution: report, per level, how many of the 337 recorded decisions change side of 0.59.
5. Re-run on any change of `model_revision`, `weights_sha256` or `temperature_digest`. The gate sends `expect` on every call, so a silent server upgrade is a 409, not a drift.

## 7. Decisions needing owner sign-off

Recommended answer first in each line.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Is local-only the default at every rung, with remote strictly opt-in per box? | Yes. |
| D2 | Which questions may go remote? | `decide` only to start. `respond` and `affirm` separate, off by default (they carry the standing ruling, last messages and inbound instructions). |
| D3 | Default redaction level for remote. | R2, with R1 as the floor. R0 never remote. |
| D4 | Client and vendor names: a local blocklist file applied at R1 and up, and R3 (no free text) for any server not on a network the owner controls. | Yes, blocklist at R1+. R3 required for any non-owner network. |
| D5 | Certificate model: SPKI pin plus bearer for v1, mTLS deferred. | Pin + bearer. mTLS only if a second client machine or a shared server appears. |
| D6 | Parity thresholds: max probability delta 0.005, zero flips in the 0.57-0.61 band. | Accept as proposed, tighten after the first golden run. |
| D7 | Raise `FLEET_GATE_TIMEOUT` from 3 s to 5 s when remote is on. | Yes. |
| D8 | Third-party hosted inference (a company runs the server): out of scope, needs its own privacy review first. | Out of scope for v1. |
| D9 | `truncated: true` on a lane 3 call: log only, or treat the score as unavailable? | Log only in v1; revisit with data. |
| D10 | Failure policy: `local-then-standing` (A then C), never swallow. | Yes. |
| D11 | Token rotation interval, 90 days. | Yes. |
| D12 | Add `backend`, `model_revision`, `redact_level`, `egress_sha256` to the decision log. Changes the log schema (additive). | Yes. |

## 8. Out of scope here

No code, no server build, no GGUF/ONNX conversion (see `03-licences.md`), no change to `laya_gate.py`, `fleet_watch.py` or any threshold. Implementation is a separate task after D1-D12 are answered.
