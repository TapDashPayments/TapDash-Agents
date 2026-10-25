#!/usr/bin/env bash
# Builds the tunnel image (QuickBooks + Maverick + accept.blue + Authorize.net servers), but only from the pinned
# TapDash sources.
# Run:  sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/build.sh
# It stops, building nothing, when, for any of the three checkouts (maverick-mcp, acceptblue-mcp, authorizenet-mcp):
#   - its .sha file (maverick-mcp.sha, acceptblue-mcp.sha, authorizenet-mcp.sha) is not one 40-character commit,
#   - the checkout (default /opt/maverick-mcp-src, /opt/acceptblue-mcp-src, /opt/authorizenet-mcp-src; moved by
#     MAVERICK_MCP_SRC, ACCEPTBLUE_MCP_SRC, AUTHORIZENET_MCP_SRC) is not owned by root, or anything in it is
#     owned by another user or writable by group or others (the agent's user must not be able to change it),
#   - the checkout has replace refs or grafts (they could make git show the pinned commit for other files),
#   - the checkout is at another commit,
#   - the checkout has any local change: an edit, a new file, or an ignored file (those would reach the image too),
# or when an image tag in docker-compose.yml does not end in -mav-<first 7 of the Maverick commit>-ab-<first 7 of
# the accept.blue commit>-an-<first 7 of the Authorize.net commit>.
# After the build it proves what each server inside the new image says about its tools: it runs
#   docker run --rm --pull=never --entrypoint <launcher> <image> --list-approvals
# as root for maverick-mcp-server, acceptblue-mcp-server and authnet-mcp-server and compares the output byte for
# byte with maverick-approvals.json, acceptblue-approvals.json and authnet-approvals.json in this folder. Any
# difference (or a launcher that does not run) stops it with a non-zero exit: do not run that image. The withHuman
# pipeline's auto rules are written from these files (tests check that), so the image is proved to agree with them.
# It only builds and checks: it never starts, stops or replaces the running wh-tunnel container.
set -euo pipefail

die() { echo "build.sh: $*" >&2; exit 1; }

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Testing only: the user the checkouts must belong to (the tests build from temporary repos they own).
OWNER=${TUNNEL_SRC_OWNER:-root}

# Checks one checkout against its pin. Sets WANT and SRC for the caller.
#   $1 sha file, $2 environment variable that moves the checkout, $3 default checkout
check_source() {
  local sha_file=$1 src_var=$2 default=$3 foreign dirty have
  WANT=$(tr -d '[:space:]' < "$HERE/$sha_file")
  [[ $WANT =~ ^[0-9a-f]{40}$ ]] || die "$sha_file must hold one 40-character commit"
  SRC=${!src_var:-$default}

  # The checkout is root's, so root's git trusts its config without safe.directory; replace refs and grafts are ignored.
  [[ -d $SRC/.git ]] || die "$SRC is not a git checkout"
  [[ $(stat -c %U "$SRC") == "$OWNER" ]] || die "$SRC must be owned by $OWNER (see gateway-setup.md, Payment server sources)"
  foreign=$(find "$SRC" \( ! -user "$OWNER" -o -perm /022 \) -print -quit) || die "cannot scan $SRC"
  [[ -z $foreign ]] || die "$SRC has a file owned by another user or writable by group or others: $foreign"
  [[ ! -e $SRC/.git/info/grafts ]] || die "$SRC has .git/info/grafts; remove it"
  g() { GIT_NO_REPLACE_OBJECTS=1 git --no-replace-objects -C "$SRC" "$@"; }
  [[ -z $(g for-each-ref refs/replace/) ]] || die "$SRC has replace refs (refs/replace/); remove them"

  have=$(g rev-parse HEAD) || die "cannot read the commit of $SRC"
  [[ $have == "$WANT" ]] || die "$SRC is at $have, not the pinned commit $WANT ($sha_file)"

  dirty=$(g status --porcelain --ignored --untracked-files=all) || die "cannot read the status of $SRC"
  [[ -z $dirty ]] || die "$SRC has local changes (edited, new or ignored files); the image must be built from the clean pinned commit"
  echo "${sha_file%.sha} source: $SRC at $WANT (clean)"
}

check_source maverick-mcp.sha MAVERICK_MCP_SRC /opt/maverick-mcp-src
MAVERICK_SHA=$WANT MAVERICK_DIR=$SRC
check_source acceptblue-mcp.sha ACCEPTBLUE_MCP_SRC /opt/acceptblue-mcp-src
ACCEPTBLUE_SHA=$WANT ACCEPTBLUE_DIR=$SRC
check_source authorizenet-mcp.sha AUTHORIZENET_MCP_SRC /opt/authorizenet-mcp-src
AUTHNET_SHA=$WANT AUTHNET_DIR=$SRC

suffix="-mav-${MAVERICK_SHA:0:7}-ab-${ACCEPTBLUE_SHA:0:7}-an-${AUTHNET_SHA:0:7}"
images=$(grep -E '^[[:space:]]*image:' "$HERE/docker-compose.yml") || die "no image tag in docker-compose.yml"
while IFS= read -r line; do
  [[ $line == *"$suffix" ]] || die "the image tag in docker-compose.yml does not end in $suffix: $line"
done <<< "$images"

cd "$HERE"
MAVERICK_MCP_SRC=$MAVERICK_DIR ACCEPTBLUE_MCP_SRC=$ACCEPTBLUE_DIR AUTHORIZENET_MCP_SRC=$AUTHNET_DIR docker compose build

# The build worked. Now prove what the image says about each tool (auto = no person, you = a person approves).
image=$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' docker-compose.yml | sort -u)
[[ -n $image && $image != *$'\n'* ]] || die "docker-compose.yml must name one image tag, the same for both services"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

# $1 name, $2 launcher in the image, $3 file with the expected output
check_approvals() {
  local got=$scratch/$1.json want=$HERE/$3
  [[ -f $want ]] || die "$3 is missing: it holds what $2 --list-approvals must print"
  docker run --rm --pull=never --entrypoint "$2" "$image" --list-approvals > "$got" \
    || die "$2 --list-approvals failed in $image; do not run this image"
  if ! cmp -s "$got" "$want"; then
    diff -u "$want" "$got" >&2 || true
    die "$2 --list-approvals in $image differs from $3 (above: - is the committed file, + is the image); do not run this image. If the pinned server really changed its approvals, review it and update the pipeline rules, tests/payments_servers.py and $3 together"
  fi
  echo "$1 approvals: ok ($3 equals what $2 --list-approvals prints in the image)"
}

check_approvals maverick maverick-mcp-server maverick-approvals.json
check_approvals acceptblue acceptblue-mcp-server acceptblue-approvals.json
check_approvals authnet authnet-mcp-server authnet-approvals.json
