#!/usr/bin/env bash
# Seal an exchange API key into the local vault.
#
# OKX keys also carry a passphrase, which is asked for, or read from the variable
# named by --api-passphrase-env, when --connector names an OKX market.
# The secret is never passed on a command line: it is read from the environment
# variable named by --api-secret-env, or typed at a hidden prompt. It reaches the
# container through the environment and is encrypted there with this machine's
# age key; only the sealed file is written to disk.
#
# Before asking, it says which exchange the key is for, as the strategy's
# config.yaml names it in trading.connector, and which kind of key each mode needs.
#
# Usage: vault.sh --arx-root DIR --image IMAGE --tenant-id ID --credential-id ID
#                 [--strategy PATH] [--connector NAME] [--api-key KEY]
#                 [--api-secret-env VAR] [--api-passphrase-env VAR] [--replace]
#
# The runner never overwrites a sealed key. --replace removes the sealed one, but
# only once the new key has been read, so a failed prompt leaves the old in place.
set -euo pipefail

ARX_ROOT="" IMAGE="" TENANT_ID="" CREDENTIAL_ID="" CONNECTOR="" STRATEGY="" REPLACE=""
API_KEY="${API_KEY:-}" API_SECRET_ENV="" API_PASSPHRASE_ENV=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --arx-root) ARX_ROOT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --tenant-id) TENANT_ID="$2"; shift 2 ;;
    --credential-id) CREDENTIAL_ID="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --api-secret-env) API_SECRET_ENV="$2"; shift 2 ;;
    --api-passphrase-env) API_PASSPHRASE_ENV="$2"; shift 2 ;;
    --connector) CONNECTOR="$2"; shift 2 ;;
    --strategy) STRATEGY="$2"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
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

# Where each mode's key comes from. Testnet runs against each exchange's own test
# environment, which issues keys separate from the live account's.
case "$CONNECTOR" in
  binance) EXCHANGE="Binance spot"; TESTNET_KEY="a key from the Binance spot testnet" ;;
  binance_perpetual)
    EXCHANGE="Binance USDⓈ-M perpetual"
    TESTNET_KEY="a key from the Binance USDⓈ-M futures testnet" ;;
  okx) EXCHANGE="OKX spot"; TESTNET_KEY="a key created in OKX demo trading" ;;
  okx_perpetual) EXCHANGE="OKX perpetual swap"; TESTNET_KEY="a key created in OKX demo trading" ;;
  sodex) EXCHANGE="SoDEX spot"; TESTNET_KEY="a key registered on SoDEX testnet" ;;
  sodex_perpetual) EXCHANGE="SoDEX perpetual"; TESTNET_KEY="a key registered on SoDEX testnet" ;;
  *) EXCHANGE="the exchange"; TESTNET_KEY="a key from the exchange's test environment" ;;
esac
EXCHANGE_NAME="${EXCHANGE%% *}"
[ "$EXCHANGE" = "the exchange" ] && EXCHANGE_NAME="Exchange"

SEALED="$ARX_ROOT/vault/$CREDENTIAL_ID.enc"
if [ -e "$SEALED" ] && [ -z "$REPLACE" ]; then
  echo "A key is already sealed for ${STRATEGY:-this strategy} as credential $CREDENTIAL_ID." >&2
  echo "To seal another in its place, run this again with REPLACE=1." >&2
  exit 1
fi

echo "Sealing the exchange key for ${STRATEGY:-this strategy} as credential $CREDENTIAL_ID."
if [ -n "$CONNECTOR" ]; then
  echo "  Exchange: $EXCHANGE ($CONNECTOR, from trading.connector in config.yaml)"
fi
echo "  MODE=sandbox never sends the key to the exchange: any values will do."
echo "  MODE=testnet needs $TESTNET_KEY."
echo "  Give the key trading permission only, never withdrawal."
echo "  To use another key later, run this again with REPLACE=1."

if [ -z "$API_KEY" ]; then
  read -r -p "$EXCHANGE_NAME API key: " API_KEY || true
fi
if [ -n "$API_SECRET_ENV" ]; then
  API_SECRET="${!API_SECRET_ENV:-}"
else
  # `|| true`: without a terminal, read fails at end of input and set -e would exit
  # before the check below could say what is missing.
  read -r -s -p "$EXCHANGE_NAME API secret: " API_SECRET || true
  echo
fi
if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
  echo "the API key and secret must both be set" >&2
  exit 1
fi

API_PASSPHRASE=""
case "$CONNECTOR" in
  okx | okx_*)
    if [ -n "$API_PASSPHRASE_ENV" ]; then
      API_PASSPHRASE="${!API_PASSPHRASE_ENV:-}"
    else
      read -r -s -p "OKX API passphrase: " API_PASSPHRASE || true
      echo
    fi
    if [ -z "$API_PASSPHRASE" ]; then
      echo "an OKX key needs its passphrase as well" >&2
      exit 1
    fi
    ;;
esac

if [ -n "$REPLACE" ] && [ -e "$SEALED" ]; then
  rm -f "$SEALED"
fi

export CUSTOS_API_KEY="$API_KEY" CUSTOS_API_SECRET="$API_SECRET" CUSTOS_API_PASSPHRASE="$API_PASSPHRASE"
docker run --rm \
  -v "$ARX_ROOT:/home/custos/.arx" \
  -e CUSTOS_API_KEY -e CUSTOS_API_SECRET -e CUSTOS_API_PASSPHRASE \
  --entrypoint /bin/sh "$IMAGE" \
  -c 'if [ -n "$CUSTOS_API_PASSPHRASE" ]; then set -- "$@" --api-passphrase-env CUSTOS_API_PASSPHRASE; fi
      tenant="$1" key="$2" recipient="$3" digest="$4"; shift 4
      exec arx-runner vault put --tenant-id "$tenant" --key-id "$key" \
        --api-key "$CUSTOS_API_KEY" --api-secret-env CUSTOS_API_SECRET \
        --age-recipient "$recipient" --scope-digest "$digest" \
        --permission-scope trade_no_withdraw --vault-dir /home/custos/.arx/vault "$@"' \
  vault-put "$TENANT_ID" "$CREDENTIAL_ID" "$RECIPIENT" "$SCOPE_DIGEST"

echo "[runner] sealed $CREDENTIAL_ID into $ARX_ROOT/vault/"
