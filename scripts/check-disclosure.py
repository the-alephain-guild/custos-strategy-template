#!/usr/bin/env python3
"""Refuse tracked text that names systems and places outside this repository.

This template is public and is forked by people who have never seen the
maintainers' other systems. Everything a user needs is here or in Custos, which
is public too. A line is refused when it matches any pattern in ``BANNED``,
wherever it appears: prose, code, comments and example payloads alike, because
an internal name pasted into a sample is as public as one written in a sentence.

A line that must mention a banned term on purpose carries
``disclosure-ok: <reason>``; the reason is reviewed like any other change.
This file and the path gate list the terms they refuse and are exempt from the scan.
Paths OWNERSHIP.toml gives to a fork are not scanned: a fork owner's strategies
may use any words they like.

Usage:
    python3 scripts/check-disclosure.py             # scan tracked files
    python3 scripts/check-disclosure.py --self-test # prove the check bites
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

# The two gates must spell out what they refuse, so neither scans them.
EXEMPT = frozenset({"scripts/check-disclosure.py", "scripts/check-public-surface.py"})
ESCAPE = "disclosure-ok:"

BANNED: dict[str, str] = {
    "internal system": (
        r"\b(crucible|speculum|synedrion|athanor|argus|elenchus|grimoire|scriptorium"
        r"|vademecum|curia|sanctum|tabularium|scrutinium|vigil|paperclip)\b"
    ),
    "internal strategy repository": r"philosophers[-_ ]?stone",
    "other organization": r"\b(alchymia|tesseract[-_ ]trading|aegis[-_ ]labs)",
    "internal methodology": r"alpha[-_ ]alchemy",
    "internal database identifier": r"\barx_(live|sim)\b",
    "internal document path": r"(codex/projects|\.claude/rules|\.forge/)",
    # Tracking ids are upper case; matching them case-insensitively would also
    # refuse ordinary words such as "dev-dependencies".
    "internal tracking number": r"(lesson\s*#\s*\d+|(?-i:\bDEV-[0-9A-Z])|(?-i:\bLES-\d{4}))",
    "internal mechanism": r"\b(search_path|saga|outbox|inbox)\b",
    # A person's home directory. /home/custos is the runner image's own user, and
    # its paths are part of the image's public contract.
    "absolute home path": r"(/Users/[A-Za-z]|/home/(?!custos/)[A-Za-z])",
}
_COMPILED = {label: re.compile(pattern, re.IGNORECASE) for label, pattern in BANNED.items()}


def violations_in(text: str) -> list[tuple[int, str, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if ESCAPE in line:
            continue
        for label, pattern in _COMPILED.items():
            match = pattern.search(line)
            if match:
                found.append((number, label, match.group(0)))
    return found


def user_paths(repo: Path) -> tuple[str, ...]:
    """Paths OWNERSHIP.toml gives to the fork; what a user keeps there is theirs."""
    ownership = repo / "OWNERSHIP.toml"
    if not ownership.is_file():
        return ()
    return tuple(tomllib.loads(ownership.read_text(encoding="utf-8")).get("user", []))


def tracked_text_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True
    ).stdout
    skip = user_paths(repo)
    paths = [
        p
        for p in out.decode("utf-8").split("\0")
        if p and p not in EXEMPT and not (skip and p.startswith(skip))
    ]
    text = []
    for rel in paths:
        path = repo / rel
        if path.is_symlink() or not path.is_file():
            continue
        if b"\0" in path.read_bytes()[:8192]:
            continue
        text.append(rel)
    return text


def check(repo: Path) -> list[str]:
    report = []
    for rel in tracked_text_files(repo):
        content = (repo / rel).read_text(encoding="utf-8", errors="replace")
        for number, label, term in violations_in(content):
            report.append(f"{rel}:{number}: {label}: {term!r}")
    return report


REFUSED_LINES = (
    "Deploy through Crucible first.",
    "see philosophers-stone for the original",
    "uses the Alpha Alchemy phases",
    'schema = "arx_live"',
    "details in codex/projects/arx",
    "fixed per lesson #21",
    "tracked as DEV-19-FOO",
    "cd /Users/someone/repo",
    "belongs to tesseract-trading",
    "mounted at /home/alice/work",
)
ALLOWED_LINES = (
    "Run the strategy on Custos in sandbox mode.",
    "Arx authorizes the deployment.",
    "entry point group alephain.strategy_runtime.v1",
    "[package.dev-dependencies]",
    "sealed under /home/custos/.arx/vault",
    "fork https://github.com/the-alephain-guild/custos-strategy-template",
    "a crucible of ideas disclosure-ok: ordinary English in a quoted example",
)


def self_test() -> int:
    failures = []
    for line in REFUSED_LINES:
        if not violations_in(line):
            failures.append(f"not refused: {line!r}")
    for line in ALLOWED_LINES:
        if violations_in(line):
            failures.append(f"refused: {line!r}")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "scripts").mkdir()
        for exempt in EXEMPT:
            (repo / exempt).write_text("crucible\n", encoding="utf-8")
        (repo / "clean.md").write_text("\n".join(ALLOWED_LINES) + "\n", encoding="utf-8")
        (repo / "leak.py").write_text('PAYLOAD = {"db": "arx_sim"}\n', encoding="utf-8")
        (repo / "blob.bin").write_bytes(b"\0crucible")
        (repo / "OWNERSHIP.toml").write_text('user = ["strategies/"]\n', encoding="utf-8")
        (repo / "strategies").mkdir()
        (repo / "strategies" / "notes.md").write_text("a saga strategy\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        report = check(repo)
        if report != ["leak.py:1: internal database identifier: 'arx_sim'"]:
            failures.append(f"repository scan reported {report}")
    if failures:
        print("self-test FAILED:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"self-test passed: {len(REFUSED_LINES)} refused, {len(ALLOWED_LINES)} allowed")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:] == ["--self-test"]:
        return self_test()
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    report = check(repo)
    if report:
        print("[check-disclosure] BLOCKED: these lines name things outside this repository:")
        for line in report:
            print(f"  {line}")
        print(
            "Describe the capability instead of the internal system, or add "
            f"'{ESCAPE} <reason>' to a line that must keep the term."
        )
        return 1
    print("[check-disclosure] ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
