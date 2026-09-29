#!/usr/bin/env python3
"""One strategy, several exchanges: venue profiles under a strategy's venues/.

A strategy's config.yaml is complete on its own and names the exchange it trades on
by default. A venue profile, venues/<id>.yaml, says what changes when the same
strategy trades somewhere else: the connector, the pairs, the fees, how bars are
served. It may set only the sections that depend on the exchange -- trading,
platforms, warmup and backtesting -- so a strategy's parameters are the same on
every exchange; a profile that touches anything else is refused.

The profile is laid over config.yaml, mappings key by key and lists and scalars
whole, the same way the strategy toolkit lays config.yaml over its own defaults.
The result is written out as a complete config.yaml of its own, next to a slice
of run.yaml and a link to the strategy's code, under
.runner/strategies/<id>/<name>/. The runner reads that directory exactly as it
reads a strategy's own: nothing in the runner knows profiles exist.

run.yaml carries one block per profile under `venues:`, with the same four keys
the file has at its top level (credential_id, sandbox, risk_config and venue).
The top level is the default profile's; a profile's block falls back to it for
the simulated account and the ceilings, never for the exchange account settings.

Usage:
    python3 tools/venues.py list --strategy trend/my_idea
    python3 tools/venues.py render --strategy trend/my_idea --venue sodex
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

VENUES_DIR = "venues"
RENDER_DIR = Path(".runner") / "strategies"
VENUE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
# The sections of config.yaml that depend on the exchange. Anything else is the
# strategy itself, and a strategy is the same on every exchange.
PROFILE_KEYS = frozenset({"trading", "platforms", "warmup", "backtesting"})
RUN_KEYS = frozenset({"credential_id", "sandbox", "risk_config", "venue"})
# The code the runner imports; the rest of a strategy's directory is for people.
LINKED = ("refinement",)


class VenueError(ValueError):
    pass


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as failure:
        raise VenueError(f"{path} is not valid YAML: {failure}") from failure
    if not isinstance(data, dict):
        raise VenueError(f"{path} must contain a YAML mapping")
    return data


def venue_ids(strategy_dir: Path) -> list[str]:
    """The profiles a strategy has, by file name."""
    folder = strategy_dir / VENUES_DIR
    if not folder.is_dir():
        return []
    return sorted(path.stem for path in folder.glob("*.yaml") if VENUE_ID.match(path.stem))


def check_venue_id(venue_id: str) -> str:
    if not VENUE_ID.match(venue_id):
        raise VenueError(
            f"venue id {venue_id!r} must be lowercase letters, digits and underscores, "
            "starting with a letter"
        )
    return venue_id


def load_profile(strategy_dir: Path, venue_id: str) -> dict:
    """The profile's overrides, refused when they reach outside the exchange's sections."""
    check_venue_id(venue_id)
    path = strategy_dir / VENUES_DIR / f"{venue_id}.yaml"
    if not path.is_file():
        available = venue_ids(strategy_dir)
        listed = ", ".join(available) if available else "none"
        raise VenueError(
            f"{strategy_dir.name} has no venue profile {venue_id!r}; it has: {listed} "
            f"(make add-venue adds one)"
        )
    profile = _yaml(path)
    outside = sorted(set(profile) - PROFILE_KEYS)
    if outside:
        allowed = ", ".join(sorted(PROFILE_KEYS))
        raise VenueError(
            f"{path} sets {', '.join(outside)}; a venue profile may set only {allowed}. "
            "Parameters and risk stay in config.yaml, the same on every exchange"
        )
    return profile


def merge(default: dict, profile: dict) -> dict:
    """Lay the profile over the default: mappings key by key, everything else whole."""
    merged = copy.deepcopy(default)
    for key, value in profile.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def render_config(strategy_dir: Path, venue_id: str) -> dict:
    """config.yaml with the profile laid over it, as the runner will read it."""
    return merge(_yaml(strategy_dir / "config.yaml"), load_profile(strategy_dir, venue_id))


def _value(node: object) -> object:
    """A config value, written plain or as {value: ..., type: ...}."""
    return node.get("value") if isinstance(node, dict) and "value" in node else node


def connector_of(config: dict) -> str:
    trading = config.get("trading") or {}
    connector = _value(trading.get("connector")) if isinstance(trading, dict) else None
    return str(connector or "")


def default_credential_id(strategy_name: str, connector: str, venue_id: str) -> str:
    """<exchange>-<strategy>-<profile>: one key per profile, as the vault keeps them."""
    exchange = connector.split("_")[0] or "exchange"
    return f"{exchange}-{strategy_name}-{venue_id}"


def check_run_venues(settings: dict, path: Path) -> dict[str, dict]:
    """The `venues:` mapping of run.yaml, or an empty one, refused when malformed."""
    venues = settings.get("venues")
    if venues is None:
        return {}
    if not isinstance(venues, dict):
        raise VenueError(f"{path}: venues must be a mapping of profile id to settings")
    seen: dict[str, str] = {}
    for venue_id, block in venues.items():
        if not isinstance(venue_id, str) or not VENUE_ID.match(venue_id):
            raise VenueError(
                f"{path}: venue id {venue_id!r} must be lowercase letters, digits and "
                "underscores, starting with a letter"
            )
        if not isinstance(block, dict):
            raise VenueError(f"{path}: venues.{venue_id} must be a mapping")
        unknown = set(block) - RUN_KEYS
        if unknown:
            raise VenueError(
                f"{path}: venues.{venue_id} has unknown keys: {', '.join(sorted(unknown))}"
            )
        credential = block.get("credential_id")
        if credential is not None and (not isinstance(credential, str) or not credential):
            raise VenueError(f"{path}: venues.{venue_id}.credential_id must be a non-empty string")
        if credential is not None:
            if credential == settings.get("credential_id"):
                raise VenueError(
                    f"{path}: venues.{venue_id} reuses the default profile's credential "
                    f"{credential!r}; each profile seals its own key"
                )
            if credential in seen:
                raise VenueError(
                    f"{path}: venues.{venue_id} and venues.{seen[credential]} share the "
                    f"credential {credential!r}; each profile seals its own key"
                )
            seen[credential] = venue_id
        for key in ("sandbox", "risk_config", "venue"):
            if key in block and not isinstance(block[key], dict):
                raise VenueError(f"{path}: venues.{venue_id}.{key} must be a mapping")
    return venues


def run_slice(settings: dict, strategy_name: str, venue_id: str, connector: str) -> dict:
    """run.yaml as the profile sees it: its own block, falling back to the top level.

    The simulated account and the ceilings are inherited when the block leaves them
    out. The exchange account settings (`venue`) are not: they belong to one
    exchange's account and would be wrong on another.
    """
    venues = check_run_venues(settings, Path("run.yaml"))
    block = venues.get(venue_id) or {}
    sliced: dict = {
        "credential_id": block.get("credential_id")
        or default_credential_id(strategy_name, connector, venue_id)
    }
    for key in ("sandbox", "risk_config"):
        value = block.get(key, settings.get(key))
        if value is not None:
            sliced[key] = copy.deepcopy(value)
    if block.get("venue") is not None:
        sliced["venue"] = copy.deepcopy(block["venue"])
    return sliced


def run_dir(root: Path, strategy_dir: Path, venue_id: str) -> Path:
    """Where the profile is rendered: .runner/strategies/<id>/<name>/.

    The strategy keeps its name as the last component, because the runner
    registers a strategy under its directory's name.
    """
    return root / RENDER_DIR / check_venue_id(venue_id) / strategy_dir.name


def _yaml_text(document: dict) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, default_flow_style=False)


def render_run_dir(root: Path, strategy_dir: Path, venue_id: str) -> Path:
    """Write the profile out as a directory the runner reads like a strategy's own.

    Rendered fresh on every call: what was there before is replaced, so an edit to
    the strategy or the profile is what the next start runs. The code is linked
    rather than copied, by a relative path, so the link resolves inside the
    container where the repository is mounted at another root.
    """
    config = render_config(strategy_dir, venue_id)
    settings = _yaml(strategy_dir / "run.yaml") if (strategy_dir / "run.yaml").is_file() else {}
    sliced = run_slice(settings, strategy_dir.name, venue_id, connector_of(config))
    target = run_dir(root, strategy_dir, venue_id)
    target.mkdir(parents=True, exist_ok=True)
    source = (
        f"# Rendered by tools/venues.py from {strategy_dir.name}/config.yaml and "
        f"venues/{venue_id}.yaml. Do not edit; it is replaced on every start.\n"
    )
    (target / "config.yaml").write_text(source + _yaml_text(config), encoding="utf-8")
    (target / "run.yaml").write_text(source + _yaml_text(sliced), encoding="utf-8")
    for name in LINKED:
        link = target / name
        if link.is_symlink() or link.exists():
            if link.is_dir() and not link.is_symlink():
                raise VenueError(f"{link} is a directory, not a link; remove it by hand")
            link.unlink()
        link.symlink_to(os.path.relpath(strategy_dir / name, target), target_is_directory=True)
    return target


def main(argv: list[str]) -> int:
    from tools.runner import spec

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--strategy", required=True)
    render = commands.add_parser("render")
    render.add_argument("--strategy", required=True)
    render.add_argument("--venue", required=True)
    args = parser.parse_args(argv[1:])
    try:
        directory = spec.strategy_dir(args.strategy)
        if args.command == "list":
            for venue_id in venue_ids(directory):
                print(venue_id)
        else:
            print(render_run_dir(ROOT, directory, args.venue).relative_to(ROOT))
    except (VenueError, spec.SpecError) as failure:
        ui.error(str(failure), tag="venues")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
