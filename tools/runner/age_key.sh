#!/usr/bin/env bash
# Create this machine's age key if it is absent, and print its recipient.
#
# One key seals the exchange credential and the runner's machine credential, and
# the runner uses it to open both. Replacing an existing key would orphan every
# credential already sealed to it, so an existing key is always reused.
#
# Usage: age_key.sh <arx-root>
set -euo pipefail

ARX_ROOT="${1:?usage: age_key.sh <arx-root>}"
AGE_KEY_FILE="$ARX_ROOT/age.key"

if ! command -v age-keygen >/dev/null 2>&1; then
  echo "age-keygen not found; install age (for example: brew install age)" >&2
  exit 1
fi

mkdir -p "$ARX_ROOT/vault"
chmod 700 "$ARX_ROOT" "$ARX_ROOT/vault"
if [ ! -f "$AGE_KEY_FILE" ]; then
  age-keygen -o "$AGE_KEY_FILE" 2>/dev/null
fi
chmod 600 "$AGE_KEY_FILE"

RECIPIENT="$(age-keygen -y "$AGE_KEY_FILE")"
case "$RECIPIENT" in
  age1*) printf '%s\n' "$RECIPIENT" ;;
  *) echo "could not derive an age recipient from $AGE_KEY_FILE" >&2; exit 1 ;;
esac
