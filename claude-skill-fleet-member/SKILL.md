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
| Say a dispatched task is **done** | `DONE-<NAME>` | Case-sensitive uppercase `DONE-`. NAME = letters/digits/`. _ : -` e.g. `DONE-PRIDE.SWEEP`. **Always end each task with one.** |
| Ask Terra to **decide/act** (blocked) | `NEEDS-INPUT-<NAME>` | Use when you're blocked on a human decision or action. Put the question in plain text too (there is no pop-up for it). |
| Ack a dispatch (optional) | `MESSAGE-RECEIVED` | Preamble only; never a substitute for `DONE`. |
| An error/`traceback` appears | *(auto-caught)* | The watcher fires on `traceback` automatically. You don't signal it; you fix it. |

A sentinel only counts if it's the **final line** of your reply and matches the token you were told to emit. If a task is interrupted, do **not** emit `DONE` for it.

## On a status check / steering ask

When you're asked for a status (e.g. Terra beams one in), reply with **exactly one line of JSON** using this envelope, then the sentinel, then **resume your work**:

```json
{"session":"<short name>","state":"working|idle|parked","summary":"what you are working on","last":"most recent completed + age","current":"...","next":"...","eta":"...","concerns":"...","blockers":"..."}
```

Rules:
- **Real status only.** Never echo the prompt's placeholders or template text as your answer.
- If the ask says "continue working after reporting", keep working after you reply.
- If blocked, say so honestly and use `NEEDS-INPUT-<name>` — do not fabricate completion to look busy.

## Being a good citizen

- **One sentinel per task**, unique per task. Don't re-fire old ones.
- **Verify before `DONE`**: real output checked (file written, test passed), not just "attempted".
- **Surface decisions plainly**: your work is managed, so when we need Terra, say the question in plain words + `NEEDS-INPUT`. Assume no interactive prompt exists.
- **Honesty > looking productive.** A real blocker with `NEEDS-INPUT` beats a fake `DONE`.
- The registry/lifecycle (register on create, unregister on close) is **Hermes's job**, not yours.
