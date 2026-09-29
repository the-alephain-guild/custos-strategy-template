#!/usr/bin/env python3
"""Give a strategy a venue profile: the same strategy on another exchange.

`make add-venue STRATEGY=trend/my_idea CONNECTOR=sodex PAIR=vBTC_vUSDC` writes
venues/<id>.yaml, with the connector, pairs and leverage the runner requires,
and adds the profile's block to run.yaml: its own credential, a simulated account
in the exchange's settlement currency, and the account settings the exchange
wants. The id is the connector's name unless VENUE= names another, so one
connector can carry two profiles with different pairs.

The pair is held to the rules the strategy's first pair was chosen under, and
every bar the strategy uses (its bar_type and any backtesting timeframes) must
be one the exchange serves; a profile that could not run is not written.

Usage:
    python3 scripts/add-venue.py <category>/<name> --connector sodex --pair vBTC_vUSDC \\
        [--pair ...|--pair A,B] [--venue <id>] [--credential-id <name>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ui, venues  # noqa: E402
from tools.data import sources  # noqa: E402
from tools.data.common import DataError, interval_for, interval_ms  # noqa: E402
from tools.runner import spec  # noqa: E402

TAG = "add-venue"

# What run.yaml's venue block carries for each exchange, beyond the settlement
# currency SoDEX needs in every mode. Lines starting with "#" stay comments in
# the file, for the person to fill in.
ACCOUNT_SETTINGS: dict[str, list[str]] = {
    "okx": ["region: global", "margin_mode: cross"],
    "sodex": [
        "settlement_currency: vUSDC",
        '# wallet_address: "0x..."     # testnet: the wallet the API key was registered for',
        '# sodex_account_id: "12345"   # testnet: that wallet\'s numeric account id on this engine',
    ],
}


class AddVenueError(ValueError):
    pass


def check_bars(config: dict, connector: str) -> None:
    """Every bar the strategy uses must be one the exchange serves."""
    platforms = config.get("platforms") or {}
    nautilus = platforms.get("nautilus") if isinstance(platforms, dict) else None
    bar_type = (
        venues._value((nautilus or {}).get("bar_type")) if isinstance(nautilus, dict) else None
    )
    wanted: list[str] = []
    if bar_type:
        wanted.append(interval_for(str(bar_type)))
    backtesting = config.get("backtesting") or {}
    extra = (
        venues._value(backtesting.get("additional_timeframes"))
        if isinstance(backtesting, dict)
        else None
    )
    for interval in extra or []:
        wanted.append(str(interval))
    served = sources.intervals_for(connector)
    for interval in wanted:
        if interval not in served:
            listed = ", ".join(sorted(served, key=interval_ms))
            raise AddVenueError(
                f"{sources.source_for(connector).NAME} has no {interval} bars for {connector}; "
                f"it serves {listed}. The strategy decides on {interval} bars, so it cannot "
                f"run there as it is"
            )


def profile_text(name: str, venue_id: str, connector: str, pairs: list[str]) -> str:
    listed = ", ".join(f'"{pair}"' for pair in pairs)
    return (
        f"# Venue profile {venue_id!r}: {name} on {connector}. Run it with\n"
        f"# `make start STRATEGY=<category>/{name} VENUE={venue_id}`; backtest, status, logs\n"
        f"# and stop take the same VENUE=. A profile may set only trading, platforms, warmup\n"
        f"# and backtesting: the parameters stay in config.yaml, the same on every exchange.\n"
        f"trading:\n"
        f"  connector:\n"
        f'    value: "{connector}"\n'
        f"  pairs:\n"
        f"    value: [{listed}]\n"
        f"  leverage:\n"
        f"    value: 1\n"
        f"  # Fees differ by exchange and account tier; set the ones your account pays.\n"
        f"  # fees:\n"
        f"  #   maker:\n"
        f"  #     value: 0.0002\n"
        f"  #   taker:\n"
        f"  #     value: 0.0005\n"
    )


def run_block(venue_id: str, connector: str, credential_id: str, settlement: str) -> list[str]:
    lines = [
        f"  {venue_id}:",
        f"    credential_id: {credential_id}",
        "    sandbox:",
        f'      starting_balances: ["10000 {settlement}"]',
    ]
    settings = ACCOUNT_SETTINGS.get(connector.split("_")[0])
    if settings:
        lines.append("    venue:")
        lines.extend(f"      {line}" for line in settings)
    return lines


def add_to_run_yaml(path: Path, block: list[str]) -> str:
    """The profile's block under `venues:`, appended without disturbing what is there.

    A first profile adds the `venues:` mapping at the end of the file. A later one
    goes at the end of that mapping, which YAML keeps in one place: after its last
    indented line and before the next top-level key, or at the end of the file.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("venues:")), None)
    if start is None:
        header = [
            "",
            "# One block per venue profile (venues/<id>.yaml), with the same keys as above.",
            "# The simulated account and the ceilings fall back to the ones above when left",
            "# out; the exchange account settings (venue) never do. Each profile seals its",
            "# own key: `make setup-key STRATEGY=... VENUE=<id> MODE=testnet`.",
            "venues:",
        ]
        return "\n".join([*lines, *header, *block]) + "\n"
    end = start + 1
    while end < len(lines) and (not lines[end] or lines[end][0] in " #"):
        end += 1
    # Blank or comment lines right before the next key belong to that key.
    while end > start + 1 and (not lines[end - 1] or lines[end - 1].startswith("#")):
        end -= 1
    return "\n".join([*lines[:end], *block, *lines[end:]]) + "\n"


def add_venue(
    directory: Path, *, connector: str, pairs: list[str], venue_id: str, credential_id: str | None
) -> tuple[Path, str]:
    """Write the profile and its run.yaml block, or refuse and write nothing."""
    venues.check_venue_id(venue_id)
    if connector not in sources.SOURCES:
        raise AddVenueError(
            f"no exchange named {connector!r}; use one of {', '.join(sorted(sources.SOURCES))}"
        )
    profile_path = directory / venues.VENUES_DIR / f"{venue_id}.yaml"
    if profile_path.exists():
        raise AddVenueError(
            f"{directory.name} already has a venue profile {venue_id!r} ({profile_path}); "
            "name another with VENUE=<id>"
        )
    for pair in pairs:
        sources.validate_pair(connector, pair)
    config = venues._yaml(directory / "config.yaml")
    check_bars(config, connector)
    settlement = sources.settlement_for(connector, pairs[0])
    credential = credential_id or venues.default_credential_id(directory.name, connector, venue_id)

    run_path = directory / "run.yaml"
    settings = spec.run_settings(directory)
    if venue_id in (settings.get("venues") or {}):
        raise AddVenueError(f"{run_path} already has a venues.{venue_id} block")
    proposed = dict(settings)
    proposed["venues"] = {
        **(settings.get("venues") or {}),
        venue_id: {"credential_id": credential},
    }
    venues.check_run_venues(proposed, run_path)
    text = add_to_run_yaml(run_path, run_block(venue_id, connector, credential, settlement))
    # Refuse before anything is written when what would be written does not parse.
    venues.check_run_venues(yaml.safe_load(text) or {}, run_path)

    profile_path.parent.mkdir(exist_ok=True)
    profile_path.write_text(
        profile_text(directory.name, venue_id, connector, pairs), encoding="utf-8"
    )
    run_path.write_text(text, encoding="utf-8")
    return profile_path, credential


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", help="strategy directory, e.g. trend/my_idea")
    parser.add_argument("--connector", required=True)
    parser.add_argument(
        "--pair", action="append", required=True, help="one pair, or several with commas"
    )
    parser.add_argument("--venue", help="the profile's id; the connector's name unless given")
    parser.add_argument("--credential-id", help="the vault name for its keys; derived unless given")
    args = parser.parse_args(argv[1:])
    pairs = [pair.strip() for item in args.pair for pair in item.split(",") if pair.strip()]
    venue_id = args.venue or args.connector
    try:
        directory = spec.strategy_dir(args.strategy)
        if not pairs:
            raise AddVenueError("name at least one pair with PAIR=")
        profile_path, credential = add_venue(
            directory,
            connector=args.connector,
            pairs=pairs,
            venue_id=venue_id,
            credential_id=args.credential_id,
        )
    except (AddVenueError, DataError, venues.VenueError, spec.SpecError) as failure:
        ui.error(str(failure), tag=TAG)
        return 1
    shown = profile_path.relative_to(ROOT) if profile_path.is_relative_to(ROOT) else profile_path
    ui.ok(f"wrote {shown} and its block in run.yaml (credential {credential})", tag=TAG)
    ui.next_steps(
        [
            (f"edit {shown}", "its fees, and anything else that differs on this exchange"),
            (
                f"make backtest STRATEGY={args.strategy} VENUE={venue_id} "
                "START=2025-01-01 END=2025-04-01",
                "backtest it on that exchange's data",
            ),
            (f"make start STRATEGY={args.strategy} VENUE={venue_id} MODE=sandbox", "run it there"),
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
