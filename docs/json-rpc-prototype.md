# JSON-RPC / stream-json control channel — prototype findings (2026-08-29)

Goal (port idea #1): replace tmux paste + `capture-pane` with a structured
control channel, **without breaking `--remote-control`** (remote control is
critical).

## What we tested
`claude` v2.1.251. Three mooted "ACPs" clarified: `stream-json` is a **print-mode
serializer** (one-shot), `--remote-control` is the **interactive ACP channel**.
Verified both, and the key question: do they coexist?

## Findings
1. `--output-format stream-json` **requires `--verbose`** (else: "requires --verbose").
2. Invocation that works:
   `claude -p --output-format stream-json --verbose "<prompt>"`
   → emits valid structured events: `system/init` (has `session_id`, tool list),
     `assistant` (has `message.id`), `result` (has `total_cost_usd`, `stop_reason`, usage).
3. **Coexist with `--remote-control`?  YES.** Adding `--remote-control` to the
   print/stream invocation did not drop or break it — the same events stream.
   (Remote control is effectively inert alongside `-p`-print mode and preserved.)
4. Nuance this is a **one-shot print channel**: it spawns a fresh process, so it
   gives a structured reply per call but does NOT steer a live session's in-flight
   context. That makes it a parallel **control plane** (status asks / quick tasks,
   pointed at the session's cwd + config_dir + resume-uuid), NOT a replacement for
   live interactive steering.
5. Real-model caveat: a non-TTY subprocess must pass the prompt as an ARG (not
   stdin) and run with proper auth/pty; a throwaway config_dir errors synthetic
   (transport framing still validated).

## Recommendation
- Adopt `-p --output-format stream-json --verbose` as the **structured dispatch/reply
  channel** (a `fleet ask <session>` that parses `assistant.message`), replacing
  pane-scraping for parseable replies.
- Keep **main-process steering + tmux** for live sessions.
- Keep `--remote-control` for the IDE channel; it coexists (remote control preserved).
- Prefer the **status/`mid` (v1 contract)** envelope over stream-json events so the
  controller stays transport-agnostic.
