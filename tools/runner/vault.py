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
sandbox. The secret is never passed on a command line: it is typed at a hidden
prompt or read from the environment variable --api-secret-env names, reaches the
container through the environment, and is encrypted there with this machine's
age key. The runner never overwrites a sealed key; --replace removes the one for
this mode, but only once the new key has been read.

Usage:
    python3 tools/runner/vault.py --strategy trend/my_idea --arx-root DIR \\
        --image IMAGE --tenant-id ID [--mode sandbox|testnet] [--replace] \\
        [--api-secret-env VAR] [--api-passphrase-env VAR]
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

from tools import ui  # noqa: E402
from tools.runner import spec  # noqa: E402

PLACEHOLDER = "sandbox-placeholder"
HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Exchange:
    name: str
    testnet: str
    passphrase: bool = False


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
    "sodex": Exchange("SoDEX spot", "the SoDEX testnet"),
    "sodex_perpetual": Exchange("SoDEX perpetual", "the SoDEX testnet"),
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
    api_key = os.environ.get("API_KEY") or ui.ask(f"{exchange.name} testnet API key")
    if secret_env:
        api_secret = os.environ.get(secret_env, "")
    else:
        api_secret = ui.secret(f"{exchange.name} testnet API secret")
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
                f"Testnet keeps a key of its own: make runner-vault STRATEGY={strategy} "
                "MODE=testnet",
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


def seal(arx_root: Path, image: str, tenant_id: str, credential_id: str,
         credential: Credential) -> None:  # fmt: skip
    recipient = subprocess.run(
        ["bash", str(HERE / "age_key.sh"), str(arx_root)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    env = dict(os.environ, CUSTOS_API_KEY=credential.api_key,
               CUSTOS_API_SECRET=credential.api_secret,
               CUSTOS_API_PASSPHRASE=credential.api_passphrase)  # fmt: skip
    script = (
        'if [ -n "$CUSTOS_API_PASSPHRASE" ]; then '
        'set -- "$@" --api-passphrase-env CUSTOS_API_PASSPHRASE; fi\n'
        'tenant="$1" key="$2" recipient="$3" digest="$4"; shift 4\n'
        'exec arx-runner vault put --tenant-id "$tenant" --key-id "$key" '
        '--api-key "$CUSTOS_API_KEY" --api-secret-env CUSTOS_API_SECRET '
        '--age-recipient "$recipient" --scope-digest "$digest" '
        '--permission-scope trade_no_withdraw --vault-dir /home/custos/.arx/vault "$@"'
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{arx_root}:/home/custos/.arx",
         "-e", "CUSTOS_API_KEY", "-e", "CUSTOS_API_SECRET", "-e", "CUSTOS_API_PASSPHRASE",
         "--entrypoint", "/bin/sh", image, "-c", script, "vault-put",
         tenant_id, credential_id, recipient, scope_digest(tenant_id, credential_id)],
        env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise VaultError(f"the runner could not seal the key: {result.stderr.strip()}")


def run(args: argparse.Namespace) -> None:
    from custos_toolkit.config import load_config

    directory = spec.strategy_dir(args.strategy)
    connector = load_config(directory / "config.yaml").trading.get("connector") or ""
    exchange = exchange_for(connector)
    mode = args.mode or pick_mode(exchange)
    if mode not in spec.MODES:
        raise VaultError(
            "local runs are MODE=sandbox or MODE=testnet; live keys are not sealed here"
        )

    credential_id = spec.credential_for(spec.run_settings(directory), mode)
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
    if replace and sealed.exists():
        sealed.unlink()
    seal(args.arx_root, args.image, args.tenant_id, credential_id, credential)
    ui.ok(f"sealed the {mode} key {credential_id}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--arx-root", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--mode")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--api-secret-env")
    parser.add_argument("--api-passphrase-env")
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
