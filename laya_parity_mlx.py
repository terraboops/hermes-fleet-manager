"""MLX scoring backend for `laya parity`: the production lane-3 path as a score(texts) callable.

Mirrors laya_gate.classify: state = DOCTRINE + Situation + Event + 24h count, asked QUESTION via
laya_mlx `.predict`; the score is P("interrupt her now"). Fixtures keep only the Event string, so
the Situation is the gate's own no-detail default and the 24h count is 0 unless `prior` is given.

    laya.py parity --backend laya_parity_mlx:score     (run under the laya-mlx venv)
"""
import importlib.metadata
import hashlib
import os

import laya_gate

LABEL = "interrupt her now"
_agent = None


def _load():
    global _agent
    if _agent is None:
        import laya_mlx
        _agent = laya_mlx.load(laya_gate.MODEL)
    return _agent


def build_state(event, prior=0, context=None):
    return (f"{laya_gate.DOCTRINE}\n\n"
            f"Situation: {context or 'no further detail available'}\n"
            f"Event: {event}\n"
            f"Events of this kind on this session in the last 24 hours: {prior}.")


def score(texts, priors=None):
    agent = _load()
    out = []
    for i, t in enumerate(texts):
        res = agent.predict(build_state(t, priors[i] if priors else 0), laya_gate.QUESTION)
        ans = res.get("answers", res).get("action", {})
        out.append(float((ans.get("probabilities") or {}).get(LABEL) or ans.get("confidence") or 0.0))
    return out


def environment():
    import glob
    snap = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--%s/snapshots/*" % laya_gate.MODEL.replace("/", "--")))
    weights = os.path.join(snap[0], "model.safetensors") if snap else None
    h = None
    if weights:
        d = hashlib.sha256()
        with open(weights, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                d.update(chunk)
        h = d.hexdigest()
    ver = {}
    for pkg in ("mlx", "laya-mlx", "laya_mlx"):
        try:
            ver[pkg] = importlib.metadata.version(pkg)
        except Exception:
            pass
    return {"model": laya_gate.MODEL, "weights_path": weights, "weights_sha256": h, "versions": ver}


def _priors(rows):
    import re
    out = []
    for r in rows:
        m = re.search(r"prior_24h:(\d+)", " ".join(r.get("tags", [])))
        out.append(int(m.group(1)) if m else 0)
    return out


def score_rows(texts, rows=None):
    """Fixture-aware scorer: the logged 24h count is part of the production input, so use it."""
    return score(texts, priors=_priors(rows) if rows else None)


score_rows.wants_rows = True
