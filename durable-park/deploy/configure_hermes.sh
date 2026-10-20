#!/usr/bin/env bash
# durable-park deploy for the proto VM (Hermes Agent v0.22.2). Idempotent: safe to re-run.
#
#   sudo bash /home/hermes/durable-park/deploy/configure_hermes.sh
#
# What it does (each step checks before it changes anything):
#   1. system user aaprecv; group dpwake (members hermes, aaprecv); /etc/durable-park (0755 root)
#   2. /etc/durable-park/wake_secret: generated once (0640 root:dpwake); never printed. No bridge_secret.
#   3. receiver venv /opt/durable-park (agentapprovalprotocol + uvicorn + starlette, pinned) and a root-owned
#      copy of receiver/*.py; system unit durable-park-receiver (enabled + started: it answers 401 to every
#      event until provision.py has written /etc/durable-park/receiver.json)
#   4. Hermes (as user hermes, through Hermes's own CLI): plugin symlink; `hermes plugins enable` (PM
#      admission installs the plugin.yaml python_dependencies into Hermes's environment); plugin settings;
#      plugins.hook_callback_timeout 60; the local webhook platform (127.0.0.1:8644, route durable-park-wake,
#      deliver log, rate_limit 120) whose secret is ${env:DURABLE_PARK_WAKE_SECRET}, supplied at startup by
#      the `command` secret source from the wake_secret file (no copy of the secret in config.yaml/.env)
#   5. sweep wrapper ~/.hermes/scripts/durable_park_sweep.sh + cron job (every 5m, --no-agent, telegram)
#   6. Tailscale Funnel /aap/events -> 127.0.0.1:8790/aap/events
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
# Receiver venv pins: what the repo's tests run against.
RECEIVER_PACKAGES=("agentapprovalprotocol==0.3.3" "uvicorn==0.54.0" "starlette==1.9.0")

log() { printf 'configure_hermes: %s\n' "$*"; }
die() { printf 'configure_hermes: ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo bash $0)"
[[ -n "$HERMES_HOMEDIR" && -d "$HERMES_HOME" ]] || die "no Hermes home for user $HERMES_USER"
[[ -x "$HERMES_BIN" ]] || die "hermes launcher not found at $HERMES_BIN"
[[ -f "$REPO/plugin/durable_park/plugin.yaml" ]] || die "repo layout not found under $REPO"
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
log_since() { awk -v s="$1" 'substr($0, 1, 19) >= s' "$HERMES_HOME/logs/agent.log" 2>/dev/null || true; }

# --- 1. users, groups, directory --------------------------------------------------------------------------
getent group dpwake >/dev/null || { groupadd --system dpwake; log "created group dpwake"; }
if ! id -u aaprecv >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin aaprecv
    log "created system user aaprecv"
fi
for member in "$HERMES_USER" aaprecv; do
    if ! id -nG "$member" | tr ' ' '\n' | grep -qx dpwake; then
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
if [[ -e "$ETC/provisioner_token" ]]; then chown root:root "$ETC/provisioner_token"; chmod 0600 "$ETC/provisioner_token"; fi

# --- 3. receiver venv, code, unit --------------------------------------------------------------------------
if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv "$VENV"
    log "created venv $VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --no-input "${RECEIVER_PACKAGES[@]}"
install -d -m 0755 -o root -g root "$VENV/receiver"
for f in app.py wake_post.py provision.py; do
    install -m 0644 -o root -g root "$REPO/receiver/$f" "$VENV/receiver/$f"
done
install -m 0644 -o root -g root "$REPO/deploy/$UNIT.service" "/etc/systemd/system/$UNIT.service"
systemctl daemon-reload
systemctl enable --quiet "$UNIT"
systemctl restart "$UNIT"   # stateless: picks up new code and the current receiver.json
log "receiver unit: $(systemctl is-active "$UNIT")"

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

if ! hermes_cli config get plugins.enabled 2>/dev/null | grep -qE -- "^[[:space:]]*- ${PLUGIN_ID}[[:space:]]*\$"; then
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

# The secret source runs this at every Hermes start: it reads the wake secret (group dpwake) and hands
# DURABLE_PARK_WAKE_SECRET=<value> to Hermes over a pipe. Never replace someone else's command source.
SECRET_COMMAND="sed -n 's/^/DURABLE_PARK_WAKE_SECRET=/p' $ETC/wake_secret"
current_command=$(hermes_cli config get secrets.command.command 2>/dev/null || true)
if [[ -n "$current_command" && "$current_command" != "Config key not set"* && "$current_command" != "$SECRET_COMMAND" ]]; then
    die "secrets.command.command is already set to another helper; merge it with: $SECRET_COMMAND"
fi

set_cfg() { hermes_cli config set "$1" "$2" >/dev/null; }
set_cfg plugins.hook_callback_timeout 60
set_cfg "plugins.entries.$PLUGIN_ID.allow_gateway_injection" true
set_cfg "plugins.entries.$PLUGIN_ID.settings.aap_base_url" "$AAP_BASE_URL"
set_cfg "plugins.entries.$PLUGIN_ID.settings.token_file" "$ETC/instance_token"
set_cfg "plugins.entries.$PLUGIN_ID.settings.ledger_path" "$LEDGER"
set_cfg secrets.command.enabled true
set_cfg secrets.command.command "$SECRET_COMMAND"
set_cfg platforms.webhook.enabled true
set_cfg platforms.webhook.extra.host 127.0.0.1
set_cfg platforms.webhook.extra.port 8644
set_cfg platforms.webhook.extra.rate_limit 120
# shellcheck disable=SC2016  # a literal Hermes ${env:...} reference, expanded by Hermes, not by bash
set_cfg "platforms.webhook.extra.routes.$ROUTE.secret" '${env:DURABLE_PARK_WAKE_SECRET}'
set_cfg "platforms.webhook.extra.routes.$ROUTE.events" '["wake"]'
set_cfg "platforms.webhook.extra.routes.$ROUTE.deliver" log
set_cfg "platforms.webhook.extra.routes.$ROUTE.prompt" \
    "durable-park wake event. The durable-park plugin handles it; if you see this, reply only: ignored."
log "Hermes config set (plugin settings, hook timeout, webhook route $ROUTE, secret source)"

as_hermes install -d -m 0750 "$(dirname "$LEDGER")"

# --- 5. sweep wrapper + cron -------------------------------------------------------------------------------
as_hermes install -d -m 0700 "$HERMES_HOME/scripts"
wrapper=$(mktemp)
cat >"$wrapper" <<WRAPPER
#!/bin/sh
# durable-park sweep for Hermes cron (every 5m, --no-agent). Written by deploy/configure_hermes.sh; edits
# are overwritten. A real file because cron refuses symlinks out of ~/.hermes/scripts and cannot pass
# arguments. The sweep uses the standard library only.
exec /usr/bin/python3 -I $REPO/scripts/durable_park_sweep.py --ledger $LEDGER --wake-secret-file $ETC/wake_secret
WRAPPER
install -m 0750 -o "$HERMES_USER" -g "$HERMES_USER" "$wrapper" "$SWEEP_WRAPPER"
rm -f "$wrapper"
if ! hermes_cli cron list --all 2>/dev/null | grep -q -- "$CRON_NAME"; then
    hermes_cli cron create "every 5m" --no-agent --script "$(basename "$SWEEP_WRAPPER")" \
        --deliver telegram --name "$CRON_NAME"
    log "created cron job $CRON_NAME"
fi

# --- 6. Funnel ---------------------------------------------------------------------------------------------
if ! tailscale funnel status 2>/dev/null | grep -qF -- "$FUNNEL_PATH"; then
    tailscale funnel --bg --set-path="$FUNNEL_PATH" "$RECEIVER_LOCAL"
fi

# --- 7. restart Hermes -------------------------------------------------------------------------------------
dpwake_gid=$(getent group dpwake | cut -d: -f3)
gateway_pid=$(user_systemctl show -p MainPID --value hermes-gateway 2>/dev/null || echo 0)
since=$(date '+%Y-%m-%d %H:%M:%S')
if [[ "${gateway_pid:-0}" != 0 ]] && grep -E '^Groups:' "/proc/$gateway_pid/status" | grep -qw -- "$dpwake_gid"; then
    user_systemctl restart hermes-gateway
    log "restarted hermes-gateway"
else
    # A user unit cannot add supplementary groups itself: restart the hermes user manager so its services
    # (hermes-gateway, and the cron scripts it runs) carry the new dpwake membership.
    systemctl restart "user@$HERMES_UID.service"
    log "restarted user@$HERMES_UID.service (new dpwake membership)"
fi
for _ in $(seq 1 120); do
    if user_systemctl is-active --quiet hermes-gateway && log_since "$since" | grep -q "durable-park loaded"; then
        break
    fi
    sleep 1
done
log_since "$since" | grep -q "durable-park loaded" \
    || die "hermes-gateway did not log 'durable-park loaded' within 120 s (see $HERMES_HOME/logs/agent.log)"
if log_since "$since" | grep -q "Failed to load plugin '$PLUGIN_ID'"; then
    die "a Hermes process failed to load $PLUGIN_ID after the restart (see $HERMES_HOME/logs/agent.log)"
fi
log "gateway: $(user_systemctl is-active hermes-gateway), durable-park loaded"
log "hermes-gateway unit $(user_systemctl show -p Restart hermes-gateway)"
log "done. Next (human): docs/receiver-setup.md (provisioner token, then provision.py)"
