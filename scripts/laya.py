#!/usr/bin/env python3
"""Laya tooling entry point.

    laya.py parity --backend replay          run a scoring backend over the parity fixtures
    laya.py doctor [--dry-run]               hardware, chosen backend, and why
    laya.py serve --backend NAME             host a local backend over HTTP (docs/laya-remote.md)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
import laya_doctor  # noqa: E402
import laya_parity  # noqa: E402
import laya_serve  # noqa: E402


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "parity":
        return laya_parity.main(argv[1:])
    if argv and argv[0] == "doctor":
        return laya_doctor.main(argv[1:])
    if argv and argv[0] == "serve":
        return laya_serve.main(argv[1:])
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
