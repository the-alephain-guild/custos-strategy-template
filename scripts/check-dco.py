#!/usr/bin/env python3
"""Refuse commits without a Developer Certificate of Origin sign-off.

Every commit in the range must carry a `Signed-off-by:` trailer, which
`git commit -s` adds. Merge commits are skipped: they carry no authored change.

Usage:
    python3 scripts/check-dco.py --range BASE..HEAD
    python3 scripts/check-dco.py --self-test
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

SIGN_OFF = re.compile(r"^Signed-off-by: .+ <[^>]+>$", re.MULTILINE)


def unsigned_commits(repo: Path, revision_range: str) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--no-merges", "--format=%H%x00%s%x00%B%x01", revision_range],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    missing = []
    for record in filter(None, (r.strip() for r in out.split("\x01"))):
        sha, subject, body = record.split("\x00", 2)
        if not SIGN_OFF.search(body):
            missing.append(f"{sha[:12]} {subject}")
    return missing


def _commit(repo: Path, message: str) -> str:
    (repo / "file").write_text(message, encoding="utf-8")
    subprocess.run(["git", "add", "file"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message],
        cwd=repo,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        base = _commit(repo, "base")
        _commit(repo, "signed\n\nSigned-off-by: A Person <a@example.com>")
        _commit(repo, "unsigned")
        _commit(repo, "mentions it in prose: remember Signed-off-by: lines")
        missing = unsigned_commits(repo, f"{base}..HEAD")
    subjects = sorted(line.split(" ", 1)[1] for line in missing)
    expected = ["mentions it in prose: remember Signed-off-by: lines", "unsigned"]
    if subjects != expected:
        print(f"self-test FAILED: flagged {subjects}, expected {expected}")
        return 1
    print("self-test passed: 2 refused, 1 allowed")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--self-test"]:
        return self_test()
    if len(argv) != 3 or argv[1] != "--range" or ".." not in argv[2]:
        print(__doc__)
        return 2
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    missing = unsigned_commits(repo, argv[2])
    if missing:
        print("[check-dco] BLOCKED: these commits have no Signed-off-by line:")
        for line in missing:
            print(f"  {line}")
        print("Amend them with `git commit --amend -s` (or rebase with --signoff).")
        return 1
    print("[check-dco] ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
