#!/usr/bin/env bash
# Interactive: writes /etc/durable-park/acceptblue.env (0600 root:root) from three hidden prompts.
# Run:  sudo bash /home/hermes/tapdash-agents/durable-park/deploy/tunnel/enter-acceptblue-secrets.sh
# Values are read with `read -rs` into shell variables only: never echoed, logged, put on a command line,
# or stored in history. Prints "saved N values".
# Until accept.blue sandbox access arrives, enter the placeholders: source key `pending`, PIN `pending`, and the
# sandbox address `https://api.sandbox.accept.blue/api/v2`. The server refuses every address that is not on
# accept.blue (so a made-up `.invalid` address cannot be used), so the placeholder address is the real sandbox:
# accept.blue's sandbox answers every call with a refusal, because the key is not real.
set -euo pipefail
set +x
umask 077

if [[ ${EUID} -ne 0 ]]; then echo "run with sudo" >&2; exit 1; fi
if [[ ! -t 0 ]]; then echo "run interactively (stdin must be a terminal)" >&2; exit 1; fi

DIR=${DURABLE_PARK_ETC_DIR:-/etc/durable-park}   # override only for testing
FILE=$DIR/acceptblue.env

install -d -m 0755 -o root -g root "$DIR"

if [[ -e $FILE ]]; then
  read -r -p "$FILE exists. Overwrite? [y/N] " yn
  [[ $yn == [yY]* ]] || { echo "aborted"; exit 1; }
fi

tmp=$(mktemp "$DIR/.acceptblue.env.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT
chmod 0600 "$tmp"

names=(ACCEPTBLUE_SOURCE_KEY ACCEPTBLUE_PIN ACCEPTBLUE_BASE_URL)
labels=("accept.blue source key" "accept.blue PIN" "accept.blue API address (https://<host>.accept.blue/api/v2)")
# The server's own rule for the address (config.py): https, a host on accept.blue, the path /api/v2, nothing else.
url_rule='^https://([A-Za-z0-9-]+\.)*accept\.blue/api/v2/?$'

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
    if [[ ${names[$i]} == *_URL && $val != https://?* ]]; then
      echo "  must start with https:// - try again"; continue
    fi
    if [[ ${names[$i]} == ACCEPTBLUE_BASE_URL && ! $val =~ $url_rule ]]; then
      echo "  must be https://<host>.accept.blue/api/v2 - try again"; continue
    fi
    # a key that starts with pk_ is a public key for web pages only; the server refuses it at start
    if [[ ${names[$i]} == ACCEPTBLUE_SOURCE_KEY && ${val,,} == pk_* ]]; then
      echo "  that is a public web-page key (pk_...), not a source key - try again"; continue
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
