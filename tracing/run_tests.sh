#!/usr/bin/env bash
# Runs the vendored tapdash-langfuse plugin's tests. Run it as the `hermes` user on the test VM:
#   bash tracing/run_tests.sh [pytest args...]
#
# It uses the Hermes environment's own Python (the first one that can `import langfuse`), because that is
# the interpreter the plugin runs under. pytest is not in that environment, so it is installed once into a
# cache directory outside the repo and put on PYTHONPATH.
#
# 4 upstream tests (the CrossesHookBoundary class) are deselected: they re-exec the process through Hermes's
# dependency installer, which drops the test path from PYTHONPATH, so they cannot run from this checkout
# (spec D8). Everything else runs, with warnings as errors.
set -euo pipefail

PYTEST_VERSION="9.1.1"
HERMES_HOME_DIR="/home/hermes/.hermes"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$HERE/tapdash_langfuse"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}"
TARGET="$CACHE_ROOT/tapdash-langfuse-pytest"

# 1. Find the Hermes environment Python that has langfuse installed.
PY=""
for candidate in "$HERMES_HOME_DIR"/installs/*/environments/*/venv/bin/python; do
  [ -x "$candidate" ] || continue
  if "$candidate" -c 'import langfuse' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "run_tests.sh: no Hermes environment Python can import langfuse (looked in $HERMES_HOME_DIR/installs/*/environments/*/venv)" >&2
  exit 2
fi

# 2. Install pytest into the cache directory if it is not there (or is another version).
have_pytest() {
  PYTHONPATH="$TARGET" "$PY" -c "import sys, pytest; sys.exit(0 if pytest.__version__ == '$PYTEST_VERSION' else 1)" >/dev/null 2>&1
}
if ! have_pytest; then
  UV=""
  for candidate in "$HERMES_HOME_DIR"/tools/uv-*/uv; do
    [ -x "$candidate" ] && UV="$candidate"
  done
  if [ -z "$UV" ]; then
    echo "run_tests.sh: uv not found under $HERMES_HOME_DIR/tools/" >&2
    exit 2
  fi
  mkdir -p "$TARGET"
  # uv reads a config file from the current directory's parents/home; run from /tmp and ignore config.
  (cd /tmp && UV_NO_CONFIG=1 UV_CACHE_DIR="$CACHE_ROOT/tapdash-langfuse-uv" \
    "$UV" pip install --quiet --python "$PY" --target "$TARGET" "pytest==$PYTEST_VERSION")
fi

# 3. Run the tests.
cd "$PLUGIN_DIR"
TERM=dumb HERMES_AGENT_REPO="$HERMES_HOME_DIR/hermes-agent" \
  PYTHONPATH="$TARGET:$HERMES_HOME_DIR/hermes-agent" \
  exec "$PY" -m pytest -p no:cacheprovider -q -W error -k "not CrossesHookBoundary" "$@"
