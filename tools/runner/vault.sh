#!/usr/bin/env bash
# Seal an exchange API key into the local vault.
#
# The secret is never passed on a command line: it is read from the environment
# variable named by --api-secret-env, or typed at a hidden prompt. It reaches the
# container through the environment and is encrypted there with this machine's
# age key; only the sealed file is written to disk.
#
# Usage: vault.sh --arx-root DIR --image IMAGE --tenant-id ID --credential-id ID
#                 [--api-key KEY] [--api-secret-env VAR]
set -euo pipefail

ARX_ROOT="" IMAGE="" TENANT_ID="" CREDENTIAL_ID="" API_KEY="${API_KEY:-}" API_SECRET_ENV=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --arx-root) ARX_ROOT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --tenant-id) TENANT_ID="$2"; shift 2 ;;
    --credential-id) CREDENTIAL_ID="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --api-secret-env) API_SECRET_ENV="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
for name in ARX_ROOT IMAGE TENANT_ID CREDENTIAL_ID; do
  if [ -z "${!name}" ]; then echo "missing --$(echo "$name" | tr 'A-Z_' 'a-z-')" >&2; exit 2; fi
done

RECIPIENT="$(bash "$(dirname "$0")/age_key.sh" "$ARX_ROOT")"

# The vault requires a scope digest. A signed deployment would supply the one it
# binds this credential to; the local lane has no signed deployment and its reader
# never compares the field, so a digest is derived from what the credential is for.
# The only code that reads the field refuses on mismatch, so a derived value can
# make a signed deployment decline this credential but never accept one it should not.
SCOPE_DIGEST="$(printf 'custos-offline-lane/%s/%s/trade_no_withdraw' \
  "$TENANT_ID" "$CREDENTIAL_ID" | shasum -a 256 | cut -d' ' -f1)"

if [ -z "$API_KEY" ]; then
  read -r -p "Exchange API key: " API_KEY
fi
if [ -n "$API_SECRET_ENV" ]; then
  API_SECRET="${!API_SECRET_ENV:-}"
else
  read -r -s -p "Exchange API secret: " API_SECRET
  echo
fi
if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
  echo "the API key and secret must both be set" >&2
  exit 1
fi

export CUSTOS_API_KEY="$API_KEY" CUSTOS_API_SECRET="$API_SECRET"
docker run --rm \
  -v "$ARX_ROOT:/home/custos/.arx" \
  -e CUSTOS_API_KEY -e CUSTOS_API_SECRET \
  --entrypoint /bin/sh "$IMAGE" \
  -c 'exec arx-runner vault put --tenant-id "$1" --key-id "$2" \
        --api-key "$CUSTOS_API_KEY" --api-secret-env CUSTOS_API_SECRET \
        --age-recipient "$3" --scope-digest "$4" \
        --permission-scope trade_no_withdraw --vault-dir /home/custos/.arx/vault' \
  vault-put "$TENANT_ID" "$CREDENTIAL_ID" "$RECIPIENT" "$SCOPE_DIGEST"

echo "[runner] sealed $CREDENTIAL_ID into $ARX_ROOT/vault/"
