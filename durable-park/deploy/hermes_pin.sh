#!/usr/bin/env bash
# Keep Hermes on the pinned version (GitHub #76). Run as the `hermes` user.
#
#   hermes_pin.sh check    -> is Hermes on the pinned commit, with updates blocked?  (exit 0 = yes)
#   hermes_pin.sh block    -> turn the update block on, and the passive update check off
#   hermes_pin.sh unblock  -> turn the update block off (only while you upgrade on purpose)
#
# How the block works: Hermes on Linux updates only with `git fetch` from github.com/NousResearch
# (`hermes update`, the dashboard's Update button and the `/update` chat command all run it).
# The block points git's proxy for that address at 127.0.0.1:9, where nothing listens, so every
# fetch fails before anything changes. It lives in the checkout's own .git/config.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PIN_FILE="$HERE/hermes-pin.json"
CHECKOUT="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
KEY="http.https://github.com/NousResearch/.proxy"
VALUE="http://127.0.0.1:9"

pinned_commit() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$PIN_FILE"; }
pinned_version() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$PIN_FILE"; }

case "${1:-}" in
  check)
    ok=0
    head="$(git -C "$CHECKOUT" rev-parse HEAD)"
    if [ "$head" = "$(pinned_commit)" ]; then
      echo "OK   Hermes is on the pinned $(pinned_version) ($head)"
    else
      echo "FAIL Hermes is at $head ($(git -C "$CHECKOUT" describe --tags --always HEAD)), pinned is $(pinned_version)"; ok=1
    fi
    if [ "$(git -C "$CHECKOUT" config --get "$KEY" || true)" = "$VALUE" ]; then
      echo "OK   update block is on (git cannot fetch from NousResearch)"
    else
      echo "FAIL update block is off: run '$0 block'"; ok=1
    fi
    if [ "$(hermes config get updates.check 2>/dev/null | tail -1)" = "false" ]; then
      echo "OK   passive update check is off"
    else
      echo "FAIL passive update check is on: run '$0 block'"; ok=1
    fi
    exit "$ok"
    ;;
  block)
    git -C "$CHECKOUT" config "$KEY" "$VALUE"
    git -C "$CHECKOUT" config tapdash.pinned "$(pinned_version) - updates blocked on purpose. See TapDash-Agents durable-park/docs/runbook.md 'Upgrade Hermes'"
    hermes config set updates.check false >/dev/null
    echo "Update block is ON. Check: $0 check"
    ;;
  unblock)
    for key in "$KEY" tapdash.pinned; do
      rc=0; git -C "$CHECKOUT" config --unset-all "$key" || rc=$?
      # 5 = the key was not set (already off); any other failure must stop here, never be hidden
      if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then
        echo "FAIL could not remove $key (git exit $rc); the update block may still be on" >&2; exit 1
      fi
    done
    if git -C "$CHECKOUT" config --get "$KEY" >/dev/null; then
      echo "FAIL the update block is still set" >&2; exit 1
    fi
    echo "Update block is OFF. The sweep will alert until you re-pin: update deploy/hermes-pin.json, then '$0 block'."
    ;;
  *)
    echo "usage: $0 check|block|unblock" >&2; exit 2 ;;
esac
