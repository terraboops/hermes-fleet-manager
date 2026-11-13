"""Locked compare-and-swap registry snapshots shared by every registry writer.

Long-running launchers may retain snapshots, but must never publish stale ones.
Conflicts fail closed and leave the live session available for explicit recovery.
"""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import tempfile


class Snapshot(dict):
    original: bytes | None
    path: str


def _bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def load(path):
    raw = _bytes(path)
    data = Snapshot(json.loads(raw) if raw is not None else {'sessions': []})
    data.original = raw
    data.path = os.path.realpath(path)
    return data


def save(path, data):
    if not isinstance(data, Snapshot) or data.path != os.path.realpath(path):
        raise ValueError('registry write requires a snapshot from this path')
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _bytes(path) != data.original:
            raise ValueError('registry changed since read; reload and retry after review')
        raw = (json.dumps(data, indent=2) + '\n').encode()
        fd, tmp = tempfile.mkstemp(prefix=Path(path).name + '.', dir=Path(path).parent)
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            data.original = raw
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
