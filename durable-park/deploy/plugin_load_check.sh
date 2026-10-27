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
#
# Per-person profiles: every profile Hermes serves loads its own copy of the plugin in the same gateway process, a
# few seconds after start and before any message, and writes its own proof in <proof directory>/profiles/<profile>/.
# The gateway start then also waits (the same time) for a matching proof of EVERY such profile and names each
# profile that has none. Those are the routed profiles (gateway.profile_routes in the Hermes config) AND every
# profile folder Hermes serves under <HERMES_HOME>/profiles, routed or not (deploy/profile_keys_check.py list
# --serving: the folders Hermes itself lists, without the ones an admin parked). With no routes and no profile
# folder this is exactly the check above. A config that cannot be read, a route with a profile name that is not
# valid, or a profile folder list that cannot be made, fails closed.
set -u

HERMES_HOME=${HERMES_HOME:-${HOME:-/nonexistent}/.hermes}
PROOF=${DURABLE_PARK_PLUGIN_LOAD:-$HERMES_HOME/plugin-data/durable-park/plugin_load.json}
TIMEOUT=${DURABLE_PARK_LOAD_TIMEOUT:-30}
MAINPID=${MAINPID:-}
PYTHON=${DURABLE_PARK_PYTHON:-/usr/bin/python3}  # overridable for tests
CONFIG=${DURABLE_PARK_HERMES_CONFIG:-$HERMES_HOME/config.yaml}
PROOF_DIR=$(dirname -- "$PROOF")
PROOF_NAME=$(basename -- "$PROOF")
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
PROFILES=()  # routed and installed profile names (filled by load_profiles)

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

# Prints the profile of each gateway.profile_routes entry (one per line). Exit 3: this python has no yaml.
ROUTES_CODE='import sys
try:
    import yaml
except ImportError:
    sys.exit(3)
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    routes = (config.get("gateway") or {}).get("profile_routes") or []
    names = [r.get("profile") for r in routes if isinstance(r, dict) and r.get("profile") is not None]
except Exception:
    sys.exit(4)
for name in names:
    print(name if isinstance(name, str) else "?")'

routes_with() {
    timeout 5 "$1" -I -c "$ROUTES_CODE" "$CONFIG" 2>/dev/null
}

# Hermes's own Python environment (it has yaml), read from its facts.json: the same lookup as hermes_python() in
# tool_policy_check.sh.
hermes_python() {
    "$PYTHON" -I -c 'import glob, json, sys
facts = glob.glob(sys.argv[1] + "/installs/*/facts.json")
if len(facts) != 1:
    sys.exit(1)
print(json.load(open(facts[0]))["packages"]["venv"]["environment"] + "/bin/python")' "$HERMES_HOME" 2>/dev/null
}

# The routed profiles, one per line. A missing config, or one that never mentions profile_routes, has none (no
# python needed). Anything that cannot be read exits 1 (the caller fails closed): grep exits 0 when the config
# mentions profile_routes, 1 when it does not (no routes), and 2 or more when it could not read the file.
read_routes() {
    local rc=0 py
    [[ -f $CONFIG ]] || return 0
    grep -q 'profile_routes' -- "$CONFIG" 2>/dev/null
    rc=$?
    [[ $rc -eq 1 ]] && return 0
    [[ $rc -eq 0 ]] || return 1
    routes_with "$PYTHON" && return 0
    rc=$?
    [[ $rc -eq 3 ]] || return 1
    py=$(hermes_python) || return 1
    [[ -x $py ]] || return 1
    routes_with "$py"
}

# The profile folders Hermes serves, one name per line (deploy/profile_keys_check.py list --serving). Without a
# python there is nothing to read a proof with, so no list is made: the missing root proof then stops the gateway
# (below). A missing helper script, or one that fails, exits 1 (the caller fails closed).
read_installed() {
    command -v "$PYTHON" >/dev/null 2>&1 || return 0
    [[ -f $HERE/profile_keys_check.py ]] || return 1
    timeout 20 "$PYTHON" -I "$HERE/profile_keys_check.py" list --hermes-home "$HERMES_HOME" --serving 2>/dev/null
}

# Fills PROFILES (default-profile names and repeats dropped) from the routes and from the profile folders. Returns 1
# after printing the alert when the routes cannot be read, a route name is not one a profile may have (the same
# rule as keys.py), or the profile folders cannot be listed. A folder name only has to be a name Hermes accepts
# (one character longer than ours): profile_keys_check.py check reports the ones the key rule does not allow.
load_profiles() {
    local list installed="" name
    PROFILES=()
    if [[ -n ${DURABLE_PARK_PROFILES+x} ]]; then  # a test (or an admin) names them directly
        list=${DURABLE_PARK_PROFILES//,/ }
    else
        if ! list=$(read_routes); then
            printf 'durable-park ALERT: could not read gateway.profile_routes from the Hermes config; stopping the gateway so no profile runs unchecked\n'
            return 1
        fi
        if ! installed=$(read_installed); then
            printf 'durable-park ALERT: could not list the Hermes profiles; stopping the gateway so no profile runs unchecked\n'
            return 1
        fi
    fi
    set -f  # the names are split from $list below: a name such as * must stay a (bad) name, never expand to files
    # shellcheck disable=SC2086  # split on purpose; globbing is off (set -f)
    for name in $list; do
        [[ $name == default || $name == main ]] && continue
        if [[ ! $name =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
            set +f
            printf 'durable-park ALERT: a gateway.profile_routes entry has a profile name that is not valid; stopping the gateway so no profile runs unchecked\n'
            return 1
        fi
        [[ " ${PROFILES[*]:-} " == *" $name "* ]] || PROFILES+=("$name")
    done
    # shellcheck disable=SC2086  # split on purpose; globbing is off (set -f)
    for name in $installed; do
        if [[ ! $name =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; then
            set +f
            printf 'durable-park ALERT: could not list the Hermes profiles; stopping the gateway so no profile runs unchecked\n'
            return 1
        fi
        [[ " ${PROFILES[*]:-} " == *" $name "* ]] || PROFILES+=("$name")
    done
    set +f
    return 0
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
    local found_root="" name found deadline NOW_US pending=()
    declare -A found_profile=()
    load_profiles || return 1
    pending=("${PROFILES[@]+"${PROFILES[@]}"}")
    now_us
    deadline=$((NOW_US + 10#$TIMEOUT * 1000000))  # 10#: "08" is not octal
    if [[ $MAINPID =~ ^[1-9][0-9]*$ ]] && command -v "$PYTHON" >/dev/null 2>&1; then
        while :; do
            if [[ -z $found_root ]]; then
                found_root=$(proof_for_pid "$PROOF" "$MAINPID") || found_root=""
            fi
            pending=()
            for name in "${PROFILES[@]+"${PROFILES[@]}"}"; do
                if [[ -z ${found_profile[$name]:-} ]]; then
                    found_profile[$name]=$(proof_for_pid "$PROOF_DIR/profiles/$name/$PROOF_NAME" "$MAINPID") || found_profile[$name]=""
                fi
                [[ -n ${found_profile[$name]} ]] || pending+=("$name")
            done
            if [[ -n $found_root && ${#pending[@]} -eq 0 ]]; then
                printf 'durable-park plugin loaded (mode=%s, pid=%s)\n' "${found_root%% *}" "${found_root##* }"
                for name in "${PROFILES[@]+"${PROFILES[@]}"}"; do
                    found=${found_profile[$name]}
                    printf 'durable-park plugin loaded for profile %s (mode=%s, pid=%s)\n' "$name" "${found%% *}" "${found##* }"
                done
                return 0
            fi
            now_us
            if [[ $NOW_US -ge $deadline ]]; then
                break
            fi
            sleep 1
        done
    fi
    if [[ -z $found_root ]]; then
        printf 'durable-park ALERT: the plugin did not load in this gateway (pid %s); stopping the gateway so no tool call runs unchecked\n' "$MAINPID"
    fi
    for name in "${pending[@]+"${pending[@]}"}"; do
        printf 'durable-park ALERT: the plugin did not load for profile %s in this gateway (pid %s); stopping the gateway so no tool call runs unchecked\n' "$name" "$MAINPID"
    done
    return 1
}

main
exit $?
