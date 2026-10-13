"""Hermes plugin: capture inbound messages as triage cards via pre_gateway_dispatch.

Fail-open: any error is logged and the message is dispatched normally.
Limitation: pre_gateway_dispatch does not fire while a turn is running (busy path); that
needs the message_received hook described in docs/intake-queue-design.md.
"""
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("intake")
_ROOT = str(Path(__file__).resolve().parents[2])


def _build():
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from intake import Intake
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return Intake(kb, kbc.connect, home / "state" / "intake")


def _on_dispatch(event=None, gateway=None, **_):
    try:
        if _ROOT not in sys.path:
            sys.path.insert(0, _ROOT)
        from intake import IntakeEvent, intake_enabled
        if not intake_enabled():
            return None
        src = event.source
        authorized = True
        check = getattr(gateway, "_is_user_authorized", None)
        if check:
            authorized = bool(check(src))
        ev = IntakeEvent(
            platform=getattr(getattr(src, "platform", None), "value", str(getattr(src, "platform", ""))),
            chat_id=str(getattr(src, "chat_id", "")),
            message_id=str(getattr(event, "message_id", "") or "") or None,
            text=getattr(event, "text", "") or "",
            sender=str(getattr(src, "user_id", "") or ""),
            internal=bool(getattr(event, "internal", False)),
            authorized=authorized,
        )
        _build().capture(ev)
    except Exception as exc:  # fail open
        log.warning("intake capture failed: %s", exc)
    return None  # never skip, never rewrite


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
