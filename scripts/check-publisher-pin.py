#!/usr/bin/env python3
"""Refuse a release workflow that calls another publisher than the lock pins.

toolchain.lock.toml names the publishing workflow and its tag under
[publisher]. A workflow's `uses:` must be written out literally, so
.github/workflows/release-strategy.yml repeats them; this check refuses the
two disagreeing, and refuses a workflow that calls the publisher twice or not
at all. The tag is the release's signing identity: a runner that trusts your
releases is told the same one.

Usage:
    python3 scripts/check-publisher-pin.py
    python3 scripts/check-publisher-pin.py --self-test
"""

from __future__ import annotations

import re
import sys
import tempfile
import tomllib
from pathlib import Path

LOCK = Path("toolchain.lock.toml")
WORKFLOW = Path(".github/workflows/release-strategy.yml")
USES = re.compile(r"^\s*uses:\s*(?P<value>\S+)\s*$", re.MULTILINE)


def problems(root: Path) -> list[str]:
    try:
        publisher = tomllib.loads((root / LOCK).read_text(encoding="utf-8"))["publisher"]
        pinned = f"{publisher['workflow']}@{publisher['ref']}"
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        return [f"{LOCK} has no [publisher] workflow and ref: {error}"]
    try:
        workflow = (root / WORKFLOW).read_text(encoding="utf-8")
    except OSError as error:
        return [f"{WORKFLOW} is missing: {error}"]
    calls = [match["value"] for match in USES.finditer(workflow)]
    if len(calls) != 1:
        return [f"{WORKFLOW} must call the publisher exactly once, found {len(calls)} uses:"]
    if calls[0] != pinned:
        return [f"{WORKFLOW} calls {calls[0]}, but {LOCK} pins {pinned}"]
    return []


def self_test() -> int:
    lock = '[publisher]\nworkflow = "owner/repo/.github/workflows/publish.yml"\nref = "v1.2.0"\n'
    cases = {
        "agreeing": ("owner/repo/.github/workflows/publish.yml@v1.2.0", True),
        "older tag": ("owner/repo/.github/workflows/publish.yml@v1.1.0", False),
        "another workflow": ("other/repo/.github/workflows/publish.yml@v1.2.0", False),
    }
    failures = []
    for name, (uses, expected) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / LOCK).write_text(lock, encoding="utf-8")
            (root / WORKFLOW).parent.mkdir(parents=True)
            (root / WORKFLOW).write_text(f"jobs:\n  release:\n    uses: {uses}\n", encoding="utf-8")
            if (not problems(root)) is not expected:
                failures.append(name)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / LOCK).write_text("schema_version = 1\n", encoding="utf-8")
        if not problems(root):
            failures.append("lock without [publisher]")
    if failures:
        print(f"self-test FAILED: {failures}")
        return 1
    print("self-test passed: the check refuses a publisher the lock does not pin")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--self-test"]:
        return self_test()
    if argv[1:]:
        print(__doc__)
        return 2
    found = problems(Path.cwd())
    if found:
        for problem in found:
            print(f"[check-publisher-pin] BLOCKED: {problem}")
        return 1
    print("[check-publisher-pin] ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
