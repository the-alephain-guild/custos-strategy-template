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
   not, how to start it.

It only reads: nothing is written and no container is started. The first check
must work before any environment exists, so everything up to it uses the standard
library alone; the Makefile runs this with the environment's own interpreter once
there is one.

Usage:
    python3 tools/next.py --repo-name officina [--strategy trend/my_idea] \\
        [--mode sandbox] [--toolchain pinned]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

DOCKER_TIMEOUT = 10


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


def project_name(repo_name: str, strategy_name: str, mode: str) -> str:
    """The compose project a run is started under; tools/runner/runner.mk names it the same."""
    return f"custos-{repo_name}-{strategy_name.replace('_', '-')}-{mode}"


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


def environment_ready(venv: Path) -> bool:
    python = venv / "bin" / "python"
    if not python.exists():
        return False
    result = subprocess.run(
        [str(python), "-c", "import custos_toolkit"], capture_output=True, check=False
    )
    return result.returncode == 0


def assess(
    root: Path,
    *,
    strategy: str | None,
    mode: str,
    toolchain: str,
    repo_name: str,
    environment_ready: Callable[[], bool],
    running_projects: Callable[[], set[str]],
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
        found.steps = [("make setup-runner", "create this machine's runner identity, once")]
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

    target = f"STRATEGY={chosen} MODE={mode}{suffix}"
    if mode == "testnet":
        from tools.runner import spec

        try:
            settings = spec.run_settings(root / "strategies" / chosen)
        except spec.SpecError as failure:
            raise NextError(str(failure)) from failure
        credential = spec.credential_for(settings, mode)
        if not (identity / "vault" / f"{credential}.enc").is_file():
            checks.append(Check("testnet key", False, f"{credential} is not sealed"))
            found.steps = [
                (f"make setup-key STRATEGY={chosen} MODE=testnet", "seal the testnet key")
            ]
            return found
        checks.append(Check("testnet key", True, credential))

    if project_name(repo_name, Path(chosen).name, mode) in running:
        checks.append(Check("running", True, f"in {mode} mode"))
        found.steps = [
            (f"make status {target}", "what it holds and has traded"),
            (f"make logs {target}", "follow its log"),
            (f"make stop {target}", "stop it"),
        ]
        return found
    checks.append(Check("running", False, docker_note or f"not in {mode} mode"))
    found.steps = [(f"make start {target}", "start it")]
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
        )
    except NextError as failure:
        ui.error(str(failure), tag="next")
        return 1
    show(found)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
