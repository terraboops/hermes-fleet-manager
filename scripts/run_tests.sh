#!/bin/sh
# The test entry point. The suite runs under the hermes-agent venv (Python 3.11+): the kanban
# intake tests import hermes_cli, which does not import under the system python 3.9, and a bare
# `python3 -m unittest` there errors instead of testing. Override with FLEET_TEST_PYTHON.
#
#   scripts/run_tests.sh                      the whole suite
#   scripts/run_tests.sh tests.test_intake    any unittest arguments pass through
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
home=${HERMES_HOME:-$HOME/.hermes}
py=${FLEET_TEST_PYTHON:-$home/hermes-agent/venv/bin/python}
if [ ! -x "$py" ]; then
    echo "run_tests: no interpreter at $py (set FLEET_TEST_PYTHON to a Python 3.11+ with hermes_cli importable)" >&2
    exit 2
fi
"$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "run_tests: $py is older than Python 3.11" >&2
    exit 2
}
cd "$here"
if [ "$#" -eq 0 ]; then
    set -- discover -s tests -t .
fi
exec "$py" -m unittest "$@"
