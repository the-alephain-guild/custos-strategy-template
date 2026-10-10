#!/usr/bin/env python3
"""Seal a strategy's exchange key into the local vault, for one mode.

Each mode has a key of its own, under run.yaml's credential_id with the mode
appended, so sealing one never replaces another. Local runs are sandbox or
testnet, never live, so a live account's key is never asked for:

- sandbox fills orders on this machine and never sends a key anywhere, so a
  placeholder is sealed without asking;
- testnet trades on the exchange's own test environment, whose keys are separate
  from a live account's; this names that environment for the exchange
  trading.connector in config.yaml names, and asks for the key.

Without --mode, a terminal is asked which mode the key is for; anything else gets
sandbox. The secret is typed at a hidden prompt or read from the environment
variable --api-secret-env names, and an OKX passphrase the same way. Neither is
passed on a command line or in the container's environment: both reach the
container on its standard input and are encrypted there with this machine's age
key. The runner never overwrites a sealed key; --replace removes the one for this
mode, but only once the new key has been read.

With --venue, the key is for one of the strategy's venue profiles: the exchange
is the profile's, and the key is sealed under the profile's own credential, so a
strategy that trades on two exchanges has a key for each.

Usage:
    python3 tools/runner/vault.py --strategy trend/my_idea --arx-root DIR \\
        --image IMAGE --tenant-id ID [--mode sandbox|testnet] [--replace] [--toolchain dev] \\
        [--api-secret-env VAR] [--api-passphrase-env VAR] [--venue ID]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui, venues  # noqa: E402
from tools.runner import spec  # noqa: E402

PLACEHOLDER = "sandbox-placeholder"
HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Exchange:
    name: str
    testnet: str
    passphrase: bool = False
    # What the exchange calls the two halves of a key, as they are asked for.
    key_label: str = "API key"
    secret_label: str = "API secret"
    key_note: str = ""


# SoDEX keeps a key's name where other exchanges keep the key string, and signs
# with the API key's own private key, which is not the wallet's.
SODEX_KEY = dict(
    key_label="API Key Name",
    secret_label="API key's private key (not the wallet's)",
    key_note="The key is the API key's name, and the secret is that key's private "
    "key, never the wallet's.",
)


EXCHANGES = {
    "binance": Exchange("Binance spot", "the Binance spot testnet (testnet.binance.vision)"),
    "binance_perpetual": Exchange(
        "Binance USDⓈ-M perpetual",
        "the Binance USDⓈ-M futures testnet (testnet.binancefuture.com)",
    ),
    "okx": Exchange(
        "OKX spot", "OKX demo trading (create the key with demo trading on)", passphrase=True
    ),
    "okx_perpetual": Exchange(
        "OKX perpetual swap",
        "OKX demo trading (create the key with demo trading on)",
        passphrase=True,
    ),
    "sodex": Exchange("SoDEX spot", "the SoDEX testnet", **SODEX_KEY),
    "sodex_perpetual": Exchange("SoDEX perpetual", "the SoDEX testnet", **SODEX_KEY),
}


class VaultError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credential:
    api_key: str
    api_secret: str
    api_passphrase: str = ""


def exchange_for(connector: str) -> Exchange:
    return EXCHANGES.get(connector) or Exchange(connector, "the exchange's test environment")


def pick_mode(exchange: Exchange) -> str:
    return ui.choose(
        "Which runs is this key for?",
        [
            ("sandbox", "sandbox: orders are filled on this machine; nothing to enter"),
            ("testnet", f"testnet: a key from the {exchange.name.split()[0]} test environment"),
        ],
        default="sandbox",
    )


def read_credential(mode: str, exchange: Exchange, secret_env: str | None,
                    passphrase_env: str | None) -> Credential:  # fmt: skip
    if mode == "sandbox":
        return Credential(
            os.environ.get("API_KEY") or PLACEHOLDER,
            (os.environ.get(secret_env) if secret_env else None) or PLACEHOLDER,
            PLACEHOLDER if exchange.passphrase else "",
        )
    api_key = os.environ.get("API_KEY") or ui.ask(f"{exchange.name} testnet {exchange.key_label}")
    if secret_env:
        api_secret = os.environ.get(secret_env, "")
    else:
        api_secret = ui.secret(f"{exchange.name} testnet {exchange.secret_label}")
    if not api_key or not api_secret:
        raise VaultError("the API key and secret must both be set")
    passphrase = ""
    if exchange.passphrase:
        if passphrase_env:
            passphrase = os.environ.get(passphrase_env, "")
        else:
            passphrase = ui.secret(f"{exchange.name} testnet API passphrase")
        if not passphrase:
            raise VaultError(f"an {exchange.name.split()[0]} key needs its passphrase as well")
    return Credential(api_key, api_secret, passphrase)


def explain(mode: str, strategy: str, credential_id: str, exchange: Exchange) -> None:
    if mode == "sandbox":
        ui.panel(
            f"Sandbox key for {strategy}",
            [
                f"Credential: {credential_id}",
                f"Sandbox runs on {exchange.name} market data and fills orders on this machine.",
                "No key reaches the exchange, so a placeholder is sealed and nothing is asked.",
                f"Testnet keeps a key of its own: make setup-key STRATEGY={strategy} MODE=testnet",
            ],
        )
    else:
        ui.panel(
            f"Testnet key for {strategy}",
            [
                f"Credential: {credential_id}",
                f"Exchange: {exchange.name}, from trading.connector in config.yaml",
                f"Use a key from {exchange.testnet}.",
                "Not your live account's key: local runs never trade live, and the testnet "
                "does not accept live keys.",
                *([exchange.key_note] if exchange.key_note else []),
                "Give it trading permission only, never withdrawal.",
            ],
            kind="warn",
        )


def scope_digest(tenant_id: str, credential_id: str) -> str:
    # The vault requires a scope digest. A signed deployment would supply the one it
    # binds this credential to; the local lane has no signed deployment and its
    # reader never compares the field, so a digest is derived from what the
    # credential is for. The only code that reads the field refuses on mismatch, so
    # a derived value can make a signed deployment decline this credential but
    # never accept one it should not.
    text = f"custos-offline-lane/{tenant_id}/{credential_id}/trade_no_withdraw"
    return hashlib.sha256(text.encode()).hexdigest()


# The script the runner's container runs to seal a key. Its standard input carries
# the secret on the first line and, for an exchange that has one, the passphrase on
# the second; neither is in the container's configuration, where docker inspect
# would show a value passed with docker run -e. read and printf are built into the
# image's /bin/sh, so the values are never any process's arguments: the secret goes
# to arx-runner's own stdin, and the passphrase only into the environment of that
# one arx-runner process, which is the only way the runner takes it.
SEAL_SCRIPT = """\
IFS= read -r secret || { echo "no secret on standard input" >&2; exit 1; }
IFS= read -r passphrase || passphrase=
tenant="$1" key="$2" recipient="$3" digest="$4"; shift 4
set -- arx-runner vault put --tenant-id "$tenant" --key-id "$key" \\
  --api-key "$CUSTOS_API_KEY" --api-secret-stdin \\
  --age-recipient "$recipient" --scope-digest "$digest" \\
  --permission-scope trade_no_withdraw --vault-dir /home/custos/.arx/vault
if [ -n "$passphrase" ]; then set -- "$@" --api-passphrase-env CUSTOS_API_PASSPHRASE; fi
printf '%s\\n' "$secret" | CUSTOS_API_PASSPHRASE="$passphrase" "$@"
"""


def seal_input(credential: Credential) -> str:
    """What the sealing container reads on its standard input."""
    lines = [credential.api_secret]
    if credential.api_passphrase:
        lines.append(credential.api_passphrase)
    if any("\n" in line or "\r" in line for line in lines):
        raise VaultError(
            "the API secret and passphrase must each be one line, without a line break"
        )
    return "".join(f"{line}\n" for line in lines)


def seal(arx_root: Path, image: str, tenant_id: str, credential_id: str,
         credential: Credential) -> None:  # fmt: skip
    sent = seal_input(credential)
    recipient = subprocess.run(
        ["bash", str(HERE / "age_key.sh"), str(arx_root)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    env = dict(os.environ, CUSTOS_API_KEY=credential.api_key)
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "-v", f"{arx_root}:/home/custos/.arx",
         "-e", "CUSTOS_API_KEY", "--entrypoint", "/bin/sh", image, "-c", SEAL_SCRIPT,
         "vault-put", tenant_id, credential_id, recipient,
         scope_digest(tenant_id, credential_id)],
        env=env, input=sent, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise VaultError(f"the runner could not seal the key: {result.stderr.strip()}")


def run(args: argparse.Namespace) -> None:
    from custos_toolkit.config import load_config

    directory = spec.strategy_dir(args.strategy)
    venue_id = args.venue or None
    settings = spec.run_settings(directory)
    if venue_id:
        try:
            connector = venues.connector_of(venues.render_config(directory, venue_id))
        except venues.VenueError as failure:
            raise VaultError(str(failure)) from failure
        settings = venues.run_slice(settings, directory.name, venue_id, connector)
    else:
        connector = load_config(directory / "config.yaml").trading.get("connector") or ""
    exchange = exchange_for(connector)
    mode = args.mode or pick_mode(exchange)
    if mode not in spec.MODES:
        raise VaultError(
            "local runs are MODE=sandbox or MODE=testnet; live keys are not sealed here"
        )

    credential_id = spec.credential_for(settings, mode)
    sealed = args.arx_root / "vault" / f"{credential_id}.enc"
    replace = args.replace
    if sealed.exists() and not replace:
        if not ui.interactive():
            raise VaultError(
                f"a {mode} key is already sealed for {args.strategy} ({credential_id}); "
                f"to seal another {mode} key in its place, add REPLACE=1"
            )
        if not ui.confirm(f"A {mode} key is already sealed for {args.strategy}. Replace it?"):
            raise VaultError(f"kept the {mode} key already sealed as {credential_id}")
        replace = True

    explain(mode, args.strategy, credential_id, exchange)
    credential = read_credential(mode, exchange, args.api_secret_env, args.api_passphrase_env)
    # The old key stays until the new one is sealed: it steps aside under another
    # name, comes back if sealing fails, and goes only once the runner has written
    # the replacement.
    set_aside = sealed.with_name(sealed.name + ".replaced") if replace and sealed.exists() else None
    if set_aside is not None:
        sealed.replace(set_aside)
    try:
        seal(args.arx_root, args.image, args.tenant_id, credential_id, credential)
    except BaseException:
        if set_aside is not None and not sealed.exists():
            set_aside.replace(sealed)
        raise
    if set_aside is not None:
        set_aside.unlink()
    ui.ok(f"sealed the {mode} key {credential_id}")
    suffix = " TOOLCHAIN=dev" if args.toolchain == "dev" else ""
    profile = f" VENUE={venue_id}" if venue_id else ""
    ui.next_steps(
        [
            (
                f"make start STRATEGY={args.strategy}{profile} MODE={mode}{suffix}",
                f"run it with this {mode} key",
            )
        ]
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--arx-root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--mode")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--toolchain", default="pinned")
    parser.add_argument("--api-secret-env")
    parser.add_argument("--api-passphrase-env")
    parser.add_argument("--venue")
    args = parser.parse_args(argv[1:])
    try:
        run(args)
    except ui.Cancelled:
        ui.error("cancelled; nothing was sealed or removed")
        return 1
    except (VaultError, spec.SpecError) as failure:
        ui.error(str(failure))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
