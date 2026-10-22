#!/usr/bin/env bash
# Runs the vendored tapdash-langfuse plugin's tests. Run it as the `hermes` user on the test VM:
#   bash tracing/run_tests.sh [pytest args...]
#
# It uses the Python of the Hermes environment the running gateway uses (read from the gateway's memory map, which
# changes nothing), because that is the interpreter the plugin runs under. If the gateway is not running or the
# environment cannot be told, it takes the first Hermes environment that can `import langfuse`, in directory
# order, and says so. pytest is not in that environment, so it is installed once into a cache directory outside
# the repo and put on PYTHONPATH.
#
# 4 upstream tests (the CrossesHookBoundary class) are always deselected, whatever arguments you pass (with
# --deselect, which an extra -k or -m cannot replace): they re-exec the process through Hermes's dependency
# installer, which would run against the gateway's shared Hermes environment and drops the test path from
# PYTHONPATH, so they cannot run from this checkout (spec D8). Everything else runs, with warnings as errors.
set -euo pipefail

PYTEST_VERSION="9.1.1"
HERMES_HOME_DIR="/home/hermes/.hermes"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$HERE/tapdash_langfuse"
CACHE_ROOT="${XDG_CACHE_HOME:-$HOME/.cache}"
TARGET="$CACHE_ROOT/tapdash-langfuse-pytest"

# 1. Find the Hermes environment Python that has langfuse installed: first the gateway's active environment.
PY=""
GW_PID="$(XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" systemctl --user show hermes-gateway -p MainPID --value 2>/dev/null || true)"
if [ -n "$GW_PID" ] && [ "$GW_PID" != "0" ] && [ -r "/proc/$GW_PID/maps" ]; then
  GW_ENVS="$(grep -o '/environments/[0-9a-f]*/venv' "/proc/$GW_PID/maps" 2>/dev/null | sort -u || true)"
  if [ "$(printf '%s\n' "$GW_ENVS" | grep -c .)" = "1" ]; then
    for candidate in "$HERMES_HOME_DIR"/installs/*"$GW_ENVS"/bin/python; do
      [ -x "$candidate" ] || continue
      if "$candidate" -c 'import langfuse' >/dev/null 2>&1; then
        PY="$candidate"
        echo "run_tests.sh: using the gateway's active Hermes environment: $PY" >&2
      fi
      break
    done
  fi
fi
if [ -z "$PY" ]; then
  for candidate in "$HERMES_HOME_DIR"/installs/*/environments/*/venv/bin/python; do
    [ -x "$candidate" ] || continue
    if "$candidate" -c 'import langfuse' >/dev/null 2>&1; then
      PY="$candidate"
      echo "run_tests.sh: the gateway's environment could not be told; using the first Hermes environment with langfuse: $PY" >&2
      break
    fi
  done
fi
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
  exec "$PY" -m pytest -p no:cacheprovider -q -W error \
  --deselect tests/test_langfuse_plugin.py::TestSystemPromptCrossesHookBoundary "$@"
