#!/usr/bin/env python3
"""Refuse tracked files that belong to maintainers' private working notes.

This repository is public. Maintainers keep planning notes, review records and
assistant configuration next to the code, but those live outside the repository
and are linked in locally; they must never be tracked. A path is refused when
any of its segments is one of ``BANNED_SEGMENTS`` or its file name is one of
``BANNED_FILE_NAMES``, compared case-insensitively. Symbolic links are tracked
paths too, so a committed link is refused the same way. Paths OWNERSHIP.toml
gives to a fork are not checked: they hold the fork owner's own files.

Usage:
    python3 scripts/check-public-surface.py             # check the index
    python3 scripts/check-public-surface.py --self-test # prove the check bites
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

BANNED_SEGMENTS = frozenset({".forge", ".claude", ".planning", "plans"})
BANNED_FILE_NAMES = frozenset({"claude.md", "agents.md"})


def refused(path: str) -> bool:
    parts = [part.lower() for part in path.split("/")]
    return any(part in BANNED_SEGMENTS for part in parts) or parts[-1] in BANNED_FILE_NAMES


def tracked_paths(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def user_paths(repo: Path) -> tuple[str, ...]:
    """Paths OWNERSHIP.toml gives to the fork; what a user keeps there is theirs."""
    ownership = repo / "OWNERSHIP.toml"
    if not ownership.is_file():
        return ()
    return tuple(tomllib.loads(ownership.read_text(encoding="utf-8")).get("user", []))


def check(repo: Path) -> list[str]:
    skip = user_paths(repo)
    return [p for p in tracked_paths(repo) if refused(p) and not (skip and p.startswith(skip))]


REFUSED_CASES = (
    ".forge/plans/x.md",
    ".claude/rules/a.md",
    "CLAUDE.md",
    "docs/Claude.md",
    "sub/.forge/x.md",
    "docs/.Claude/settings.json",
    "AGENTS.md",
    ".planning/STATE.md",
    "tools/plans/p.md",
)
ALLOWED_CASES = (
    "README.md",
    "docs/claude-integration.md",
    "src/custos/forge.py",
    "docs/plan.md",
    "src/custos/planning/x.py",
    "strategies/trend/mine/CLAUDE.md",
)


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "OWNERSHIP.toml").write_text('user = ["strategies/"]\n', encoding="utf-8")
        for rel in REFUSED_CASES + ALLOWED_CASES:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n", encoding="utf-8")
        (repo / "sub" / "CLAUDE.md").symlink_to("../README.md")
        subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True)

        found = set(check(repo))
        expected = set(REFUSED_CASES) | {"sub/CLAUDE.md"}
        missed = expected - found
        false_hits = found - expected
        if missed or false_hits:
            print(f"self-test FAILED: missed={sorted(missed)} false_hits={sorted(false_hits)}")
            return 1
        print(f"self-test passed: {len(expected)} refused, {len(ALLOWED_CASES)} allowed")
        return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--self-test"]:
        return self_test()
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    hits = check(repo)
    if hits:
        print(
            "[check-public-surface] BLOCKED: these paths must not be tracked "
            "in a public repository:"
        )
        for path in hits:
            print(f"  {path}")
        print(
            "Keep them in the maintainers' private workspace and untrack them "
            "with `git rm --cached`."
        )
        return 1
    print("[check-public-surface] ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
