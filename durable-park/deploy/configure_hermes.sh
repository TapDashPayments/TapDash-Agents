#!/usr/bin/env bash
# On proto-agents this script re-applies the current tool policy (v1 lockdown, re-applied 2026-10-21 03:16 UTC). It restarts the gateway: run it only when no ledger row is parked, approved or executing. See docs/specs/2026-10-21-all-tools-withhuman-design.md.
# durable-park deploy for the proto VM (Hermes Agent v0.22.3). Idempotent: safe to re-run.
#
#   sudo bash /home/hermes/durable-park/deploy/configure_hermes.sh
#
# What it does (each step checks before it changes anything):
#   1. system user aaprecv; group dpwake (members hermes, aaprecv); /etc/durable-park (0755 root)
#   2. /etc/durable-park/wake_secret: generated once (0640 root:dpwake); never printed. No bridge_secret.
#   3. receiver venv /opt/durable-park (agentapprovalprotocol + uvicorn + starlette + httpx, pinned) and a root-owned
#      copy of receiver/*.py; system unit durable-park-receiver (enabled + started: it answers 401 to every
#      event until provision.py has written /etc/durable-park/receiver.json); the Hermes secret-source helper
#      /opt/durable-park/libexec/hermes-secrets
#   3b. pay links (TAP2-82): system user dppay; folder /var/lib/durable-park-paylinks (2750 hermes:dppay: the
#      plugin writes one file per link, the service only reads); root-owned copy of paylink/paylink_app.py under
#      /opt/durable-park/paylink; system unit durable-park-paylink (127.0.0.1:8793, User=dppay)
#   4. Hermes (as user hermes, through Hermes's own CLI): plugin symlink; `hermes plugins enable` (PM
#      admission installs the plugin.yaml python_dependencies into Hermes's environment); plugin settings;
#      plugins.hook_callback_timeout 60; agent.restart_drain_timeout 45; the local webhook platform (127.0.0.1:8644, route durable-park-wake,
#      deliver log, rate_limit 120) whose secret is ${DURABLE_PARK_WAKE_SECRET}, supplied at startup by
#      the `command` secret source (the helper above reads wake_secret, and instance_token once it exists, for
#      deploy/hermes_mcp.sh); no copy of a secret in config.yaml/.env
#   4b. gateway bridge: system user dpbridge (no login, no home, no other group: the bot cannot signal it);
#      bridge_secret made root:root 0600 when it exists (systemd reads it as root, LoadCredential); root-owned copy of
#      bridge/ + the whole plugin/durable_park package (incl. _vendor/, stale files removed) under /opt/durable-park;
#      socket unit durable-park-bridge.socket (systemd holds 127.0.0.1:8791, also while the bridge restarts) and
#      system unit durable-park-bridge (User=dpbridge, gets the socket from systemd); it must answer 401 to an
#      unsigned POST within 10 s of the restart or the script stops; then deploy/bridge_check.py (root-owned copy
#      under /opt/durable-park/deploy, required in the checkout) must pass. /etc/durable-park/bridge_secret and
#      /etc/durable-park/audit_key (the org key it asks withHuman with) are stored by Yaakov, never created here, and
#      must exist and be non-empty: the unit cannot start without them (LoadCredential), so the script stops at the
#      very start, changing nothing, when one is missing. Both are read at service start: restart
#      durable-park-bridge after rotating one. The bridge is stateless: no ledger, no writable path
#   4c. agent tool policy (deploy/hermes_tool_policy.py, run in Hermes's own Python): every platform key an
#      agent can run under gets only durable_park + clarify + MCP server wh (QuickBooks tools); every toolset
#      with local file/command/code access is disabled; model-named file delivery is limited to the Hermes
#      caches (closes the ledger bypass: the agent's tools run as hermes, which owns the ledger). Toolsets an
#      admin lists in /etc/durable-park/allowed_toolsets (created empty here; Phase-2 ones refused) are opened too. Checked
#      before AND after the gateway restart; a failed pre-restart check aborts without restarting anything.
#      A hermes-gateway drop-in (50-durable-park-policy.conf) re-runs the check at every gateway start
#      (deploy/tool_policy_check.sh); the plugin refuses every tool call when its report is missing/failed/stale.
#      The same drop-in runs deploy/plugin_load_check.sh as ExecStartPost (a failure stops the gateway when the
#      plugin did not load: no plugin_load.json for the gateway's pid) and limits restarts (StartLimitBurst=3 in 10 min).
#   5. sweep wrapper ~/.hermes/scripts/durable_park_sweep.sh + cron job (`*/5 * * * *`, --no-agent, telegram;
#      an existing job on another schedule is moved over with `hermes cron edit`)
#   6. Tailscale Funnel /aap/events -> 127.0.0.1:8790/aap/events, /wh/bridge -> 127.0.0.1:8791/wh/bridge and
#      /pay -> 127.0.0.1:8793/pay; the plugin's paylink_origin is this machine's Funnel address
#   7. restart: the hermes user manager when the gateway does not carry the dpwake group yet, else the
#      hermes-gateway user unit; then wait for the plugin's "durable-park loaded" log line
#
# Never prints secrets. Does NOT provision the withHuman instance (human step: docs/receiver-setup.md).
set -euo pipefail
umask 022

REPO=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)
HERMES_USER=hermes
HERMES_HOMEDIR=$(getent passwd "$HERMES_USER" | cut -d: -f6)
HERMES_HOME="$HERMES_HOMEDIR/.hermes"
HERMES_BIN="$HERMES_HOMEDIR/.local/bin/hermes"
ETC=/etc/durable-park
VENV=/opt/durable-park
UNIT=durable-park-receiver
PLUGIN_ID=durable-park
ROUTE=durable-park-wake
LEDGER="$HERMES_HOME/plugin-data/durable-park/ledger.db"
SWEEP_WRAPPER="$HERMES_HOME/scripts/durable_park_sweep.sh"
CRON_NAME=durable-park-sweep
AAP_BASE_URL=https://app.withhuman.ai/api/aap
FUNNEL_PATH=/aap/events
RECEIVER_LOCAL=http://127.0.0.1:8790/aap/events
BRIDGE_UNIT=durable-park-bridge
BRIDGE_FUNNEL_PATH=/wh/bridge
BRIDGE_LOCAL=http://127.0.0.1:8791/wh/bridge
PAYLINK_UNIT=durable-park-paylink
PAYLINK_DIR=/var/lib/durable-park-paylinks
PAYLINK_FUNNEL_PATH=/pay
PAYLINK_LOCAL=http://127.0.0.1:8793/pay
SECRETS_HELPER="$VENV/libexec/hermes-secrets"
# Earlier value of secrets.command.command (inline sed); migrated to SECRETS_HELPER.
OLD_SECRET_COMMAND="sed -n 's/^/DURABLE_PARK_WAKE_SECRET=/p' $ETC/wake_secret"
# Receiver venv pins: what the repo's tests run against. httpx is imported directly by the bridge (bridge/whapi.py):
# pinned here, to the version already in the VM's venv, instead of relying on agentapprovalprotocol to pull it in.
RECEIVER_PACKAGES=("agentapprovalprotocol==0.3.3" "uvicorn==0.54.0" "starlette==1.9.0" "httpx==0.28.1")

log() { printf 'configure_hermes: %s\n' "$*"; }
die() { printf 'configure_hermes: ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo bash $0)"
[[ -n "$HERMES_HOMEDIR" && -d "$HERMES_HOME" ]] || die "no Hermes home for user $HERMES_USER"
[[ -x "$HERMES_BIN" ]] || die "hermes launcher not found at $HERMES_BIN"
[[ -f "$REPO/plugin/durable_park/plugin.yaml" ]] || die "repo layout not found under $REPO"
[[ "$REPO" != *[[:space:]]* ]] || die "the checkout path contains whitespace ($REPO); move it (the systemd ExecStartPre= line cannot hold it)"
# The bridge unit reads both keys as credentials and cannot start without them; its health check ships in the repo.
# Stop here, before anything changes (in particular before an old bridge is stopped), when one is missing.
[[ -s "$ETC/bridge_secret" ]] || die "$ETC/bridge_secret is missing or empty: store it first (as root, with read -rs; see docs/runbook.md), then run this script again; nothing was changed"
[[ -s "$ETC/audit_key" ]] || die "$ETC/audit_key is missing or empty: store it first (as root, with read -rs; see docs/runbook.md), then run this script again; nothing was changed"
[[ -f "$REPO/deploy/bridge_check.py" ]] || die "deploy/bridge_check.py is missing from the checkout $REPO (a broken deploy); nothing was changed"
HERMES_UID=$(id -u "$HERMES_USER")
RUNTIME_DIR="/run/user/$HERMES_UID"

# Run a command as the hermes user with a clean environment (nothing of root's environment leaks in).
as_hermes() {
    (cd "$HERMES_HOMEDIR" && runuser -u "$HERMES_USER" -- env -i HOME="$HERMES_HOMEDIR" USER="$HERMES_USER" \
        LOGNAME="$HERMES_USER" HERMES_HOME="$HERMES_HOME" XDG_RUNTIME_DIR="$RUNTIME_DIR" LANG=C.UTF-8 \
        PATH="$HERMES_HOMEDIR/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@")
}
hermes_cli() { as_hermes "$HERMES_BIN" "$@"; }
user_systemctl() { as_hermes systemctl --user "$@"; }
# agent.log lines written since a "YYYY-MM-DD HH:MM:SS" timestamp
# Hermes's own Python environment (PM-managed; can change after `hermes pm repair`), read from its facts.json
hermes_python() {
    as_hermes /usr/bin/python3 -I -c 'import glob, json, sys
facts = glob.glob(sys.argv[1] + "/installs/*/facts.json")
if len(facts) != 1:
    sys.exit("expected one Hermes install, found %d" % len(facts))
print(json.load(open(facts[0]))["packages"]["venv"]["environment"] + "/bin/python")' "$HERMES_HOME"
}
tool_policy() { as_hermes "$(hermes_python)" -I "$REPO/deploy/hermes_tool_policy.py" "$1"; }
log_since() { awk -v s="$1" 'substr($0, 1, 19) >= s' "$HERMES_HOME/logs/agent.log" 2>/dev/null || true; }
# grep a captured string, never a pipe: under pipefail, `cmd | grep -q` can report the writer's SIGPIPE
# (grep exits at the first match) as "not found". Usage: has TEXT GREP-ARGS...
has() { local text=$1; shift; grep -q "$@" <<<"$text"; }
# install SRC as DST (root:root, MODE) only when content, mode or owner differ; then sets CHANGED=1
install_root() {
    local src=$1 dst=$2 mode=$3
    if ! cmp -s "$src" "$dst" || [[ "$(stat -c '%a %U:%G' "$dst" 2>/dev/null)" != "${mode#0} root:root" ]]; then
        install -m "$mode" -o root -g root "$src" "$dst"
        CHANGED=1
    fi
}
# restart a system unit when CHANGED=1 or it is not running
restart_if_needed() {
    if [[ $CHANGED == 1 ]] || ! systemctl is-active --quiet "$1"; then
        systemctl restart "$1"
        log "restarted $1"
    fi
}

# --- 1. users, groups, directory --------------------------------------------------------------------------
getent group dpwake >/dev/null || { groupadd --system dpwake; log "created group dpwake"; }
if ! id -u aaprecv >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin aaprecv
    log "created system user aaprecv"
fi
for member in "$HERMES_USER" aaprecv; do
    if ! has "$(id -nG "$member" | tr ' ' '\n')" -x dpwake; then
        usermod -aG dpwake "$member"
        log "added $member to dpwake"
    fi
done
install -d -m 0755 -o root -g root "$ETC"

# --- 2. wake secret ----------------------------------------------------------------------------------------
if [[ ! -s "$ETC/wake_secret" ]]; then
    tmp=$(mktemp "$ETC/.wake_secret.XXXXXX")             # created 0600 root
    { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; printf '\n'; } >"$tmp"   # 64 hex chars
    chown root:dpwake "$tmp"
    chmod 0640 "$tmp"
    mv -f "$tmp" "$ETC/wake_secret"
    log "generated $ETC/wake_secret"
fi
chown root:dpwake "$ETC/wake_secret"
chmod 0640 "$ETC/wake_secret"
# Files provision.py / the human step own: only re-assert their owners and modes if they exist.
if [[ -e "$ETC/receiver.json" ]]; then chown aaprecv:aaprecv "$ETC/receiver.json"; chmod 0600 "$ETC/receiver.json"; fi
if [[ -e "$ETC/instance_token" ]]; then chown "root:$HERMES_USER" "$ETC/instance_token"; chmod 0640 "$ETC/instance_token"; fi
if [[ -e "$ETC/audit_key" ]]; then chown "root:$HERMES_USER" "$ETC/audit_key"; chmod 0640 "$ETC/audit_key"; fi  # the plugin reads it
if [[ -e "$ETC/provisioner_token" ]]; then chown root:root "$ETC/provisioner_token"; chmod 0600 "$ETC/provisioner_token"; fi

# --- 3. receiver venv, code, unit --------------------------------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
    log "created venv $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-input "${RECEIVER_PACKAGES[@]}"
install -d -m 0755 -o root -g root "$VENV/receiver" "$VENV/libexec"
CHANGED=0
for f in app.py wake_post.py provision.py; do
    install_root "$REPO/receiver/$f" "$VENV/receiver/$f" 0644
done
install_root "$REPO/deploy/$UNIT.service" "/etc/systemd/system/$UNIT.service" 0644
[[ $CHANGED == 1 ]] && systemctl daemon-reload
systemctl enable --quiet "$UNIT"
restart_if_needed "$UNIT"   # stateless; provision.py restarts it itself after writing receiver.json
log "receiver unit: $(systemctl is-active "$UNIT")"

# --- 3b. pay-link service (TAP2-82) ------------------------------------------------------------------------
if ! id -u dppay >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin dppay
    log "created system user dppay"
fi
# setgid: every file the plugin (as hermes) writes here belongs to group dppay, which the service reads with.
install -d -m 2750 -o "$HERMES_USER" -g dppay "$PAYLINK_DIR"
install -d -m 0755 -o root -g root "$VENV/paylink"
CHANGED=0
install_root "$REPO/paylink/paylink_app.py" "$VENV/paylink/paylink_app.py" 0644
install_root "$REPO/deploy/$PAYLINK_UNIT.service" "/etc/systemd/system/$PAYLINK_UNIT.service" 0644
[[ $CHANGED == 1 ]] && systemctl daemon-reload
systemctl enable --quiet "$PAYLINK_UNIT"
restart_if_needed "$PAYLINK_UNIT"
log "pay-link unit: $(systemctl is-active "$PAYLINK_UNIT")"
PAYLINK_ORIGIN="https://$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
[[ $PAYLINK_ORIGIN =~ ^https://[a-z0-9.-]+\.ts\.net$ ]] || die "could not read this machine's Funnel address ($PAYLINK_ORIGIN)"

# Hermes `command` secret source helper: KEY=VALUE lines on stdout, read by Hermes at every start (as hermes;
# Hermes discards the helper's stderr). The instance token line appears only once provisioning wrote the file.
helper=$(mktemp)
cat >"$helper" <<HELPER
#!/bin/sh
# durable-park secrets for Hermes (secrets.command). Written by deploy/configure_hermes.sh; edits are overwritten.
/usr/bin/sed -n '1s/^/DURABLE_PARK_WAKE_SECRET=/p' $ETC/wake_secret || exit 1
if [ -r $ETC/instance_token ]; then
    /usr/bin/sed -n '1s/^/DURABLE_PARK_WH_TOKEN=/p' $ETC/instance_token || exit 1
fi
exit 0
HELPER
CHANGED=0
install_root "$helper" "$SECRETS_HELPER" 0755
rm -f "$helper"

# --- 4. Hermes configuration (Hermes's own CLI, as hermes) -------------------------------------------------
stamp=$(date -u +%Y%m%dT%H%M%SZ)
as_hermes cp -p "$HERMES_HOME/config.yaml" "$HERMES_HOME/config.yaml.bak-durable-park-$stamp"
chmod 0600 "$HERMES_HOME"/config.yaml.bak-durable-park-*
log "backed up config.yaml to config.yaml.bak-durable-park-$stamp"

link="$HERMES_HOME/plugins/$PLUGIN_ID"
if [[ "$(readlink "$link" 2>/dev/null || true)" != "$REPO/plugin/durable_park" ]]; then
    as_hermes install -d -m 0775 "$HERMES_HOME/plugins"
    as_hermes ln -sfn "$REPO/plugin/durable_park" "$link"
    log "linked $link"
fi

enabled_plugins=$(hermes_cli config get plugins.enabled 2>/dev/null || true)
if ! has "$enabled_plugins" -E -- "^[[:space:]]*- ${PLUGIN_ID}[[:space:]]*\$"; then
    # PM admission: resolves plugin.yaml python_dependencies into Hermes's environment with the config change.
    hermes_cli plugins enable "$PLUGIN_ID" --no-allow-tool-override
fi
if ! hermes_cli --run-module agentapprovalprotocol._wire >/dev/null 2>&1; then
    log "agentapprovalprotocol not importable by Hermes: running hermes pm repair"
    hermes_cli pm repair
    hermes_cli --run-module agentapprovalprotocol._wire >/dev/null 2>&1 \
        || die "Hermes still cannot import agentapprovalprotocol after pm repair"
fi
log "Hermes runtime imports agentapprovalprotocol"

# The secret source runs the helper at every Hermes start. Never replace someone else's command source
# (our own earlier inline sed value is migrated).
current_command=$(hermes_cli config get secrets.command.command 2>/dev/null || true)
if [[ -n "$current_command" && "$current_command" != "Config key not set"* \
      && "$current_command" != "$SECRETS_HELPER" && "$current_command" != "$OLD_SECRET_COMMAND" ]]; then
    die "secrets.command.command is already set to another helper; merge it with $SECRETS_HELPER"
fi

set_cfg() { hermes_cli config set "$1" "$2" >/dev/null; }
set_cfg plugins.hook_callback_timeout 60
# GitHub #66: a stop or restart waits up to 45 s for a reply in progress (Hermes default 0 cuts it off).
# Keep it at least 15 s under the gateway unit's TimeoutStopSec (70).
set_cfg agent.restart_drain_timeout 45
set_cfg "plugins.entries.$PLUGIN_ID.allow_gateway_injection" true
set_cfg "plugins.entries.$PLUGIN_ID.settings.aap_base_url" "$AAP_BASE_URL"
set_cfg "plugins.entries.$PLUGIN_ID.settings.token_file" "$ETC/instance_token"
set_cfg "plugins.entries.$PLUGIN_ID.settings.ledger_path" "$LEDGER"
set_cfg "plugins.entries.$PLUGIN_ID.settings.paylink_dir" "$PAYLINK_DIR"
set_cfg "plugins.entries.$PLUGIN_ID.settings.paylink_origin" "$PAYLINK_ORIGIN"
set_cfg secrets.command.enabled true
set_cfg secrets.command.command "$SECRETS_HELPER"
set_cfg platforms.webhook.enabled true
set_cfg platforms.webhook.extra.host 127.0.0.1
set_cfg platforms.webhook.extra.port 8644
set_cfg platforms.webhook.extra.rate_limit 120
# A bare ${VAR} reference, not the env:-prefixed form: Hermes expands config at import time, before the
# secrets.command helper has put the value in the environment, and warns "Config ref ... is not set" in every
# process for the env: form only (hermes_cli/config.py _env_expand_match). Both forms resolve alike; the
# cached config is re-expanded after the helper ran, so the real value is used. See docs/receiver-setup.md.
# shellcheck disable=SC2016  # a literal Hermes ${...} reference, expanded by Hermes, not by bash
set_cfg "platforms.webhook.extra.routes.$ROUTE.secret" '${DURABLE_PARK_WAKE_SECRET}'
set_cfg "platforms.webhook.extra.routes.$ROUTE.events" '["wake"]'
set_cfg "platforms.webhook.extra.routes.$ROUTE.deliver" log
set_cfg "platforms.webhook.extra.routes.$ROUTE.prompt" \
    "durable-park wake event. The durable-park plugin handles it; if you see this, reply only: ignored."
log "Hermes config set (plugin settings, hook timeout, webhook route $ROUTE, secret source)"

as_hermes install -d -m 0750 "$(dirname "$LEDGER")"   # the plugin's data directory (ledger and status files)

# --- 4c. agent tool policy ---------------------------------------------------------------------------------
# The one list of toolsets opened beyond the v1 lockdown (read by apply and check). Created empty if missing,
# never overwritten: an admin edits it as root (docs/runbook.md "Open a toolset").
[[ -e "$ETC/allowed_toolsets" ]] || install -m 640 -o root -g hermes /dev/null "$ETC/allowed_toolsets"
# Propagate the script's exit code: 2 = invalid allow file (nothing applied, nothing restarted), 1 = anything else.
tool_policy apply || { rc=$?; printf 'configure_hermes: ERROR: %s\n' "could not apply the agent tool policy (exit $rc)" >&2; exit "$rc"; }
# Check BEFORE any restart: a failed check stops here and the running gateway keeps its old process.
policy_report=$(tool_policy check) \
    || { printf '%s\n' "$policy_report" >&2; die "agent tool policy check failed (nothing restarted)"; }
log "agent tool policy ok before restart"

# Re-check the policy at every gateway start: a user-unit drop-in runs deploy/tool_policy_check.sh as
# ExecStartPre=- (never blocks the start; the plugin fails closed when the report it writes is missing, failed
# or older than 15 minutes) and as ExecStartPost (no "-": the gateway is stopped when the plugin did not load).
# The %h/durable-park paths in the repo copy are rewritten to this checkout (the sed has no g flag: one per line).
DROPIN_DIR="$HERMES_HOMEDIR/.config/systemd/user/hermes-gateway.service.d"
DROPIN="$DROPIN_DIR/50-durable-park-policy.conf"
dropin_tmp=$(mktemp)
trap 'rm -f "$dropin_tmp"' EXIT
sed "s|%h/durable-park/deploy/|$REPO/deploy/|" "$REPO/deploy/hermes-gateway-policy.conf" >"$dropin_tmp"
if ! as_hermes cmp -s - "$DROPIN" <"$dropin_tmp"; then
    as_hermes install -d -m 0755 "$DROPIN_DIR"
    as_hermes tee "$DROPIN" <"$dropin_tmp" >/dev/null
    as_hermes chmod 0644 "$DROPIN"
    log "installed $DROPIN"
fi
user_systemctl daemon-reload

# --- 4b. gateway bridge ------------------------------------------------------------------------------------
CHANGED=0
# The bridge imports durable_park.ledger (args_digest) and durable_park.gatecall (gateway_slug), which run
# durable_park/__init__.py -> formcheck -> _vendor/fastjsonschema:
# copy the WHOLE package (every .py plus the vendored text files, subdirectories kept, no __pycache__), and
# remove files the repo no longer has. Touches nothing outside $VENV/bridge and $VENV/plugin/durable_park.
sync_tree() {
    local src=$1 dst=$2 rel
    local -a want=()
    install -d -m 0755 -o root -g root "$dst"
    while IFS= read -r -d '' rel; do
        rel=${rel#./}
        want+=("$rel")
        if [[ ! -d "$dst/$(dirname "$rel")" ]]; then
            install -d -m 0755 -o root -g root "$dst/$(dirname "$rel")"
            CHANGED=1
        fi
        install_root "$src/$rel" "$dst/$rel" 0644
    done < <(cd "$src" && find . -type f -not -path '*/__pycache__/*' \
        \( -name '*.py' -o -name LICENSE.fastjsonschema -o -name README.md \) -print0)
    # stale files (a renamed or deleted module must not linger), then empty directories
    while IFS= read -r -d '' rel; do
        rel=${rel#./}
        [[ " ${want[*]} " == *" $rel "* ]] && continue
        rm -f -- "${dst:?}/$rel"
        CHANGED=1
    done < <(cd "$dst" && find . -type f -not -path '*/__pycache__/*' -print0)
    find "$dst" -mindepth 1 -type d -empty -not -name __pycache__ -delete
}
install -d -m 0755 -o root -g root "$VENV/plugin"
# The bridge's own user: no login, no home, and no group but its own (NOT dpwake, NOT hermes), so nothing the bot
# runs can signal the bridge or read its files.
if ! getent passwd dpbridge >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --user-group dpbridge
    log "created system user dpbridge"
fi
# systemd reads bridge_secret as root (LoadCredential), so the bot (group dpwake) no longer needs to: root only.
# Only an existing file is touched; Yaakov stores it, this script never creates it. A change restarts the bridge.
[[ -e "$ETC/bridge_secret" ]] && { [[ "$(stat -c '%a %U:%G' "$ETC/bridge_secret")" == "600 root:root" ]] || { chown root:root "$ETC/bridge_secret"; chmod 0600 "$ETC/bridge_secret"; CHANGED=1; }; }
sync_tree "$REPO/bridge" "$VENV/bridge"
sync_tree "$REPO/plugin/durable_park" "$VENV/plugin/durable_park"
install_root "$REPO/deploy/$BRIDGE_UNIT.socket" "/etc/systemd/system/$BRIDGE_UNIT.socket" 0644
install_root "$REPO/deploy/$BRIDGE_UNIT.service" "/etc/systemd/system/$BRIDGE_UNIT.service" 0644
[[ $CHANGED == 1 ]] && systemctl daemon-reload
systemctl enable --quiet "$BRIDGE_UNIT"
# Until the socket is active, an earlier bridge unit (User=hermes) may still bind 127.0.0.1:8791 itself, and the
# socket could not take the port: stop it first. Once the socket is active the service never holds the port.
if ! systemctl is-active --quiet "$BRIDGE_UNIT.socket"; then
    systemctl stop "$BRIDGE_UNIT"
    log "stopped $BRIDGE_UNIT so that the socket unit can take over its port"
fi
# The socket could not take 127.0.0.1:8791. The old bridge was stopped just above, so whatever holds the port now is
# not ours: with the bot's user that is something answering withHuman's webhook in the bridge's place. Show who holds
# it (ss prints process names and pids, never a secret) and stop.
bridge_socket_failed() {
    printf 'configure_hermes: holders of 127.0.0.1:8791 (ss -ltnp "sport = :8791"):\n' >&2
    ss -ltnp 'sport = :8791' >&2 || true
    die "could not start $BRIDGE_UNIT.socket: something else may hold 127.0.0.1:8791. This may be a SECURITY INCIDENT (a process answering withHuman's webhook in the bridge's place): tell the admin, look at the ss output above, do not continue and do not re-run this script until the holder is explained and stopped (docs/runbook.md, Health check)"
}
systemctl enable --now "$BRIDGE_UNIT.socket" || bridge_socket_failed
# The service reads both keys only at start (LoadCredential): restart it by hand after storing or rotating one.
# The socket keeps the port meanwhile.
restart_if_needed "$BRIDGE_UNIT"
# The health check ships root-owned beside the other root-owned copies (its presence in the checkout is checked at the top).
install -d -m 0755 -o root -g root "$VENV/deploy"
install_root "$REPO/deploy/bridge_check.py" "$VENV/deploy/bridge_check.py" 0755
# Health gate: `systemctl is-active` right after a restart passes before a crash-looping unit first dies, so also
# demand a live app: an unsigned POST must be rejected with 401. Stop here (no gateway restart) if it is not.
bridge_ok=0
for _ in $(seq 1 10); do
    if systemctl is-active --quiet "$BRIDGE_UNIT" \
        && [[ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' -X POST "$BRIDGE_LOCAL" || true)" == 401 ]]; then
        bridge_ok=1; break
    fi
    sleep 1
done
if [[ $bridge_ok != 1 ]]; then
    journalctl -u "$BRIDGE_UNIT" -n 20 --no-pager 2>&1 | grep -v -i secret >&2 || true
    die "bridge unit is not answering 401 on $BRIDGE_LOCAL within 10s (last 20 journal lines above); not continuing"
fi
log "bridge unit: $(systemctl is-active "$BRIDGE_UNIT"), answers 401 to an unsigned request"
# Isolation check (own user, socket holds the port, no other listener or copy, secret modes): any FAIL stops here.
python3 -I "$VENV/deploy/bridge_check.py" || die "bridge_check.py failed: the bridge is not isolated as designed (FAIL lines above); not continuing"

# --- 5. sweep wrapper + cron -------------------------------------------------------------------------------
as_hermes install -d -m 0700 "$HERMES_HOME/scripts"
wrapper=$(mktemp)
cat >"$wrapper" <<WRAPPER
#!/bin/sh
# durable-park sweep for Hermes cron (every 5 minutes on the wall clock, --no-agent). Written by
# deploy/configure_hermes.sh; edits are overwritten. A real file because cron refuses symlinks out of ~/.hermes/scripts and cannot pass
# arguments. The sweep uses the standard library only.
exec /usr/bin/python3 -I $REPO/scripts/durable_park_sweep.py --ledger $LEDGER --wake-secret-file $ETC/wake_secret --retention-days 30
WRAPPER
install -m 0750 -o "$HERMES_USER" -g "$HERMES_USER" "$wrapper" "$SWEEP_WRAPPER"
rm -f "$wrapper"
cron_jobs=$(hermes_cli cron list --all 2>/dev/null) || die "hermes cron list failed; not creating a job blindly"
has "$cron_jobs" -E "Scheduled Jobs|No scheduled jobs" \
    || die "hermes cron list printed no job list; not creating a job blindly"
# A cron expression, not `every 5m`: Hermes re-arms an interval from the previous run's time, so with its 60 s
# ticker phase `every 5m` ran every 6 minutes; a cron expression is anchored to the wall clock (cron/jobs.py
# compute_next_run), so the sweep runs every 5 minutes (each run still starts up to 60 s after the boundary).
CRON_SCHEDULE='*/5 * * * *'
# `hermes cron list --all` prints one block per job: "  <id> [state]", then "Name:" and "Schedule:" lines.
# Prints "<id><TAB><schedule>" of the job whose name is $name (nothing when there is none).
# shellcheck disable=SC2016  # an awk program, not for bash to expand
CRON_JOB_AWK='
/^[[:space:]]*[0-9a-f]+[[:space:]]+\[/ { id = $1; named = 0 }
$1 == "Name:" { named = ($2 == name) }
named && $1 == "Schedule:" { sub(/^[[:space:]]*Schedule:[[:space:]]+/, ""); sub(/[[:space:]]+$/, ""); print id "\t" $0; exit }
'
if has "$cron_jobs" -E -- "^[[:space:]]*Name:[[:space:]]+${CRON_NAME}[[:space:]]*\$"; then
    cron_job=$(awk -v name="$CRON_NAME" "$CRON_JOB_AWK" <<<"$cron_jobs")
    [[ -n "$cron_job" ]] || die "cron job $CRON_NAME is listed but its id and schedule could not be read; not editing it blindly"
    job_id=${cron_job%%$'\t'*}
    job_schedule=${cron_job#*$'\t'}
    if [[ "$job_schedule" != "$CRON_SCHEDULE" ]]; then
        hermes_cli cron edit "$job_id" --schedule "$CRON_SCHEDULE"
        log "cron job $CRON_NAME ($job_id) moved from '$job_schedule' to '$CRON_SCHEDULE'"
    fi
else
    hermes_cli cron create "$CRON_SCHEDULE" --no-agent --script "$(basename "$SWEEP_WRAPPER")" \
        --deliver telegram --name "$CRON_NAME"
    log "created cron job $CRON_NAME"
fi

# --- 6. Funnel ---------------------------------------------------------------------------------------------
funnel_status=$(tailscale funnel status 2>/dev/null || true)
if ! has "$funnel_status" -F -- "$FUNNEL_PATH"; then
    tailscale funnel --bg --set-path="$FUNNEL_PATH" "$RECEIVER_LOCAL"
fi
if ! has "$funnel_status" -F -- "$BRIDGE_FUNNEL_PATH"; then
    tailscale funnel --bg --set-path="$BRIDGE_FUNNEL_PATH" "$BRIDGE_LOCAL"
fi
if ! has "$funnel_status" -F -- "$PAYLINK_FUNNEL_PATH "; then
    tailscale funnel --bg --set-path="$PAYLINK_FUNNEL_PATH" "$PAYLINK_LOCAL"
fi

# --- 7. restart Hermes -------------------------------------------------------------------------------------
dpwake_gid=$(getent group dpwake | cut -d: -f3)
gateway_pid=$(user_systemctl show -p MainPID --value hermes-gateway 2>/dev/null || echo 0)
since=$(date '+%Y-%m-%d %H:%M:%S')
gateway_groups=$(grep -E '^Groups:' "/proc/${gateway_pid:-0}/status" 2>/dev/null || true)
if [[ "${gateway_pid:-0}" != 0 ]] && has "$gateway_groups" -w -- "$dpwake_gid"; then
    user_systemctl restart hermes-gateway
    log "restarted hermes-gateway"
else
    # A user unit cannot add supplementary groups itself: restart the hermes user manager so its services
    # (hermes-gateway, and the cron scripts it runs) carry the new dpwake membership.
    systemctl restart "user@$HERMES_UID.service"
    log "restarted user@$HERMES_UID.service (new dpwake membership)"
fi
for _ in $(seq 1 120); do
    if user_systemctl is-active --quiet hermes-gateway && has "$(log_since "$since")" -F "durable-park loaded"; then
        break
    fi
    sleep 1
done
has "$(log_since "$since")" -F "durable-park loaded" \
    || die "hermes-gateway did not log 'durable-park loaded' within 120 s (see $HERMES_HOME/logs/agent.log)"
if has "$(log_since "$since")" -F "Failed to load plugin '$PLUGIN_ID'"; then
    die "a Hermes process failed to load $PLUGIN_ID after the restart (see $HERMES_HOME/logs/agent.log)"
fi
log "gateway: $(user_systemctl is-active hermes-gateway), durable-park loaded"
policy_report=$(tool_policy check) || { printf '%s\n' "$policy_report" >&2; die "agent tool policy check failed"; }
log "agent tools per platform: $(grep -E '^(cli|telegram|cron|webhook|api_server):' <<<"$policy_report" | tr '\n' ';')"
log "hermes-gateway unit $(user_systemctl show -p Restart hermes-gateway)"
log "done. Next (human): docs/receiver-setup.md (provisioner token, then provision.py)"
