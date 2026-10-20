#!/usr/bin/env bash
# Interactive: writes /etc/durable-park/tunnel.env (0600 root:root) from five
# hidden prompts. Run:  sudo bash /home/hermes/durable-park/deploy/tunnel/enter-secrets.sh
# Values are read with `read -rs` into shell variables only: never echoed,
# logged, put on a command line, or stored in history. Prints "saved N values".
set -euo pipefail
set +x
umask 077

if [[ ${EUID} -ne 0 ]]; then echo "run with sudo" >&2; exit 1; fi
if [[ ! -t 0 ]]; then echo "run interactively (stdin must be a terminal)" >&2; exit 1; fi

DIR=${DURABLE_PARK_ETC_DIR:-/etc/durable-park}   # override only for testing
FILE=$DIR/tunnel.env

install -d -m 0755 -o root -g root "$DIR"

if [[ -e $FILE ]]; then
  read -r -p "$FILE exists. Overwrite? [y/N] " yn
  [[ $yn == [yY]* ]] || { echo "aborted"; exit 1; }
fi

tmp=$(mktemp "$DIR/.tunnel.env.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
chmod 0600 "$tmp"

names=(QUICKBOOKS_CLIENT_ID QUICKBOOKS_CLIENT_SECRET QUICKBOOKS_REFRESH_TOKEN QUICKBOOKS_REALM_ID WITHHUMAN_TUNNEL_TOKEN)
labels=("QuickBooks Client ID" "QuickBooks Client Secret" "QuickBooks Refresh token" "QuickBooks Realm ID" "withHuman tunnel token")

n=0
for i in "${!names[@]}"; do
  while true; do
    read -rs -p "${labels[$i]}: " val
    echo
    # trim surrounding whitespace (paste artifacts)
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    if [[ -z $val ]]; then echo "  empty - try again"; continue; fi
    # env_file-safe: no whitespace/control chars, quotes, $, #, backslash or backtick
    if [[ $val =~ [[:space:][:cntrl:]\'\"\$#\\\`] ]]; then
      echo "  contains a character that is not allowed in the env file - try again"; continue
    fi
    break
  done
  printf '%s=%s\n' "${names[$i]}" "$val" >> "$tmp"
  n=$((n+1))
  val=
done

chown root:root "$tmp"
chmod 0600 "$tmp"
mv -f -- "$tmp" "$FILE"
trap - EXIT
echo "saved $n values"
