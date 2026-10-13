"""Idempotent capture of inbound messages as triage cards, before any model runs.

Order of operations for one message:
  1. filter (internal, unauthorized, slash command, clarify/approval reply -> no card)
  2. append a receipt to an fsynced journal            (receipt is durable)
  3. create the triage card under a cross-process lock (idempotency_key dedupes)
  4. append a commit line to the journal

A crash after step 2 leaves a receipt without a commit line. ``recover()`` replays
those; because step 3 is keyed, replay and platform redelivery both land on the same card.

The feature is off unless ``intake.enabled`` is true in config.yaml
(or HERMES_INTAKE_ENABLED=1). Capture never sends anything to the chat.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

TRUTHY = {"1", "true", "yes", "on"}


def intake_enabled(config_path: Optional[Path] = None) -> bool:
    env = os.environ.get("HERMES_INTAKE_ENABLED")
    if env is not None:
        return env.strip().lower() in TRUTHY
    path = config_path or (Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml")
    try:
        import yaml
        cfg = yaml.safe_load(Path(path).read_text()) or {}
        return bool((cfg.get("intake") or {}).get("enabled", False))
    except Exception:
        return False  # unreadable config means off


@dataclass
class IntakeEvent:
    platform: str
    chat_id: str
    message_id: Optional[str]
    text: str = ""
    sender: str = ""
    internal: bool = False
    authorized: bool = True
    awaiting_reply: bool = False   # clarify / approval answer


def intake_key(ev: IntakeEvent) -> str:
    if ev.message_id:
        return f"intake:{ev.platform}:{ev.chat_id}:{ev.message_id}"
    h = hashlib.sha256((ev.text or "").encode()).hexdigest()[:16]
    return f"intake:{ev.platform}:{ev.chat_id}:h:{h}"


def skip_reason(ev: IntakeEvent) -> Optional[str]:
    if ev.internal:
        return "internal"
    if not ev.authorized:
        return "unauthorized"
    if (ev.text or "").lstrip().startswith("/"):
        return "command"
    if ev.awaiting_reply:
        return "reply"
    return None


class Intake:
    def __init__(self, kb, connect: Callable[[], Any], state_dir: Path, assignee: str = "default"):
        self.kb, self.connect, self.assignee = kb, connect, assignee
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal = self.dir / "intake-receipts.jsonl"
        self.lockfile = self.dir / "intake.lock"

    # -- journal ---------------------------------------------------------
    def _append(self, rec: dict) -> None:
        fd = os.open(self.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _records(self) -> list[dict]:
        if not self.journal.exists():
            return []
        out = []
        for ln in self.journal.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue  # torn final line from a kill mid-write; the receipt was never acked
        return out

    # -- card write ------------------------------------------------------
    def _write_card(self, key: str, ev: dict) -> str:
        title = (ev.get("text") or "").strip().splitlines()[0][:80] if (ev.get("text") or "").strip() else "(voice or media message)"
        body = (f"Inbound {ev['platform']} message {ev.get('message_id') or '-'} "
                f"in chat {ev['chat_id']}.\n\n{ev.get('text') or ''}")
        # flock makes lookup+insert atomic across processes; create_task alone can race.
        with open(self.lockfile, "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            conn = self.connect()
            try:
                return self.kb.create_task(conn, title=title, body=body, triage=True,
                                           created_by="intake", idempotency_key=key)
            finally:
                conn.close()

    def capture(self, ev: IntakeEvent, _fault: Optional[Callable[[str], None]] = None) -> Optional[str]:
        """Return the card id, or None when filtered. ``_fault`` is a test seam called at named points."""
        if skip_reason(ev):
            return None
        key = intake_key(ev)
        payload = {"platform": ev.platform, "chat_id": ev.chat_id, "message_id": ev.message_id,
                   "text": ev.text, "sender": ev.sender}
        self._append({"k": "receipt", "key": key, "ev": payload})
        if _fault:
            _fault("after_receipt")
        tid = self._write_card(key, payload)
        if _fault:
            _fault("after_card")
        self._append({"k": "commit", "key": key, "card": tid})
        return tid

    def recover(self) -> list[str]:
        """Replay receipts that have no commit line. Safe to run repeatedly."""
        recs = self._records()
        committed = {r["key"] for r in recs if r["k"] == "commit"}
        done = []
        for r in recs:
            if r["k"] == "receipt" and r["key"] not in committed:
                tid = self._write_card(r["key"], r["ev"])
                self._append({"k": "commit", "key": r["key"], "card": tid})
                committed.add(r["key"])
                done.append(tid)
        return done

    # -- promotion -------------------------------------------------------
    def promote(self, card_id: str, assignee: Optional[str] = None) -> bool:
        """triage -> ready (todo while a parent is unfinished), assigned, for the existing dispatcher."""
        conn = self.connect()
        try:
            return bool(self.kb.specify_triage_task(conn, card_id, assignee=assignee or self.assignee))
        finally:
            conn.close()
