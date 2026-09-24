#!/usr/bin/env python3
"""Refuse upstream changes that reach into paths a fork owns.

OWNERSHIP.toml lists the paths that belong to whoever forked the template. A
push to upstream, or a pull request proposed to it, must not touch any of them.
Both sides of a rename count: moving a file into a user path is as much a
change there as editing one.

Usage:
    python3 scripts/check-ownership.py --range BASE..HEAD
    python3 scripts/check-ownership.py --self-test
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def user_prefixes(repo: Path) -> tuple[str, ...]:
    data = tomllib.loads((repo / "OWNERSHIP.toml").read_text(encoding="utf-8"))
    return tuple(data["user"])


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, head],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def check(repo: Path, base: str, head: str) -> list[str]:
    prefixes = user_prefixes(repo)
    return [p for p in changed_paths(repo, base, head) if p.startswith(prefixes)]


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for rel, body in files.items():
        path = repo / rel
        if body is None:
            subprocess.run(["git", "rm", "-q", rel], cwd=repo, check=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", rel], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message],
        cwd=repo,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def self_test() -> int:
    ownership = (Path(__file__).resolve().parents[1] / "OWNERSHIP.toml").read_text(encoding="utf-8")
    cases = {
        "upstream edits a strategy": ({"strategies/trend/a/config.yaml": "b"}, True),
        "pull request adds to the registry": ({"registry/strategies.toml": "b"}, True),
        "pull request moves a tool into strategies": (
            {"tools/move.py": None, "strategies/move.py": "m"},
            True,
        ),
        "pull request improves a tool": ({"tools/backtest.py": "b"}, False),
        "file merely named like a user path": ({"docs/strategies/guide.md": "g"}, False),
    }
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        base = _commit(
            repo,
            {
                "OWNERSHIP.toml": ownership,
                "strategies/trend/a/config.yaml": "a",
                "registry/strategies.toml": "a",
                "tools/backtest.py": "a",
                "tools/move.py": "m",
            },
            "seed",
        )
        for name, (files, should_refuse) in cases.items():
            subprocess.run(["git", "checkout", "-q", "-B", "case", base], cwd=repo, check=True)
            head = _commit(repo, files, name)
            refused = bool(check(repo, base, head))
            if refused != should_refuse:
                failures.append(f"{name}: refused={refused}, expected {should_refuse}")
    if failures:
        print("self-test FAILED:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    refused_count = sum(1 for _, expected in cases.values() if expected)
    print(f"self-test passed: {refused_count} refused, {len(cases) - refused_count} allowed")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--self-test"]:
        return self_test()
    if len(argv) != 3 or argv[1] != "--range" or ".." not in argv[2]:
        print(__doc__)
        return 2
    base, head = argv[2].split("..", 1)
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    hits = check(repo, base, head)
    if hits:
        print("[check-ownership] BLOCKED: these paths belong to the forks, not to upstream:")
        for path in hits:
            print(f"  {path}")
        print("Keep strategies and registry entries in your fork; propose tool changes only.")
        return 1
    print("[check-ownership] ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
