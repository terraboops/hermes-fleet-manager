#!/bin/sh
# Point this clone at the repository's tracked hooks.
set -eu
cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/pre-push scripts/check_private.py
git config core.hooksPath .githooks
echo "hooks enabled: .githooks/pre-push"
echo "denylist: \${HFM_DENYLIST:-~/.config/hermes-fleet-manager/denylist.txt} (kept outside the repo; one regex per line)"
