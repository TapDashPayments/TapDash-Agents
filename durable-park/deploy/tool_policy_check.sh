#!/usr/bin/env bash
# Check the agent tool policy before every hermes-gateway start (drop-in hermes-gateway-policy.conf runs this as
# ExecStartPre=-, as the hermes user). It writes the report $DURABLE_PARK_POLICY_STATUS that the durable-park
# plugin reads at load: a missing, failed or older-than-15-minutes report makes the plugin refuse every tool
# call. It ALWAYS exits 0, so the gateway still starts (Telegram/Teams and the sweep keep running) and fails
# closed inside. Never prints secrets.
#
# Per-person profiles (docs/specs/2026-10-26-per-person-roles-design.md, 6.3 and 6.4): Hermes serves every profile
# folder under ~/.hermes/profiles, routed to a person or not, and loads its own copy of the plugin in each. So
# after the default profile's check this script
#   1. lists the installed profiles (deploy/profile_keys_check.py list),
#   2. runs the same policy check for each one with HERMES_HOME set to that profile, writing its report next to the
#      default's, in <report folder>/profiles/<profile>/ (the profile's own status_dir); the `not-set-up` profile is
#      checked with --expect-no-tools: it must have no tool at all,
#   3. runs the key check (deploy/profile_keys_check.py check): a profile with tools but no key file, a secrets command
#      that does not name its profile, or two profiles sharing a key makes EVERY report failing.
# Anything that did not finish fails the reports it could not complete, never leaves an old passing one. It also
# removes each profile's load proof from the earlier gateway process, so a stale proof (a reused pid) can never
# satisfy plugin_load_check.sh.
set -u

HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
STATUS=${DURABLE_PARK_POLICY_STATUS:-$HERMES_HOME/plugin-data/durable-park/tool_policy_status.json}
STATUS_DIR=$(dirname -- "$STATUS")
PROOF=${DURABLE_PARK_PLUGIN_LOAD:-$HERMES_HOME/plugin-data/durable-park/plugin_load.json}
PROFILE_STATUS_NAME=tool_policy_status.json  # the plugin's own name for it (POLICY_STATUS_FILENAME)
NOT_SET_UP=not-set-up
REPO=$(cd -- "$(dirname -- "$0")/.." && pwd)
PROFILES=()  # the installed profile names (filled by list_profiles)

log() { printf 'tool_policy_check: %s\n' "$*" >&2; }

# A failing report at $1, written atomically (temp file next to it, then rename). Best effort: when the directory is
# not writable the plugin's missing/stale checks are the backstop. $2 is a fixed message (no quotes).
write_failing() {
    local dir tmp
    dir=$(dirname -- "$1")
    mkdir -p -- "$dir" 2>/dev/null || true
    tmp=$(mktemp "$dir/.status-XXXXXX.tmp" 2>/dev/null) || return 1
    printf '{"ok": false, "checked_at": "%s", "problems": ["%s"], "allowed": []}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" >"$tmp" || { rm -f -- "$tmp"; return 1; }
    chmod 0644 -- "$tmp"
    mv -f -- "$tmp" "$1" || { rm -f -- "$tmp"; return 1; }
}

# Record a failure at $1; when even that cannot be written (directory not writable?) say so and carry on: an old
# passing report may remain, and the plugin's 15-minute staleness check is the backstop.
fail_report() {
    write_failing "$1" "$2" || {
        log "durable-park: tool policy report could not be refreshed (directory not writable?)"
        if command -v logger >/dev/null 2>&1; then
            logger -t durable-park "tool policy report could not be refreshed" || true
        fi
    }
}

# Hermes's own Python environment, read from its facts.json: the same lookup as hermes_python() in
# configure_hermes.sh (a test keeps the two snippets identical).
hermes_python() {
    /usr/bin/python3 -I -c 'import glob, json, sys
facts = glob.glob(sys.argv[1] + "/installs/*/facts.json")
if len(facts) != 1:
    sys.exit("expected one Hermes install, found %d" % len(facts))
print(json.load(open(facts[0]))["packages"]["venv"]["environment"] + "/bin/python")' "$HERMES_HOME"
}

# Fills PROFILES with the installed profile names. Returns 1 when the list could not be made (or has a name that is
# not a Hermes profile name: it is never put in a path).
list_profiles() {
    local out name
    PROFILES=()
    out=$(timeout 20 "$1" -I "$REPO/deploy/profile_keys_check.py" list --hermes-home "$HERMES_HOME") || return 1
    while IFS= read -r name; do
        [[ -n $name ]] || continue
        [[ $name =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]] || return 1
        PROFILES+=("$name")
    done <<<"$out"
    return 0
}

# Run the policy check with Hermes python $1, for the profile home $2 (empty: the default profile, with the
# environment as it is), writing the report $3. $4 names it in the log. Further arguments go to the check. A
# report older than this run (or none) was not written by the check: it was killed, crashed, or could not
# write (an unwritable directory also keeps an old passing report from being replaced or removed).
run_policy_check() {
    local py=$1 home=$2 report=$3 label=$4 start rc=0
    shift 4
    # The check writes its report with a temp file next to it and fails when that folder is missing: a profile's
    # status folder does not exist before its plugin first loads (found by running this against a fake profile).
    mkdir -p -- "$(dirname -- "$report")" 2>/dev/null || true
    start=$(date +%s)
    if [[ -n $home ]]; then
        HERMES_HOME=$home timeout 60 "$py" -I "$REPO/deploy/hermes_tool_policy.py" check --status-file "$report" "$@" || rc=$?
    else
        timeout 60 "$py" -I "$REPO/deploy/hermes_tool_policy.py" check --status-file "$report" "$@" || rc=$?
    fi
    if [[ ! -e "$report" || $(stat -c %Y -- "$report" 2>/dev/null || echo 0) -lt $start ]]; then
        log "the check left no new report$label (exit $rc)"
        fail_report "$report" "the tool policy check did not write a report"
    elif [[ $rc -ne 0 ]]; then
        log "the tool policy check$label failed (exit $rc); the plugin will refuse tool calls"
    fi
}

# Fail the default profile's report and every profile's report (the listed ones and every report folder there is)
# with the fixed message $1: used when a step that all profiles depend on did not finish.
fail_all() {
    local name dir
    fail_report "$STATUS" "$1"
    for name in "${PROFILES[@]+"${PROFILES[@]}"}"; do
        fail_report "$STATUS_DIR/profiles/$name/$PROFILE_STATUS_NAME" "$1"
    done
    for dir in "$STATUS_DIR"/profiles/*/; do
        [[ -d $dir ]] || continue
        fail_report "${dir}$PROFILE_STATUS_NAME" "$1"
    done
}

# Every installed profile's own check, then the key check across all of them.
check_profiles() {
    local py=$1 name rc=0
    if ! list_profiles "$py"; then
        log "could not list the Hermes profiles"
        fail_all "could not list the Hermes profiles"
        return 0
    fi
    for name in "${PROFILES[@]+"${PROFILES[@]}"}"; do
        if [[ $name == "$NOT_SET_UP" ]]; then
            run_policy_check "$py" "$HERMES_HOME/profiles/$name" "$STATUS_DIR/profiles/$name/$PROFILE_STATUS_NAME" \
                " for profile $name" --expect-no-tools
        else
            run_policy_check "$py" "$HERMES_HOME/profiles/$name" "$STATUS_DIR/profiles/$name/$PROFILE_STATUS_NAME" \
                " for profile $name"
        fi
    done
    timeout 60 "$py" -I "$REPO/deploy/profile_keys_check.py" check --hermes-home "$HERMES_HOME" \
        --status-root "$STATUS_DIR" || rc=$?
    case $rc in
        0) ;;
        1) log "the profile key check found a key problem; the plugin will refuse tool calls in every profile" ;;
        *) log "the profile key check did not finish (exit $rc)"; fail_all "the key check did not finish" ;;
    esac
}

main() {
    local py
    # Each profile's load proof from an earlier gateway process goes first (the unit's own ExecStartPre removes the
    # default profile's).
    rm -f -- "$(dirname -- "$PROOF")"/profiles/*/"$(basename -- "$PROOF")" 2>/dev/null || true
    if ! py=$(hermes_python) || [[ ! -x "$py" ]]; then
        log "hermes python not found"; fail_report "$STATUS" "hermes python not found"; return 0
    fi
    run_policy_check "$py" "" "$STATUS" ""
    check_profiles "$py"
    return 0
}

main
exit 0
