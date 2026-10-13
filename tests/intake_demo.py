#!/usr/bin/env python3
"""Run log: a MessageEvent goes through the plugin hook, becomes a card, a worker completes it.
Scratch board only. Run with the hermes venv."""
import importlib, os, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests" / "kanban_reliability"))
import harness as H
tmp = Path(tempfile.mkdtemp(prefix="intake-demo-")); (tmp / "out").mkdir()
ctx = H.Ctx("demo", tmp, tmp / "out")
os.environ["HERMES_HOME"] = str(tmp)           # state dir + config live in the scratch dir
os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "2"; os.environ["H_ADOPT"] = "1"
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.config import Platform
sys.path.insert(0, str(ROOT / "intake")); plug = importlib.import_module("plugin")
def log(s): print(time.strftime("%H:%M:%S"), s, flush=True)
src = SessionSource(platform=Platform.TELEGRAM, chat_id="1001", user_id="42")
ev = MessageEvent(text="please renew the domain before friday", source=src, message_id="9001")
log("flag unset -> hook must do nothing")
plug._on_dispatch(event=ev, gateway=None)
log(f"cards on board: {len(ctx.conn().execute('select id from tasks').fetchall())}")
os.environ["HERMES_INTAKE_ENABLED"] = "1"
log("flag on; message 9001 delivered")
plug._on_dispatch(event=ev, gateway=None)
log("message 9001 redelivered")
plug._on_dispatch(event=ev, gateway=None)
rows = ctx.conn().execute("select id,status,title,idempotency_key from tasks").fetchall()
log(f"cards on board: {len(rows)} -> {[tuple(r) for r in rows]}")
tid = rows[0][0]
from intake import Intake
it = Intake(ctx.kb, ctx.kbc.connect, tmp / "state" / "intake")
log(f"promote -> {it.promote(tid)}; status={ctx.row(tid)['status']}")
ctx.spawn_dispatcher(H_TTL=900)
ok = ctx.wait(lambda: ctx.row(tid)["status"] == "done", 40)
log(f"dispatcher claimed and worker finished: done={ok}; events={ctx.events(tid)}")
log(f"executions={len(ctx.starts(tid))} runs={ctx.runs(tid)}")
ctx.cleanup()
