#!/usr/bin/env python3
"""Queue one verified dispatch to an existing Codex fleet thread."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = Path.home() / ".hermes/scripts/cc-watch"
REGISTRY = RUNTIME / "fleet_registry.json"
STATE = RUNTIME / "fleet_state.py"
ACK = RUNTIME / "fleet_ack.py"
MARKER_RE = re.compile(r"DISPATCH-[A-Za-z0-9_.-]+-[0-9]{10,}")
QUEUED_RE = re.compile(r"Queued message [0-9a-f-]+ for thread ([0-9a-f-]+)\.")
MAX_MESSAGE_BYTES = 64 * 1024


def registry_entry(name: str, registry_path: Path = REGISTRY) -> dict:
    data = json.loads(registry_path.read_text())
    rows = data.get("sessions", [])
    for entry in rows:
        if entry.get("name") == name:
            return entry
    raise ValueError(f"no registry entry for {name}")


def dispatch(name: str, payload_path: Path, timeout: float = 60,
             registry_path: Path = REGISTRY, state_path: Path = STATE,
             ack_path: Path = ACK, runner=subprocess.run,
             sleep=time.sleep, now=time.monotonic) -> str:
    entry = registry_entry(name, registry_path)
    if entry.get("provider") != "codex":
        raise ValueError(f"{name} is not explicitly registered as Codex")
    thread_id = entry.get("uuid")
    try:
        if not isinstance(thread_id, str) or str(uuid.UUID(thread_id)) != thread_id.lower():
            raise ValueError
    except ValueError:
        raise ValueError("Codex registry entry has no valid native thread UUID") from None
    cwd = entry.get("cwd")
    if not cwd or not Path(cwd).is_dir():
        raise ValueError("Codex working directory is missing or unavailable")
    body = payload_path.read_text(errors="replace")
    if not body.strip():
        raise ValueError("EMPTY-FILE")
    marker_match = MARKER_RE.search(body)
    marker = marker_match.group(0) if marker_match else f"DISPATCH-{entry.get('short') or name}-{int(time.time() * 1000)}"
    message = body if marker_match else f"{body.rstrip()}\n\n[{marker}]"
    if len(message.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError(f"CODEX-PAYLOAD-TOO-LARGE ({len(message.encode())} bytes; limit {MAX_MESSAGE_BYTES})")

    try:
        state = runner([sys.executable, str(state_path), name], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"NOT-READY: could not establish Codex state ({exc})") from exc
    fingerprint = state.stdout.strip()
    if state.returncode or not fingerprint.startswith(("IDLE|", "WORKING|")):
        raise ValueError(f"NOT-READY: Codex state is not dispatchable ({fingerprint or state.stderr.strip()})")

    codex_home = os.path.expanduser(entry.get("codex_home") or "~/.codex")
    env = dict(os.environ, CODEX_HOME=codex_home)
    command = ["codex", "queue", "--thread", thread_id, "--message", message, "--cd", cwd]
    try:
        result = runner(command, capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"UNCERTAIN: codex queue outcome unknown; do not retry ({exc})") from exc
    response = (result.stdout or "") + (result.stderr or "")
    queued = QUEUED_RE.search(response)
    if result.returncode or not queued or queued.group(1) != thread_id:
        raise ValueError(f"UNCERTAIN: Codex queue did not confirm the target thread; do not retry ({response.strip()})")

    deadline = now() + timeout
    while now() < deadline:
        try:
            receipt = runner([sys.executable, str(ack_path), "delivered", name, marker],
                             capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"UNCERTAIN: transcript receipt could not be read for {marker}; do not retry ({exc})") from exc
        if receipt.returncode == 0:
            return f"LANDED {marker}"
        if receipt.returncode != 1:
            raise ValueError(f"UNCERTAIN: transcript receipt could not be read for {marker}; do not retry")
        sleep(min(2, max(0, deadline - now())))
    raise ValueError(f"UNCERTAIN: Codex accepted the queue message but transcript receipt is pending for {marker}; do not retry")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    parser.add_argument("payload")
    parser.add_argument("timeout", nargs="?", type=float,
                        default=float(os.environ.get("FLEET_DELIVERY_TIMEOUT_S", "60")))
    args = parser.parse_args(argv)
    try:
        print(dispatch(args.session, Path(args.payload), args.timeout))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 3 if str(exc).startswith("UNCERTAIN:") else 1


if __name__ == "__main__":
    raise SystemExit(main())
