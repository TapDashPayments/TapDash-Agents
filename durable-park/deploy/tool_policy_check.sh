#!/usr/bin/env bash
# Check the agent tool policy before every hermes-gateway start (drop-in hermes-gateway-policy.conf runs this as
# ExecStartPre=-, as the hermes user). It writes the report $DURABLE_PARK_POLICY_STATUS that the durable-park
# plugin reads at load: a missing, failed or older-than-15-minutes report makes the plugin refuse every tool
# call. It ALWAYS exits 0, so the gateway still starts (Telegram/Teams and the sweep keep running) and fails
# closed inside. Never prints secrets.
set -u

HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
STATUS=${DURABLE_PARK_POLICY_STATUS:-$HERMES_HOME/plugin-data/durable-park/tool_policy_status.json}
REPO=$(cd -- "$(dirname -- "$0")/.." && pwd)

log() { printf 'tool_policy_check: %s\n' "$*" >&2; }

# A failing report, written atomically (temp file next to it, then rename). Best effort: when the directory is
# not writable the plugin's missing/stale checks are the backstop. $1 is a fixed message (no quotes).
write_failing() {
    local dir tmp
    dir=$(dirname -- "$STATUS")
    mkdir -p -- "$dir" 2>/dev/null || true
    tmp=$(mktemp "$dir/.status-XXXXXX.tmp" 2>/dev/null) || { log "cannot write $STATUS"; return 0; }
    printf '{"ok": false, "checked_at": "%s", "problems": ["%s"], "allowed": []}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >"$tmp" || { rm -f -- "$tmp"; return 0; }
    chmod 0644 -- "$tmp"
    mv -f -- "$tmp" "$STATUS" || rm -f -- "$tmp"
    return 0
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

# what the status file is now: inode, mtime, size (empty when missing)
fingerprint() { stat -c '%i %Y %s' -- "$STATUS" 2>/dev/null || true; }

main() {
    local py before after rc
    py=$(hermes_python) && [[ -x "$py" ]] || { log "hermes python not found"; write_failing "hermes python not found"; return 0; }
    before=$(fingerprint)
    rc=0
    timeout 60 "$py" -I "$REPO/deploy/hermes_tool_policy.py" check --status-file "$STATUS" || rc=$?
    after=$(fingerprint)
    if [[ -z "$after" || ( $rc -ne 0 && "$after" == "$before" ) ]]; then
        # the check failed and left no new report (killed, crashed, or unable to write): record a failure ourselves
        log "the check left no new report (exit $rc)"
        write_failing "the tool policy check did not write a report"
    elif [[ $rc -ne 0 ]]; then
        log "the tool policy check failed (exit $rc); the plugin will refuse tool calls"
    fi
    return 0
}

main
exit 0
