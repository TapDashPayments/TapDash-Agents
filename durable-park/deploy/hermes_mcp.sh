#!/usr/bin/env bash
# Connect Hermes (proto VM, v0.22.2) to the withHuman MCP gateway as MCP server `wh`. Idempotent.
#
#   sudo bash /home/hermes/durable-park/deploy/hermes_mcp.sh      (or run it as the hermes user)
#
# Run AFTER deploy/configure_hermes.sh and AFTER provisioning (it refuses while
# /etc/durable-park/instance_token is missing). It writes, through Hermes's own `hermes config set`:
#
#   mcp_servers:
#     wh:
#       url: https://app.withhuman.ai/gateway/mcp           # Streamable HTTP (Hermes's default transport)
#       headers:
#         Authorization: Bearer ${env:DURABLE_PARK_WH_TOKEN}
#       trust: full                 # `untrusted` would block the plugin's dispatch_tool replay (probe report)
#       tools: {include: ["quickbooks__*"], resources: false, prompts: false}   # v1: QuickBooks only
#       enabled: true
#
# The token is never written to config.yaml or .env: Hermes's `command` secret source
# (/opt/durable-park/libexec/hermes-secrets, installed by configure_hermes.sh) prints
# DURABLE_PARK_WH_TOKEN=<instance_token> at every Hermes start. Then it restarts hermes-gateway and waits for
# the `wh` tool registration in the gateway log. Never prints a secret.
set -euo pipefail
umask 077

HERMES_USER=hermes
HERMES_HOMEDIR=$(getent passwd "$HERMES_USER" | cut -d: -f6)
HERMES_HOME="$HERMES_HOMEDIR/.hermes"
HERMES_BIN="$HERMES_HOMEDIR/.local/bin/hermes"
HERMES_UID=$(id -u "$HERMES_USER")
TOKEN_FILE=/etc/durable-park/instance_token
SECRETS_HELPER=/opt/durable-park/libexec/hermes-secrets
TOKEN_VAR=DURABLE_PARK_WH_TOKEN
SERVER=wh
GATEWAY_URL=https://app.withhuman.ai/gateway/mcp
TOOL_INCLUDE='["quickbooks__*"]'
EXPECTED_TOOL=mcp__wh__quickbooks__get_company_info

log() { printf 'hermes_mcp: %s\n' "$*"; }
die() { printf 'hermes_mcp: ERROR: %s\n' "$*" >&2; exit 1; }
# grep a captured string, never a pipe (pipefail + grep -q can turn SIGPIPE into "not found")
has() { local text=$1; shift; grep -q "$@" <<<"$text"; }

if [[ $EUID -eq 0 ]]; then
    as_hermes() {
        (cd "$HERMES_HOMEDIR" && runuser -u "$HERMES_USER" -- env -i HOME="$HERMES_HOMEDIR" USER="$HERMES_USER" \
            LOGNAME="$HERMES_USER" HERMES_HOME="$HERMES_HOME" XDG_RUNTIME_DIR="/run/user/$HERMES_UID" \
            LANG=C.UTF-8 PATH="$HERMES_HOMEDIR/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@")
    }
elif [[ $(id -un) == "$HERMES_USER" ]]; then
    as_hermes() { (cd "$HERMES_HOMEDIR" && XDG_RUNTIME_DIR="/run/user/$HERMES_UID" HERMES_HOME="$HERMES_HOME" "$@"); }
else
    die "run as root (sudo) or as $HERMES_USER"
fi
hermes_cli() { as_hermes "$HERMES_BIN" "$@"; }
log_since() { awk -v s="$1" 'substr($0, 1, 19) >= s' "$HERMES_HOME/logs/agent.log" 2>/dev/null || true; }

# --- preconditions -----------------------------------------------------------------------------------------
[[ -x "$HERMES_BIN" ]] || die "hermes launcher not found at $HERMES_BIN"
[[ -e "$TOKEN_FILE" ]] || die "$TOKEN_FILE does not exist: provision the instance first (docs/receiver-setup.md, Step 6)"
as_hermes test -r "$TOKEN_FILE" || die "$HERMES_USER cannot read $TOKEN_FILE (expected 0640 root:$HERMES_USER)"
as_hermes test -s "$TOKEN_FILE" || die "$TOKEN_FILE is empty"
[[ -x "$SECRETS_HELPER" ]] || die "$SECRETS_HELPER missing: run deploy/configure_hermes.sh first"
helper_command=$(hermes_cli config get secrets.command.command 2>/dev/null || true)
[[ "$helper_command" == "$SECRETS_HELPER" ]] \
    || die "secrets.command.command is not $SECRETS_HELPER: run deploy/configure_hermes.sh first"
# Key names only (cut drops the values; the whole output is read, so no SIGPIPE).
helper_keys=$(as_hermes "$SECRETS_HELPER" | cut -d= -f1) || die "the secret helper failed as $HERMES_USER"
has "$helper_keys" -x "$TOKEN_VAR" || die "the secret helper does not print $TOKEN_VAR (is $TOKEN_FILE readable?)"
log "preconditions ok (instance token present, secret helper prints $TOKEN_VAR)"

# --- config ------------------------------------------------------------------------------------------------
stamp=$(date -u +%Y%m%dT%H%M%SZ)
as_hermes cp -p "$HERMES_HOME/config.yaml" "$HERMES_HOME/config.yaml.bak-hermes-mcp-$stamp"
log "backed up config.yaml to config.yaml.bak-hermes-mcp-$stamp"
set_cfg() { hermes_cli config set "$1" "$2" >/dev/null; }
set_cfg "mcp_servers.$SERVER.url" "$GATEWAY_URL"
# shellcheck disable=SC2016  # a literal Hermes ${env:...} reference, resolved by Hermes at connect time
set_cfg "mcp_servers.$SERVER.headers.Authorization" 'Bearer ${env:DURABLE_PARK_WH_TOKEN}'
set_cfg "mcp_servers.$SERVER.trust" full
set_cfg "mcp_servers.$SERVER.tools.include" "$TOOL_INCLUDE"
set_cfg "mcp_servers.$SERVER.tools.resources" false
set_cfg "mcp_servers.$SERVER.tools.prompts" false
set_cfg "mcp_servers.$SERVER.enabled" true
if as_hermes cmp -s "$HERMES_HOME/config.yaml" "$HERMES_HOME/config.yaml.bak-hermes-mcp-$stamp"; then
    as_hermes rm -f "$HERMES_HOME/config.yaml.bak-hermes-mcp-$stamp"
    log "config.yaml already had mcp_servers.$SERVER (no change, backup removed)"
else
    log "config.yaml: mcp_servers.$SERVER set"
fi

# --- restart and check -------------------------------------------------------------------------------------
# Always restart: the token reaches Hermes only through the secret source at process start.
since=$(date '+%Y-%m-%d %H:%M:%S')
as_hermes systemctl --user restart hermes-gateway
log "restarted hermes-gateway; waiting for MCP server '$SERVER'"
registered=""
for _ in $(seq 1 120); do
    recent=$(log_since "$since")
    registered=$(grep -F "MCP server '$SERVER'" <<<"$recent" | grep -F "registered" | tail -n 1 || true)
    [[ -n "$registered" ]] && break
    sleep 1
done
[[ -n "$registered" ]] || {
    grep -F "'$SERVER'" <<<"$(log_since "$since")" | tail -n 5 >&2 || true
    die "no tool registration for MCP server '$SERVER' within 120 s (lines above, if any; see $HERMES_HOME/logs/agent.log)"
}
log "${registered#*INFO }"
has "$registered" -F -- "$EXPECTED_TOOL" \
    || die "$EXPECTED_TOOL is not registered: is the QuickBooks server granted to agent proto-agents in withHuman?"
has "$(log_since "$since")" -F "durable-park loaded" || log "WARNING: no 'durable-park loaded' line yet after the restart"
log "done: $EXPECTED_TOOL available. Live check: DURABLE_PARK_LIVE=1 .venv/bin/pytest -q tests/live/test_gateway_smoke.py"
