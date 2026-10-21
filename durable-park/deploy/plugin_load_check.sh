#!/usr/bin/env bash
# Fail the hermes-gateway start when the durable-park plugin did not load in THIS gateway process (drop-in
# hermes-gateway-policy.conf runs it as ExecStartPost=, with no leading "-": a non-zero exit stops the unit).
# Hermes drops all of a plugin's registrations and carries on with no pre_tool_call hook when the plugin fails to
# import or overruns plugins.load_timeout_seconds, and then every tool call would run unchecked. The plugin writes
# plugin_load.json (pid, mode, hooks) as the last step of a successful load, in the gateway process only. Here we
# poll once a second for a proof whose pid is $MAINPID (set by systemd for ExecStartPost), and give up only once
# DURABLE_PARK_LOAD_TIMEOUT seconds (default 30) of real time have passed since the first check: a microsecond
# deadline from $EPOCHREALTIME, not whole-second $SECONDS, which can end the wait up to 1 s early. The proof is
# checked once more at or after the deadline. Mode refusing-* counts as loaded: the plugin is live and refusing, as
# designed. Never prints secrets.
set -u

HERMES_HOME=${HERMES_HOME:-${HOME:-/nonexistent}/.hermes}
PROOF=${DURABLE_PARK_PLUGIN_LOAD:-$HERMES_HOME/plugin-data/durable-park/plugin_load.json}
TIMEOUT=${DURABLE_PARK_LOAD_TIMEOUT:-30}
MAINPID=${MAINPID:-}
PYTHON=${DURABLE_PARK_PYTHON:-/usr/bin/python3}  # overridable for tests

if [[ ! $TIMEOUT =~ ^[0-9]+$ ]]; then
    TIMEOUT=30
fi

# Prints "<mode> <pid>" and exits 0 when $1 is a proof for pid $2; exits 1 otherwise (missing, unreadable,
# malformed, other pid, unknown mode).
proof_for_pid() {
    timeout 5 "$PYTHON" -I -c 'import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        proof = json.load(handle)
    pid, mode = proof["pid"], proof["mode"]
    ok = (type(pid) is int and pid == int(sys.argv[2])
          and mode in ("asking", "refusing-policy", "refusing-error"))
except (OSError, ValueError, KeyError, TypeError):
    ok = False
if not ok:
    sys.exit(1)
print(mode, pid)' "$1" "$2" 2>/dev/null
}

# Sets NOW_US to the wall clock in whole microseconds ($EPOCHREALTIME, bash 5+; the sub-second separator is
# locale-dependent, so strip either "." or ","). Falls back to GNU date for an older bash.
now_us() {
    if [[ -n ${EPOCHREALTIME:-} ]]; then
        NOW_US=${EPOCHREALTIME/[.,]/}
    else
        NOW_US=$(date +%s%N)
        NOW_US=${NOW_US%???}
    fi
}

main() {
    local found deadline NOW_US
    now_us
    deadline=$((NOW_US + 10#$TIMEOUT * 1000000))  # 10#: "08" is not octal
    if [[ $MAINPID =~ ^[1-9][0-9]*$ ]] && command -v "$PYTHON" >/dev/null 2>&1; then
        while :; do
            if found=$(proof_for_pid "$PROOF" "$MAINPID"); then
                printf 'durable-park plugin loaded (mode=%s, pid=%s)\n' "${found%% *}" "${found##* }"
                return 0
            fi
            now_us
            if [[ $NOW_US -ge $deadline ]]; then
                break
            fi
            sleep 1
        done
    fi
    printf 'durable-park ALERT: the plugin did not load in this gateway (pid %s); stopping the gateway so no tool call runs unchecked\n' "$MAINPID"
    return 1
}

main
exit $?
