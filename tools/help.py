#!/usr/bin/env python3
"""Show one command's help in full: `make help CMD=start`.

Each command's help is written right above its target in the Makefiles, as a
block of `#>` lines:

    #> usage: make start STRATEGY=<category>/<name> [MODE=sandbox|testnet]
    #> var: STRATEGY | required | the strategy directory under strategies/
    #> example: make start STRATEGY=trend/supertrend MODE=testnet
    #> then: make status STRATEGY=trend/supertrend|what it holds and has traded
    #> note: testnet needs make setup-key first
    start: ...  ## Start a strategy on a local runner

The block has to sit directly above the target, so it cannot drift onto another
one; tests/test_help.py checks that every command has one. This uses the
standard library alone, so it works before any environment is installed.

Usage:
    python3 tools/help.py start
"""

from __future__ import annotations

import difflib
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

FILES = ("Makefile", "tools/runner/runner.mk")
# Commands removed in 0.5.0 and what replaced them, as docs/upgrading.md lists.
RENAMED = {
    "toolkit": "setup",
    "toolkit-dev": "setup-dev",
    "runner-init": "setup-runner",
    "runner-vault": "setup-key",
    "run": "start",
    "run-detached": "start",
    "run-report": "status",
    "run-status": "status",
    "run-logs": "logs",
    "run-stop": "stop",
    "run-smoke": "smoke",
}
_TARGET = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9-]*):.*##\s+(.*)$")
_BLOCK = re.compile(r"^#>\s?(usage|var|example|then|note):\s*(.*)$")


@dataclass
class Command:
    name: str
    summary: str
    usage: list[str] = field(default_factory=list)
    variables: list[tuple[str, str, str]] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    then: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse(texts: Iterable[str]) -> dict[str, Command]:
    commands: dict[str, Command] = {}
    for text in texts:
        pending: list[tuple[str, str]] = []
        for line in text.splitlines():
            block = _BLOCK.match(line)
            if block:
                pending.append((block.group(1), block.group(2).strip()))
                continue
            target = _TARGET.match(line)
            if target:
                command = Command(target.group(1), target.group(2).strip())
                for kind, value in pending:
                    _add(command, kind, value)
                commands[command.name] = command
            # Anything else between a block and a target detaches the block.
            pending = []
    return commands


def _add(command: Command, kind: str, value: str) -> None:
    if kind == "usage":
        command.usage.append(value)
    elif kind == "var":
        name, default, meaning = (part.strip() for part in (value.split("|", 2) + ["", ""])[:3])
        command.variables.append((name, default, meaning))
    elif kind == "example":
        command.examples.append(value)
    elif kind == "then":
        run, _, meaning = value.partition("|")
        command.then.append((run.strip(), meaning.strip()))
    elif kind == "note":
        command.notes.append(value)


def show(name: str, commands: dict[str, Command]) -> int:
    command = commands.get(name)
    if command is None:
        if name in RENAMED:
            ui.error(f"make {name} was renamed: use make {RENAMED[name]}", tag="help")
            ui.next_steps([(f"make help CMD={RENAMED[name]}", "its help")])
            return 1
        closest = difflib.get_close_matches(name, list(commands), n=1)
        ui.error(f"there is no command {name!r}", tag="help")
        if closest:
            ui.next_steps([(f"make help CMD={closest[0]}", "the closest command")])
        else:
            ui.next_steps([("make help", "every command")])
        return 1

    # A summary may end with an example, which the usage below says in full.
    ui.header(f"make {command.name}", [command.summary.split(": make ", 1)[0]])
    if command.usage:
        ui.space()
        ui.section("Usage", command.usage)
    if command.variables:
        ui.space()
        ui.grid(
            "Variables",
            ["Variable", "Default", "Meaning"],
            [
                [variable, ui.Cell(default, "warn" if default == "required" else "muted"), meaning]
                for variable, default, meaning in command.variables
            ],
        )
    if command.examples:
        ui.space()
        ui.section("Examples", command.examples)
    if command.notes:
        ui.space()
        ui.section("Notes", command.notes)
    if command.then:
        ui.space()
        ui.next_steps(command.then)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    return show(argv[1], parse((ROOT / name).read_text(encoding="utf-8") for name in FILES))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
