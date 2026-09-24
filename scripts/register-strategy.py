#!/usr/bin/env python3
"""Add a newly created strategy to registry/strategies.toml.

`make new-strategy` runs this after copier has written the strategy directory.
The entry is appended as text so the comments and layout already in the file
survive; a strategy registered twice is refused rather than duplicated.

Usage:
    python3 scripts/register-strategy.py <category> <name>
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "registry" / "strategies.toml"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    category, name = argv[1], argv[2]
    path = f"strategies/{category}/{name}"
    answers = ROOT / path / ".copier-answers.yml"
    if not answers.is_file():
        print(f"[register-strategy] {path} was not created by copier; nothing registered")
        return 1

    registry = tomllib.loads(REGISTRY.read_text(encoding="utf-8"))
    if any(entry.get("name") == name for entry in registry.get("strategies", [])):
        print(f"[register-strategy] {name} is already registered")
        return 1

    entry = (
        "\n[[strategies]]\n"
        f'name = "{name}"\n'
        f'category = "{category}"\n'
        f'path = "{path}"\n'
        'language = "python"\n'
    )
    with REGISTRY.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    print(f"[register-strategy] registered {name} at {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
