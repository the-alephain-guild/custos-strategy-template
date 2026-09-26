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
# Each mode has its own key, under its own credential id, so sealing one never
# replaces another. --mode says which: sandbox (the default) seals a placeholder
# without asking, since sandbox never sends a key anywhere; testnet asks for a key
# from the exchange's test environment, named after trading.connector in
# config.yaml. Live keys are never sealed here.
#
# Usage: vault.sh --arx-root DIR --image IMAGE --tenant-id ID --credential-id ID
#                 [--strategy PATH] [--connector NAME] [--api-key KEY]
#                 [--api-secret-env VAR] [--api-passphrase-env VAR] [--replace]
#                 [--mode sandbox|testnet]
#
# The runner never overwrites a sealed key. --replace removes the one sealed for
# this mode, but only once the new key has been read, so a failed prompt leaves
# the old in place.
set -euo pipefail

ARX_ROOT="" IMAGE="" TENANT_ID="" CREDENTIAL_ID="" CONNECTOR="" STRATEGY="" REPLACE=""
MODE="sandbox"
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
    --mode) MODE="$2"; shift 2 ;;
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

# Local runs are sandbox or testnet, never live, so a live key is never asked for.
# Sandbox fills orders on this machine and never sends the key anywhere, so it gets
# a placeholder; testnet trades on the exchange's own test environment, whose keys
# are separate from the live account's.
case "$CONNECTOR" in
  binance)
    EXCHANGE="Binance spot"
    TESTNET="the Binance spot testnet (testnet.binance.vision)" ;;
  binance_perpetual)
    EXCHANGE="Binance USDⓈ-M perpetual"
    TESTNET="the Binance USDⓈ-M futures testnet (testnet.binancefuture.com)" ;;
  okx | okx_perpetual)
    EXCHANGE="OKX ${CONNECTOR/okx_perpetual/perpetual swap}"
    EXCHANGE="${EXCHANGE/OKX okx/OKX spot}"
    TESTNET="OKX demo trading (create the key with demo trading switched on)" ;;
  sodex | sodex_perpetual)
    EXCHANGE="SoDEX ${CONNECTOR/sodex_perpetual/perpetual}"
    EXCHANGE="${EXCHANGE/SoDEX sodex/SoDEX spot}"
    TESTNET="the SoDEX testnet" ;;
  *) EXCHANGE="${CONNECTOR:-the exchange}"; TESTNET="the exchange's test environment" ;;
esac

case "$MODE" in
  sandbox | testnet) ;;
  *) echo "local runs are MODE=sandbox or MODE=testnet; a live key is never sealed here" >&2
     exit 2 ;;
esac

SEALED="$ARX_ROOT/vault/$CREDENTIAL_ID.enc"
if [ -e "$SEALED" ] && [ -z "$REPLACE" ]; then
  echo "A $MODE key is already sealed for ${STRATEGY:-this strategy} (credential $CREDENTIAL_ID)." >&2
  echo "To seal another $MODE key in its place, add REPLACE=1." >&2
  exit 1
fi

if [ "$MODE" = sandbox ]; then
  echo "Sealing a placeholder key for ${STRATEGY:-this strategy} (credential $CREDENTIAL_ID)."
  echo "  MODE=sandbox runs on $EXCHANGE market data and fills orders on this"
  echo "  machine. No key reaches the exchange, so none is asked for."
  echo "  Testnet keeps a key of its own: make runner-vault STRATEGY=${STRATEGY:-...} MODE=testnet"
  API_KEY="${API_KEY:-sandbox-placeholder}"
  if [ -n "$API_SECRET_ENV" ]; then
    API_SECRET="${!API_SECRET_ENV:-sandbox-placeholder}"
  else
    API_SECRET="sandbox-placeholder"
  fi
  case "$CONNECTOR" in
    okx | okx_*) export CUSTOS_SANDBOX_PASSPHRASE="sandbox-placeholder" ;;
  esac
else
  echo "Sealing a TESTNET key for ${STRATEGY:-this strategy} (credential $CREDENTIAL_ID)."
  echo "  Exchange: $EXCHANGE, from trading.connector in config.yaml."
  echo "  Use a key from $TESTNET."
  echo "  Not your live account's key: local runs never trade live, and the testnet"
  echo "  does not accept live keys."
  echo "  Give it trading permission only, never withdrawal."
  if [ -z "$API_KEY" ]; then
    read -r -p "$EXCHANGE testnet API key: " API_KEY || true
  fi
  if [ -n "$API_SECRET_ENV" ]; then
    API_SECRET="${!API_SECRET_ENV:-}"
  else
    # `|| true`: without a terminal, read fails at end of input and set -e would
    # exit before the check below could say what is missing.
    read -r -s -p "$EXCHANGE testnet API secret: " API_SECRET || true
    echo
  fi
  if [ -z "$API_KEY" ] || [ -z "$API_SECRET" ]; then
    echo "the API key and secret must both be set" >&2
    exit 1
  fi
fi

API_PASSPHRASE=""
case "$CONNECTOR" in
  okx | okx_*)
    if [ -n "${CUSTOS_SANDBOX_PASSPHRASE:-}" ]; then
      API_PASSPHRASE="$CUSTOS_SANDBOX_PASSPHRASE"
    elif [ -n "$API_PASSPHRASE_ENV" ]; then
      API_PASSPHRASE="${!API_PASSPHRASE_ENV:-}"
    else
      read -r -s -p "OKX testnet API passphrase: " API_PASSPHRASE || true
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

echo "[runner] sealed the $MODE key $CREDENTIAL_ID into $ARX_ROOT/vault/"
