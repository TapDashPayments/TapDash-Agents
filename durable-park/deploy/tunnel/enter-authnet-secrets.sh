#!/usr/bin/env bash
# Interactive: writes /etc/durable-park/authnet.env (0600 root:root) from two hidden prompts.
# Run:  sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/enter-authnet-secrets.sh
# Values are read with `read -rs` into shell variables only: never echoed, logged, put on a command line,
# or stored in history. Prints "saved N values".
# Until Authorize.net sandbox access arrives, enter the placeholder `pending` for both. Every Authorize.net call
# then fails with a clear error (the sandbox refuses the made-up login).
# Not asked for here: AUTHNET_ENV is fixed to `sandbox` in tunnel.yaml (production is a reviewed change to that file),
# and the sandbox addresses are the server's own defaults.
set -euo pipefail
set +x
umask 077

if [[ ${EUID} -ne 0 ]]; then echo "run with sudo" >&2; exit 1; fi
if [[ ! -t 0 ]]; then echo "run interactively (stdin must be a terminal)" >&2; exit 1; fi

DIR=${DURABLE_PARK_ETC_DIR:-/etc/durable-park}   # override only for testing
FILE=$DIR/authnet.env

install -d -m 0755 -o root -g root "$DIR"

if [[ -e $FILE ]]; then
  read -r -p "$FILE exists. Overwrite? [y/N] " yn
  [[ $yn == [yY]* ]] || { echo "aborted"; exit 1; }
fi

tmp=$(mktemp "$DIR/.authnet.env.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
chmod 0600 "$tmp"

names=(AUTHNET_LOGIN_ID AUTHNET_TRANSACTION_KEY)
labels=("Authorize.net API Login ID" "Authorize.net Transaction Key")
# The server's own limits (config.py): the API Login ID is 1 to 20 characters, the Transaction Key 1 to 16.
longest=(20 16)

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
    if (( ${#val} > longest[i] )); then
      echo "  at most ${longest[$i]} characters - try again"; continue
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
