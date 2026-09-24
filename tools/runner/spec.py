#!/usr/bin/env python3
"""Render the DeploymentSpec that tells a local Custos runner what to run.

The spec says only how to run the strategy: the credential, exposure ceilings
and exchange account settings from run.yaml. What to trade -- connector, pairs,
leverage -- the runner reads from config.yaml under strategy_path, which is the
only place they are written; they are checked here so a missing one is caught
before the runner refuses it. The strategy itself is not copied
anywhere; the repository is mounted into the runner at CONTAINER_ROOT and the
spec points at the strategy's directory there, so an edit to the source is live
on the next start.

Usage:
    python3 tools/runner/spec.py render --strategy trend/my_idea --mode sandbox \
        --generation 1 --lifecycle-state running --output .runner/deployment.json
    python3 tools/runner/spec.py credential-id --strategy trend/my_idea
    python3 tools/runner/spec.py connector --strategy trend/my_idea
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_ROOT = PurePosixPath("/opt/repo")
MODES = ("sandbox", "testnet")
LIFECYCLE_STATES = ("running", "stopped")
RUN_FIELDS = {"credential_id", "sandbox", "risk_config", "venue"}


class SpecError(ValueError):
    pass


def strategy_dir(argument: str) -> Path:
    for candidate in (ROOT / "strategies" / argument, ROOT / argument):
        if (candidate / "config.yaml").is_file():
            return candidate.resolve()
    raise SpecError(f"no strategy with a config.yaml at {argument!r}")


def _yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SpecError(f"{path} must contain a YAML mapping")
    return data


def run_settings(directory: Path) -> dict:
    path = directory / "run.yaml"
    if not path.is_file():
        raise SpecError(f"{path} is missing; it names the credential this strategy trades with")
    settings = _yaml(path)
    unknown = set(settings) - RUN_FIELDS
    if unknown:
        raise SpecError(f"{path} has unknown keys: {', '.join(sorted(unknown))}")
    credential_id = settings.get("credential_id")
    if not isinstance(credential_id, str) or not credential_id:
        raise SpecError(f"{path}: credential_id must be a non-empty string")
    risk = settings.get("risk_config")
    if risk is not None and not isinstance(risk, dict):
        raise SpecError(f"{path}: risk_config must be a mapping of ceilings")
    venue = settings.get("venue")
    if venue is not None and not isinstance(venue, dict):
        raise SpecError(f"{path}: venue must be a mapping of exchange account settings")
    return settings


def build_spec(
    directory: Path, *, mode: str, generation: int, lifecycle_state: str
) -> dict[str, object]:
    if mode not in MODES:
        raise SpecError(f"mode must be one of {', '.join(MODES)}; live needs an enrolled runner")
    if lifecycle_state not in LIFECYCLE_STATES:
        raise SpecError(f"lifecycle state must be one of {', '.join(LIFECYCLE_STATES)}")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise SpecError("generation must be a positive integer")

    settings = run_settings(directory)
    relative = directory.relative_to(ROOT)

    spec: dict[str, object] = {
        "spec_id": f"{directory.name}-{mode}",
        "generation": generation,
        "lifecycle_state": lifecycle_state,
        "trading_mode": mode,
        "code_hash": None,
        "strategy_path": str(CONTAINER_ROOT.joinpath(*relative.parts)),
        "strategy_registry_name": directory.name,
        "provenance_ref": {"credential_id": settings["credential_id"]},
    }
    if mode == "sandbox":
        spec["sandbox"] = settings.get("sandbox") or {"starting_balances": ["10000 USDT"]}
    if settings.get("risk_config") is not None:
        spec["risk_config"] = settings["risk_config"]
    # The runner reads exchange account settings from here and refuses fields it
    # does not know for the connector, so they are passed through unchanged.
    if settings.get("venue"):
        spec["nautilus_config"] = {"venue": settings["venue"]}
    # Checked in the file itself: the toolkit fills these from its defaults when
    # loading, but the runner requires them written in config.yaml.
    written = _yaml(directory / "config.yaml").get("trading") or {}
    missing = [key for key in ("connector", "pairs", "leverage") if key not in written]
    if missing:
        names = ", ".join(f"trading.{key}" for key in missing)
        raise SpecError(f"{directory}/config.yaml must set {names}")
    return spec


def write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    render.add_argument("--strategy", required=True)
    render.add_argument("--mode", required=True, choices=MODES)
    render.add_argument("--generation", required=True, type=int)
    render.add_argument("--lifecycle-state", required=True, choices=LIFECYCLE_STATES)
    render.add_argument("--output", required=True, type=Path)
    credential = commands.add_parser("credential-id")
    credential.add_argument("--strategy", required=True)
    location = commands.add_parser("container-path")
    location.add_argument("--strategy", required=True)
    connector = commands.add_parser("connector")
    connector.add_argument("--strategy", required=True)
    args = parser.parse_args(argv[1:])

    try:
        directory = strategy_dir(args.strategy)
        if args.command == "credential-id":
            print(run_settings(directory)["credential_id"])
        elif args.command == "connector":
            from custos_toolkit.config import load_config

            print(load_config(directory / "config.yaml").trading.get("connector") or "")
        elif args.command == "container-path":
            print(CONTAINER_ROOT.joinpath(*directory.relative_to(ROOT).parts))
        else:
            spec = build_spec(
                directory,
                mode=args.mode,
                generation=args.generation,
                lifecycle_state=args.lifecycle_state,
            )
            write_atomic(args.output, spec)
            print(f"[runner] spec written to {args.output}")
    except SpecError as error:
        print(f"[runner] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
