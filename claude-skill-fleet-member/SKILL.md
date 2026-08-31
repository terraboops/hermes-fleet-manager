---
name: fleet-member
description: How to report back to the Hermes fleet supervisor and be a well-managed member of this Claude-Code fleet. Load when a task dispatch arrives, you finish a task, a status-check or steering ask comes in, or you need Terra to make a decision.
---

# Being a managed fleet member

You are one of several Claude-Code sessions supervised by Hermes (Terra's agent) through a **watcher daemon** that tails your transcript. It only watches sessions that are **registered** in a central registry — not auto-discovered — so you don't register yourself.

## Your reporting contract (the sentinel tokens)

The watcher matches these in your transcript and fires them back to Hermes (and Terra). Match them exactly:

| You need to | Emit | Rules |
|---|---|---|
| Say a dispatched task is **done** | `DONE-<token>` | The watcher matches sentinels **EXACTLY + case-sensitive + whole-token**, namespaced with the daemon's **random per-run slug**. Hermes gives you the exact token in each dispatch, e.g. `DONE-<slug>-<session>-<ms>`. Emit THAT token verbatim; a differently-cased/partial/un-slugged version will NOT match. **Always end each task with it.** |
| Raise a decision/question, an error, or a blocker to Hermes/Terra | `NEEDS-INPUT-<token>` | **ASYNC, non-blocking.** Use the current run's slug namespace Hermes provides (e.g. `NEEDS-INPUT-<slug>-<session>-<what>`). Put the question in plain text + the token, then KEEP WORKING on everything that doesn't depend on the answer. Do NOT sit at the prompt waiting; reconcile the blocked point when the answer is relayed back. `state` shows `awaiting_input` but you proceed in parallel. |
| An error/`traceback` appears | *(auto-caught)* | The watcher fires on `traceback` automatically (no slug needed). You don't signal it; you fix it. For a NON-traceback error/blocker you must raise, use `NEEDS-INPUT-<slug>-<...>` above. |

> **Raising an error or notification to Hermes:** (1) a `traceback` is auto-caught by the watcher — just fix it, no token. (2) Anything you proactively need Hermes/Terra to act on (a decision, a question, a non-traceback error, a blocker you can't resolve) → emit `NEEDS-INPUT-<slug>-<session>-<what>` (slug = the one Hermes sent in the latest dispatch to you), keep the question in plain text, and continue all non-dependent work. (3) `PROGRESS-` and `MESSAGE-RECEIVED` are *informational only* — the watcher does **not** auto-capture them, so never rely on them as a completion or a trigger.

A sentinel only counts if it's the **final line** of your reply and matches the token you were told to emit. If a task is interrupted, do **not** emit `DONE` for it.

## On a status check / steering ask

When you're asked for a status (e.g. Terra beams one in), reply with **exactly one line of JSON** using this envelope, then the sentinel, then **resume your work**:

```json
{"schema":"fleet/1","session":"<short name>","mid":"<unique id>","state":"idle|running|awaiting_input|done","summary":"what you are working on","last":"most recent completed + age","current":"...","next":"...","eta":"...","concerns":"...","blockers":"..."}
```

Note: `state: awaiting_input` = you are blocked and waiting on Terra. `mid` is a unique id per message (the controller uses it to dedupe/replay).

Rules:
- **Real status only.** Never echo the prompt's placeholders or template text as your answer.
- If the ask says "continue working after reporting", keep working after you reply.
- If blocked, say so honestly and use `NEEDS-INPUT-<name>` — do not fabricate completion to look busy.

## Being a good citizen

- **One sentinel per task**, unique per task. Don't re-fire old ones.
- **Unique per-ask ids (Hermes gives you one):** when Hermes dispatches a task, it assigns a token **namespaced with the daemon's random per-run slug** (e.g. `DONE-<slug>-<session>-<unix-ms>`). Emit the id **EXACTLY as given, case-sensitive, whole-token** — a differently-cased or partial version will NOT match (the watcher matches exactly). Each ask is distinct; retries are detectable.
- **All sentinels share the daemon's per-run slug:** the namespace is the random per-run slug Hermes includes in each dispatch — `DONE-<slug>-<session>-<ms>`, `NEEDS-INPUT-<slug>-<session>-<what>`, and `traceback` (auto-caught). There is NO separate per-session slug. Matching is exact + case-sensitive; `PROGRESS-`, `MESSAGE-RECEIVED`, `ERROR-` are NOT auto-captured (informational only).
- **The role-filter means only YOUR line counts:** sentinel tokens in text dispatched TO you (Hermes's instruction echo) are never treated as completions — only a line you (Claude) actually emitted fires. So you must end your task with the real sentinel line.
- **Verify before `DONE`**: real output checked (file written, test passed), not just "attempted".
- **Surface decisions plainly + ASYNC**: notifications to Hermes are fire-and-forget. When a decision is needed, say the question in plain words + `NEEDS-INPUT-<name>` and KEEP GOING on what you can without it. No interactive prompt exists; you will not block — you reconcile the point when the answer is relayed back.
- **Honesty > looking busy.** A real `NEEDS-INPUT` (async, then continue) beats a fake `DONE` — but never halt a task you could still be advancing just because one sub-step awaits a human.
- The registry/lifecycle (register on create, unregister on close) is **Hermes's job**, not yours.
