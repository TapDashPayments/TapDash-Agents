#!/usr/bin/env bash
# Builds the tunnel image (QuickBooks + Maverick servers), but only from the pinned Maverick source.
# Run:  sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/build.sh
# It stops, building nothing, when:
#   - maverick-mcp.sha is not one 40-character commit,
#   - the checkout (MAVERICK_MCP_SRC, default /opt/maverick-mcp-src) is not owned by root, or anything in it is
#     owned by another user or writable by group or others (the agent's user must not be able to change it),
#   - the checkout has replace refs or grafts (they could make git show the pinned commit for other files),
#   - the checkout is at another commit,
#   - the checkout has any local change: an edit, a new file, or an ignored file (those would reach the image too),
#   - the image tag in docker-compose.yml does not end in -mav-<first 7 of the commit>.
# It only builds: it never starts, stops or replaces the running wh-tunnel container.
set -euo pipefail

die() { echo "build.sh: $*" >&2; exit 1; }

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SRC=${MAVERICK_MCP_SRC:-/opt/maverick-mcp-src}
# Testing only: the user the checkout must belong to (the tests build from a temporary repo they own).
OWNER=${MAVERICK_MCP_SRC_OWNER:-root}

want=$(tr -d '[:space:]' < "$HERE/maverick-mcp.sha")
[[ $want =~ ^[0-9a-f]{40}$ ]] || die "maverick-mcp.sha must hold one 40-character commit"

# The checkout is root's, so root's git trusts its config without safe.directory; replace refs and grafts are ignored.
[[ -d $SRC/.git ]] || die "$SRC is not a git checkout"
[[ $(stat -c %U "$SRC") == "$OWNER" ]] || die "$SRC must be owned by $OWNER (see gateway-setup.md, Maverick server source)"
foreign=$(find "$SRC" \( ! -user "$OWNER" -o -perm /022 \) -print -quit) || die "cannot scan $SRC"
[[ -z $foreign ]] || die "$SRC has a file owned by another user or writable by group or others: $foreign"
[[ ! -e $SRC/.git/info/grafts ]] || die "$SRC has .git/info/grafts; remove it"
g() { GIT_NO_REPLACE_OBJECTS=1 git --no-replace-objects -C "$SRC" "$@"; }
[[ -z $(g for-each-ref refs/replace/) ]] || die "$SRC has replace refs (refs/replace/); remove them"

have=$(g rev-parse HEAD) || die "cannot read the commit of $SRC"
[[ $have == "$want" ]] || die "$SRC is at $have, not the pinned commit $want (maverick-mcp.sha)"

dirty=$(g status --porcelain --ignored --untracked-files=all) || die "cannot read the status of $SRC"
[[ -z $dirty ]] || die "$SRC has local changes (edited, new or ignored files); the image must be built from the clean pinned commit"

images=$(grep -E '^[[:space:]]*image:' "$HERE/docker-compose.yml") || die "no image tag in docker-compose.yml"
while IFS= read -r line; do
  [[ $line == *"-mav-${want:0:7}" ]] || die "the image tag in docker-compose.yml does not end in -mav-${want:0:7}: $line"
done <<< "$images"

echo "maverick-mcp source: $SRC at $want (clean)"
cd "$HERE"
MAVERICK_MCP_SRC=$SRC docker compose build
