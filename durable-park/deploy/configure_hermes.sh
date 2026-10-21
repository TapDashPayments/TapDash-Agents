#!/usr/bin/env bash
# On proto-agents this script re-applies the current tool policy (v1 lockdown, re-applied 2026-10-21 03:16 UTC). It restarts the gateway: run it only when no ledger row is parked, approved or executing. See docs/specs/2026-10-21-all-tools-withhuman-design.md.
# durable-park deploy for the proto VM (Hermes Agent v0.22.2). Idempotent: safe to re-run.
#
#   sudo bash /home/hermes/durable-park/deploy/configure_hermes.sh
#
# What it does (each step checks before it changes anything):
#   1. system user aaprecv; group dpwake (members hermes, aaprecv); /etc/durable-park (0755 root)
#   2. /etc/durable-park/wake_secret: generated once (0640 root:dpwake); never printed. No bridge_secret.
#   3. receiver venv /opt/durable-park (agentapprovalprotocol + uvicorn + starlette, pinned) and a root-owned
#      copy of receiver/*.py; system unit durable-park-receiver (enabled + started: it answers 401 to every
#      event until provision.py has written /etc/durable-park/receiver.json); the Hermes secret-source helper
#      /opt/durable-park/libexec/hermes-secrets
#   4. Hermes (as user hermes, through Hermes's own CLI): plugin symlink; `hermes plugins enable` (PM
#      admission installs the plugin.yaml python_dependencies into Hermes's environment); plugin settings;
#      plugins.hook_callback_timeout 60; the local webhook platform (127.0.0.1:8644, route durable-park-wake,
#      deliver log, rate_limit 120) whose secret is ${env:DURABLE_PARK_WAKE_SECRET}, supplied at startup by
#      the `command` secret source (the helper above reads wake_secret, and instance_token once it exists, for
#      deploy/hermes_mcp.sh); no copy of a secret in config.yaml/.env
#   4b. gateway bridge: root-owned copy of bridge/ + plugin/durable_park under /opt/durable-park, system unit
#      durable-park-bridge (User=hermes, 127.0.0.1:8791); it answers 401 until Yaakov stores
#      /etc/durable-park/bridge_secret (never created here), and human while the ledger file is missing
#   4c. agent tool policy (deploy/hermes_tool_policy.py, run in Hermes's own Python): every platform key an
#      agent can run under gets only durable_park + clarify + MCP server wh (QuickBooks tools); every toolset
#      with local file/command/code access is disabled; model-named file delivery is limited to the Hermes
#      caches (closes the ledger bypass: the agent's tools run as hermes, which owns the ledger). Checked
#      before AND after the gateway restart; a failed pre-restart check aborts without restarting anything.
#   5. sweep wrapper ~/.hermes/scripts/durable_park_sweep.sh + cron job (every 5m, --no-agent, telegram)
#   6. Tailscale Funnel /aap/events -> 127.0.0.1:8790/aap/events and /wh/bridge -> 127.0.0.1:8791/wh/bridge
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
SECRETS_HELPER="$VENV/libexec/hermes-secrets"
# Earlier value of secrets.command.command (inline sed); migrated to SECRETS_HELPER.
OLD_SECRET_COMMAND="sed -n 's/^/DURABLE_PARK_WAKE_SECRET=/p' $ETC/wake_secret"
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
set_cfg "plugins.entries.$PLUGIN_ID.allow_gateway_injection" true
set_cfg "plugins.entries.$PLUGIN_ID.settings.aap_base_url" "$AAP_BASE_URL"
set_cfg "plugins.entries.$PLUGIN_ID.settings.token_file" "$ETC/instance_token"
set_cfg "plugins.entries.$PLUGIN_ID.settings.ledger_path" "$LEDGER"
set_cfg secrets.command.enabled true
set_cfg secrets.command.command "$SECRETS_HELPER"
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

as_hermes install -d -m 0750 "$(dirname "$LEDGER")"   # also the bridge unit's ReadWritePaths: must exist

# --- 4c. agent tool policy ---------------------------------------------------------------------------------
tool_policy apply || die "could not apply the agent tool policy"
# Check BEFORE any restart: a failed check stops here and the running gateway keeps its old process.
policy_report=$(tool_policy check) \
    || { printf '%s\n' "$policy_report" >&2; die "agent tool policy check failed (nothing restarted)"; }
log "agent tool policy ok before restart"

# --- 4b. gateway bridge ------------------------------------------------------------------------------------
CHANGED=0
install -d -m 0755 -o root -g root "$VENV/bridge" "$VENV/plugin" "$VENV/plugin/durable_park"
for f in "$REPO"/bridge/*.py; do
    install_root "$f" "$VENV/bridge/$(basename "$f")" 0644
done
for f in "$REPO"/plugin/durable_park/*.py; do   # the bridge imports durable_park.ledger
    install_root "$f" "$VENV/plugin/durable_park/$(basename "$f")" 0644
done
install_root "$REPO/deploy/$BRIDGE_UNIT.service" "/etc/systemd/system/$BRIDGE_UNIT.service" 0644
[[ $CHANGED == 1 ]] && systemctl daemon-reload
systemctl enable --quiet "$BRIDGE_UNIT"
restart_if_needed "$BRIDGE_UNIT"   # it reads its secret only at start: restart it by hand after storing one
[[ -e "$ETC/bridge_secret" ]] || log "no $ETC/bridge_secret yet: the bridge answers 401 (Yaakov stores it)"
[[ -f "$LEDGER" ]] || log "ledger $LEDGER does not exist yet: the bridge answers human until the plugin creates it"
log "bridge unit: $(systemctl is-active "$BRIDGE_UNIT")"

# --- 5. sweep wrapper + cron -------------------------------------------------------------------------------
as_hermes install -d -m 0700 "$HERMES_HOME/scripts"
wrapper=$(mktemp)
cat >"$wrapper" <<WRAPPER
#!/bin/sh
# durable-park sweep for Hermes cron (every 5m, --no-agent). Written by deploy/configure_hermes.sh; edits
# are overwritten. A real file because cron refuses symlinks out of ~/.hermes/scripts and cannot pass
# arguments. The sweep uses the standard library only.
exec /usr/bin/python3 -I $REPO/scripts/durable_park_sweep.py --ledger $LEDGER --wake-secret-file $ETC/wake_secret --retention-days 30
WRAPPER
install -m 0750 -o "$HERMES_USER" -g "$HERMES_USER" "$wrapper" "$SWEEP_WRAPPER"
rm -f "$wrapper"
cron_jobs=$(hermes_cli cron list --all 2>/dev/null) || die "hermes cron list failed; not creating a job blindly"
has "$cron_jobs" -E "Scheduled Jobs|No scheduled jobs" \
    || die "hermes cron list printed no job list; not creating a job blindly"
if ! has "$cron_jobs" -E -- "^[[:space:]]*Name:[[:space:]]+${CRON_NAME}[[:space:]]*\$"; then
    hermes_cli cron create "every 5m" --no-agent --script "$(basename "$SWEEP_WRAPPER")" \
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
