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
    """The include is optional: the template has no local.mk, a fork usually does, and
    this test runs in both."""
    assert re.search(r"^-include local\.mk$", MAKEFILE, re.MULTILINE), "no `-include local.mk`"
    assert "local.mk" not in (REPO_ROOT / "copier.yml").read_text(encoding="utf-8"), (
        "local.mk is the repository's own file, never generated from the template"
    )


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


def test_copier_reads_the_checked_out_template_not_the_newest_tag() -> None:
    """Without --vcs-ref, copier copies from the tag that sorts highest, not from HEAD.

    In a fork that carries its own older tags (a `v1.0` from before it adopted the
    template sorts above `v0.10.0`), that tag has no copier.yml and copier renders
    the whole repository at that commit into the strategy directory."""
    copy_line = next(line for line in MAKEFILE.splitlines() if "copier copy" in line)
    assert "--vcs-ref=HEAD" in copy_line, "make new-strategy must copy from HEAD"

    upgrading = (REPO_ROOT / "docs" / "upgrading.md").read_text(encoding="utf-8")
    update_lines = [
        line for line in upgrading.splitlines() if "copier update" in line and "strategies/" in line
    ]
    assert update_lines, "docs/upgrading.md shows the copier update command"
    assert all("--vcs-ref=HEAD" in line for line in update_lines), update_lines
