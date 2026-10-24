"""Point every gate log at a throwaway directory before any module under test is imported.

The gate and the watcher read their log paths from the environment at import time. Without this,
a test that reaches classify(), respond() or the watcher's decision writer appends fixture rows
to the real logs under ~/.hermes/logs, and the morning digest then reports events that never
happened. Forced, not setdefault: an inherited production path must not survive into a test run.
"""
import atexit
import os
import shutil
import tempfile

_ISOLATED = tempfile.mkdtemp(prefix="fleet-tests-")
atexit.register(shutil.rmtree, _ISOLATED, ignore_errors=True)
os.environ["LAYA_GATE_HELD"] = os.path.join(_ISOLATED, "held.jsonl")
os.environ["LAYA_GATE_DECISIONS"] = os.path.join(_ISOLATED, "decisions.jsonl")
os.environ["LAYA_GATE_LABELS"] = os.path.join(_ISOLATED, "labels.jsonl")
os.environ["LAYA_GATE_REVIEW_DIR"] = _ISOLATED
os.environ["FLEET_GATE_LOG"] = os.path.join(_ISOLATED, "gate-decisions.jsonl")
