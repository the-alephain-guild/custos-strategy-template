"""A private fork extends the Makefile through local.mk instead of editing it.

The template's Makefile is upstream-owned: a fork that edits it conflicts on every
update. Two extension points let a fork add its own targets and choose which checks
`make verify` runs, in a file the template never ships.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

# Every check `make verify` runs today, in order. A fork overrides the list; the
# template's default must keep naming all of them.
VERIFY_CHECKS = (
    "verify-pinned",
    "check-public-surface",
    "check-disclosure",
    "check-ownership",
    "check-dco",
    "check-publisher-pin",
    "lint",
    "test",
)


def test_the_makefile_includes_an_optional_local_mk() -> None:
    assert re.search(r"^-include local\.mk$", MAKEFILE, re.MULTILINE), "no `-include local.mk`"
    assert not (REPO_ROOT / "local.mk").exists(), "the template must not ship a local.mk"


def test_verify_runs_the_checks_named_in_VERIFY_CHECKS() -> None:
    match = re.search(r"^VERIFY_CHECKS \?= (.+)$", MAKEFILE, re.MULTILINE)
    assert match, "VERIFY_CHECKS must have a `?=` default so local.mk can override it"
    assert tuple(match.group(1).split()) == VERIFY_CHECKS
    assert re.search(r"^verify: \$\(VERIFY_CHECKS\)", MAKEFILE, re.MULTILINE)


def test_a_local_mk_can_replace_the_checks_and_add_a_target(tmp_path: Path) -> None:
    """Dry-run `make verify` against a copy of the Makefile plus a local.mk."""
    (tmp_path / "tools" / "runner").mkdir(parents=True)
    (tmp_path / "tools" / "runner" / "runner.mk").write_text("", encoding="utf-8")
    (tmp_path / "Makefile").write_text(MAKEFILE, encoding="utf-8")
    (tmp_path / "local.mk").write_text(
        "VERIFY_CHECKS = fork-check lint\n"
        "##@ Fork\n"
        "fork-check:  ## A check only this fork runs\n"
        "\t@echo fork-check-ran\n",
        encoding="utf-8",
    )

    dry_run = subprocess.run(
        ["make", "-n", "verify"], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    assert dry_run.returncode == 0, dry_run.stderr
    assert "fork-check-ran" in dry_run.stdout
    assert "ruff format --check" in dry_run.stdout
    assert "check-disclosure.py" not in dry_run.stdout, "the fork's list replaced the default"
