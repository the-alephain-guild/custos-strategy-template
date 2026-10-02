#!/usr/bin/env python3
"""Say where this repository stands and the one command to run next.

Checks in order, and stops at the first that is not done yet:

1. the environment is installed (`make setup`, or `make setup-dev` on the dev
   toolchain);
2. there is a strategy (`make new-strategy`);
3. this machine has a runner identity (`make setup-runner`);
4. on testnet, the strategy's testnet key is sealed (`make setup-key`); sandbox
   seals its own placeholder;
5. whether the strategy is running: if so, how to look at it and stop it; if
   not, how to start it;
6. once a version of it is released (`make release`), how to take it to ARX:
   sign in first (`make arx-login`), then read the release back and check it
   (`make arx-evidence`); without a deploy.yaml, where to get one; with one,
   see the deployment spec it would be deployed with (`make deploy-preview`)
   and deploy it (`make deploy`). A deployment this machine made in the mode
   that still runs is stopped first (`make deploy-stop`): the released
   version's, instead of deploying it again, and an older version's, before
   the released one is deployed.

A strategy's venue profiles (venues/<id>.yaml) are listed; with --venue the
checks are for that profile's run, whose names carry the profile.

It only reads: nothing is written and no container is started. The first check
must work before any environment exists, so everything up to it uses the standard
library alone; the Makefile runs this with the environment's own interpreter once
there is one.

Usage:
    python3 tools/next.py --repo-name officina [--strategy trend/my_idea] \\
        [--venue sodex] [--mode sandbox] [--toolchain pinned]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

DOCKER_TIMEOUT = 10
RECEIPT_FILE = "strategy-release-publication-receipt-v1.json"
# Deployment receipt states in which nothing runs any more: stopped by
# `make deploy-stop` (tools/arx/deploy.py), or recorded by ARX but never started.
ENDED = frozenset({"stopped", "archived", "refused"})


class NextError(ValueError):
    pass


class DockerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    done: bool
    detail: str


@dataclass
class Assessment:
    checks: list[Check] = field(default_factory=list)
    steps: list[tuple[str, str]] = field(default_factory=list)
    # Every strategy and the modes it is running in, when none was chosen.
    strategies: dict[str, list[str]] = field(default_factory=dict)


def project_name(repo_name: str, strategy_name: str, mode: str, venue_id: str | None = None) -> str:
    """The compose project a run is started under; tools/runner/runner.mk names it the same."""
    tagged = f"{strategy_name}-{venue_id}" if venue_id else strategy_name
    return f"custos-{repo_name}-{tagged.replace('_', '-')}-{mode}"


def find_strategies(root: Path) -> list[str]:
    base = root / "strategies"
    return sorted(str(config.parent.relative_to(base)) for config in base.glob("*/*/config.yaml"))


def running_projects() -> set[str]:
    """The compose projects with a runner up on this machine."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "label=com.docker.compose.service=custos-runner",
             "--format", '{{.Label "com.docker.compose.project"}}'],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as failure:
        raise DockerUnavailable(str(failure)) from failure
    if result.returncode != 0:
        raise DockerUnavailable(result.stderr.strip() or "docker ps failed")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _version_key(version: str) -> tuple:
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in version.split("."))


def latest_release(root: Path, strategy: str) -> str | None:
    """The highest version of `strategy` whose receipt `make release` downloaded."""
    base = root / ".releases" / strategy
    versions = [d.name for d in base.glob("*") if d.is_dir() and any(d.rglob(RECEIPT_FILE))]
    return max(versions, key=_version_key) if versions else None


def deployments(root: Path, strategy: str, mode: str) -> list[tuple[str, str]]:
    """(version, state) of each receipt `make deploy` wrote for `strategy` in `mode`.

    A receipt that cannot be read, or has no state, counts as running: offering
    `make deploy-stop` for it is safer than offering a second deployment.
    """
    found = []
    for path in sorted((root / ".deployments" / strategy).glob(f"*/{mode}-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError, AttributeError):
            state = None
        found.append((path.parent.name, state if isinstance(state, str) else "unknown"))
    return found


def arx_signed_in() -> bool:
    """Whether this machine keeps ARX sessions; the file is not opened, only found."""
    from tools.arx.session import config_directory

    return (config_directory() / "hosts.json").is_file()


def environment_ready(venv: Path) -> bool:
    python = venv / "bin" / "python"
    if not python.exists():
        return False
    result = subprocess.run(
        [str(python), "-c", "import custos_toolkit"], capture_output=True, check=False
    )
    return result.returncode == 0


def arx_steps(
    root: Path, found: Assessment, chosen: str, mode: str, arx_signed_in: Callable[[], bool]
) -> None:
    """The steps that take the strategy's latest release through ARX, added to `found`.

    They need no local runner: a deployment runs on a runner enrolled with ARX,
    so none of this waits for `make setup-runner`, a sealed key or Docker.
    """
    checks = found.checks
    released = latest_release(root, chosen)
    if not released:
        return
    checks.append(Check("release", True, f"{released} released"))
    if not arx_signed_in():
        found.steps.append(
            ("make arx-login ARX_URL=https://arx.example.com", "sign in to ARX to deploy it")
        )
        return
    found.steps.append(
        (
            f"make arx-evidence STRATEGY={chosen} VERSION={released}",
            "read the release back from its package and check it",
        )
    )
    deployed = deployments(root, chosen, mode)
    running = sorted(
        {version for version, state in deployed if state not in ENDED}, key=_version_key
    )
    if released in running:
        checks.append(Check("deployment", True, f"{released} deployed in {mode}"))
        found.steps.append(
            (
                f"make deploy-stop STRATEGY={chosen} MODE={mode} VERSION={released}",
                "stop it before another release of it runs in this mode",
            )
        )
        return
    for version in running:
        checks.append(Check("deployment", True, f"{version} deployed in {mode}"))
        found.steps.append(
            (
                f"make deploy-stop STRATEGY={chosen} MODE={mode} VERSION={version}",
                f"stop it first: one release of a strategy runs at a time in {mode}",
            )
        )
    stopped = [version for version, state in deployed if version == released]
    if stopped:
        checks.append(Check("deployment", False, f"{released} stopped in {mode}"))
    if not (root / "strategies" / chosen / "deploy.yaml").is_file():
        found.steps.append(
            (
                f"cp examples/trend/sma_cross/deploy.yaml strategies/{chosen}/",
                "then fill it in: a deployment's settings live there (docs/deploying.md)",
            )
        )
        return
    again = "; change deploy.yaml first, the same settings do not start it again"
    # Whether the product exists is ARX's to say: make deploy finds it, or
    # creates it and stops until it is active (docs/deploying.md).
    found.steps += [
        (
            f"make deploy-preview STRATEGY={chosen} MODE={mode} VERSION={released} "
            "RUNNER=<runner id>",
            "see the deployment spec it would be deployed with, and its product",
        ),
        (
            f"make deploy STRATEGY={chosen} MODE={mode} VERSION={released} RUNNER=<runner id>",
            "deploy it through ARX, with one authenticator code; the first time in a mode it "
            "creates the strategy's product and stops until the product has capital and is "
            "active, and once its first instance runs a FINANCE holder allocates that capital "
            "to it in the ARX console" + (again if stopped else ""),
        ),
    ]


def assess(
    root: Path,
    *,
    strategy: str | None,
    mode: str,
    toolchain: str,
    repo_name: str,
    environment_ready: Callable[[], bool],
    running_projects: Callable[[], set[str]],
    venue: str | None = None,
    arx_signed_in: Callable[[], bool] = lambda: False,
) -> Assessment:
    suffix = " TOOLCHAIN=dev" if toolchain == "dev" else ""
    found = Assessment()
    checks = found.checks

    venv = ".venv-dev" if toolchain == "dev" else ".venv"
    if not environment_ready():
        checks.append(Check("environment", False, f"{venv} has no strategy toolkit"))
        found.steps = [
            ("make setup-dev", "build the dev toolchain")
            if toolchain == "dev"
            else ("make setup", "download the pinned toolkit and install the environment")
        ]
        return found
    checks.append(Check("environment", True, venv))

    strategies = find_strategies(root)
    if strategy is not None and strategy not in strategies:
        raise NextError(f"no strategy {strategy!r} under strategies/")
    if not strategies:
        checks.append(Check("strategy", False, "none under strategies/ yet"))
        found.steps = [
            ("make new-strategy NAME=my_idea", "create one from the template"),
            (
                "make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-02-01",
                "or try the example first",
            ),
        ]
        return found

    identity = root / ".runner" / ".arx"
    if (
        not (identity / "runner.toml").is_file()
        or not (identity / "vault" / "runner-machine.enc").is_file()
    ):
        checks.append(Check("runner identity", False, "not created on this machine"))
        found.steps = [
            ("make setup-runner", "create this machine's runner identity, to run it here")
        ]
        if strategy is not None or len(strategies) == 1:
            arx_steps(root, found, strategy or strategies[0], mode, arx_signed_in)
        return found
    checks.append(Check("runner identity", True, ".runner/.arx/runner.toml"))

    try:
        running = running_projects()
        docker_note = ""
    except DockerUnavailable as failure:
        running = set()
        docker_note = f"docker could not be asked: {failure}"

    if strategy is None and len(strategies) > 1:
        found.strategies = {
            name: [
                candidate
                for candidate in ("sandbox", "testnet")
                if project_name(repo_name, Path(name).name, candidate) in running
            ]
            for name in strategies
        }
        checks.append(Check("strategy", True, f"{len(strategies)} strategies"))
        found.steps = [(f"make next STRATEGY={strategies[0]}", "choose one to go on with")]
        return found
    chosen = strategy or strategies[0]
    checks.append(Check("strategy", True, chosen))

    from tools import venues

    directory = root / "strategies" / chosen
    profiles = venues.venue_ids(directory)
    if venue is not None and venue not in profiles:
        listed = ", ".join(profiles) or "none"
        raise NextError(f"{chosen} has no venue profile {venue!r}; it has: {listed}")
    if profiles:
        checks.append(Check("venue profiles", True, f"{', '.join(profiles)} (VENUE=<id> runs one)"))

    profile = f" VENUE={venue}" if venue else ""
    target = f"STRATEGY={chosen}{profile} MODE={mode}{suffix}"
    if mode == "testnet":
        from tools.runner import spec

        try:
            settings = spec.run_settings(directory)
            if venue:
                connector = venues.connector_of(venues.render_config(directory, venue))
                settings = venues.run_slice(settings, directory.name, venue, connector)
        except (spec.SpecError, venues.VenueError) as failure:
            raise NextError(str(failure)) from failure
        credential = spec.credential_for(settings, mode)
        if not (identity / "vault" / f"{credential}.enc").is_file():
            checks.append(Check("testnet key", False, f"{credential} is not sealed"))
            found.steps = [
                (f"make setup-key STRATEGY={chosen}{profile} MODE=testnet", "seal the testnet key")
            ]
            arx_steps(root, found, chosen, mode, arx_signed_in)
            return found
        checks.append(Check("testnet key", True, credential))

    if project_name(repo_name, Path(chosen).name, mode, venue) in running:
        checks.append(Check("running", True, f"in {mode} mode"))
        found.steps = [
            (f"make status {target}", "what it holds and has traded"),
            (f"make logs {target}", "follow its log"),
            (f"make stop {target}", "stop it"),
        ]
        arx_steps(root, found, chosen, mode, arx_signed_in)
        return found
    checks.append(Check("running", False, docker_note or f"not in {mode} mode"))
    found.steps = [(f"make start {target}", "start it")]
    arx_steps(root, found, chosen, mode, arx_signed_in)
    return found


def show(found: Assessment) -> None:
    rows = [
        [ui.Cell("✓", "up") if check.done else ui.Cell("✗", "down"), check.name, check.detail]
        for check in found.checks
    ]
    ui.grid("Where this repository stands", ["", "Step", "State"], rows)
    if found.strategies:
        ui.space()
        ui.grid(
            "Strategies",
            ["Strategy", "Running in"],
            [[name, ", ".join(modes) or "—"] for name, modes in found.strategies.items()],
        )
    ui.space()
    ui.next_steps(found.steps)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--strategy")
    parser.add_argument("--venue")
    parser.add_argument("--mode", default="sandbox")
    parser.add_argument("--toolchain", default="pinned")
    args = parser.parse_args(argv[1:])
    venv = ROOT / (".venv-dev" if args.toolchain == "dev" else ".venv")
    try:
        found = assess(
            ROOT,
            strategy=args.strategy,
            mode=args.mode,
            toolchain=args.toolchain,
            repo_name=args.repo_name,
            environment_ready=lambda: environment_ready(venv),
            running_projects=running_projects,
            venue=args.venue or None,
            arx_signed_in=arx_signed_in,
        )
    except NextError as failure:
        ui.error(str(failure), tag="next")
        return 1
    show(found)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
