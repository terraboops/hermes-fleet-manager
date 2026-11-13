#!/usr/bin/env python3
"""Fail when private detail is about to be published.

Two rules:
  1. A home-directory path of a real account (/Users/<name>, -Users-<name>) outside a short
     list of placeholder names. Always on.
  2. A private denylist: one case-insensitive regex per line ('#' starts a comment), read from
     $HFM_DENYLIST or ~/.config/hermes-fleet-manager/denylist.txt. The file lives OUTSIDE the
     repository on purpose; without it only rule 1 runs (the CI case).

Modes:
  check_private.py --tree             every tracked file
  check_private.py --range A..B       only lines ADDED by commits in the range (and their messages)
  check_private.py --pre-push         read git's pre-push stdin; scan what the push would publish
  check_private.py FILE...            the named files
Exit 1 when anything matches. Matches print as path:line: rule, with the offending text masked
to its rule name, so the output itself never repeats the private value.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

PLACEHOLDERS = r"(?:yourname|username|user|name|me|you|someone|example|shared|Shared|Guest|Library)\b"
HOME_RE = re.compile(r"(?:/Users/|-Users-)(?!" + PLACEHOLDERS + r")[A-Za-z][A-Za-z0-9._-]*")
SELF = "scripts/check_private.py"


def load_denylist():
    path = Path(os.environ.get("HFM_DENYLIST") or Path.home() / ".config/hermes-fleet-manager/denylist.txt")
    rules = []
    if path.is_file():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if line and not line.startswith("#"):
                rules.append((f"denylist:{n}", re.compile(line, re.I)))
    return rules


def scan_text(label, text, rules, out):
    for i, line in enumerate(text.splitlines(), 1):
        if HOME_RE.search(line):
            out.append(f"{label}:{i}: home-path")
        for name, rx in rules:
            if rx.search(line):
                out.append(f"{label}:{i}: {name}")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, errors="replace").stdout


def scan_range(rev_range, rules, out):
    for sha in git("rev-list", rev_range).split():
        scan_text(f"{sha[:8]} (message)", git("log", "-1", "--format=%B", sha), rules, out)
        diff = git("show", "--format=", "-U0", "--no-color", sha)
        path = None
        for ln in diff.splitlines():
            if ln.startswith("+++ b/"):
                path = ln[6:]
            elif ln.startswith("+") and not ln.startswith("+++") and path != SELF:
                scan_text(f"{sha[:8]} {path}", ln[1:], rules, out)


def main(argv):
    rules = load_denylist()
    out = []
    if argv[:1] == ["--tree"]:
        for f in git("ls-files").splitlines():
            if f == SELF:
                continue
            try:
                scan_text(f, Path(f).read_text(errors="strict"), rules, out)
            except (UnicodeDecodeError, OSError):
                pass
    elif argv[:1] == ["--range"] and len(argv) == 2:
        scan_range(argv[1], rules, out)
    elif argv[:1] == ["--pre-push"]:
        zero = "0" * 40
        for ln in sys.stdin.read().splitlines():
            parts = ln.split()
            if len(parts) < 4 or parts[2] == zero:
                continue  # deleting a ref publishes nothing
            local, remote = parts[1], parts[3]
            if remote == zero:
                scan_range_new(local, rules, out)
            else:
                scan_range(f"{remote}..{local}", rules, out)
    else:
        for f in argv:
            scan_text(f, Path(f).read_text(errors="replace"), rules, out)
    if out:
        print("check_private: private detail would be published:", file=sys.stderr)
        print("\n".join(out[:50]), file=sys.stderr)
        if len(out) > 50:
            print(f"... and {len(out) - 50} more", file=sys.stderr)
        return 1
    return 0


def scan_range_new(local, rules, out):
    """A new remote ref: scan every commit not already on a remote-tracking ref."""
    shas = git("rev-list", local, "--not", "--remotes").split()
    for sha in shas:
        scan_range(f"{sha}^!", rules, out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
