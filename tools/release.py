"""Publish a signed release of one strategy through GitHub Actions.

`make release STRATEGY=<category>/<name>` runs this. It builds nothing itself:
it starts .github/workflows/release-strategy.yml on the commit this checkout
has pushed, follows the run to its end and downloads the publication receipt
into .releases/<category>/<name>/<version>/.

The release is built from the pushed commit, never from local files, so this
refuses a checkout with uncommitted changes or commits the remote does not
have. It pushes nothing: pushing is yours to do.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

WORKFLOW = "release-strategy.yml"
RECEIPT_ARTIFACT_PREFIX = "strategy-release-receipt-"
RECEIPT_FILE = "strategy-release-publication-receipt-v1.json"
# How long to wait for a started run to be listed before giving up.
RUN_APPEAR_SECONDS = 60
# git@github.com:owner/name(.git) or https://github.com/owner/name(.git)
GITHUB_REMOTE = re.compile(
    r"(?:git@github\.com:|https://github\.com/)([^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)


class ReleaseError(RuntimeError):
    """Why a release cannot be started or did not complete."""

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), cwd=ROOT, capture_output=True, text=True, check=False)


def _watch(repository: str, run_id: str) -> int:
    """Show the run's progress on this terminal until it ends; its exit status."""
    return subprocess.run(
        ["gh", "run", "watch", run_id, "--repo", repository, "--exit-status", "--interval", "10"],
        cwd=ROOT,
        check=False,
    ).returncode


def _next(steps: Sequence[tuple[str, str]]) -> None:
    # A command run as a step of another leaves the next steps to the outer one.
    if not os.environ.get("CUSTOS_NO_NEXT"):
        ui.next_steps(steps)


@dataclass(frozen=True)
class Release:
    strategy: str
    version: str
    branch: str
    commit: str
    # <owner>/<name> of origin: gh is always told, because in a fork with an
    # `upstream` remote it would otherwise act on the template's repository.
    repository: str
    root: Path = ROOT

    @property
    def directory(self) -> str:
        return f"strategies/{self.strategy}"

    @property
    def receipt_dir(self) -> Path:
        return self.root / ".releases" / self.strategy / self.version


def _output(run: Run, command: Sequence[str], failure: str, fix: str = "") -> str:
    result = run(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ReleaseError(f"{failure}: {detail[-1] if detail else 'no output'}", fix)
    return result.stdout.strip()


def prepare(strategy: str, run: Run = _run, root: Path = ROOT) -> Release:
    """Everything that must hold before a run is started."""

    parts = strategy.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ReleaseError(
            f"STRATEGY must be <category>/<name>, not {strategy!r}",
            "make release STRATEGY=trend/my_idea",
        )
    pyproject = root / "strategies" / strategy / "pyproject.toml"
    if not pyproject.is_file():
        raise ReleaseError(f"there is no strategy at strategies/{strategy}", "make next")
    version = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseError(f"strategies/{strategy}/pyproject.toml has no [project] version")

    changed = _output(
        run, ["git", "status", "--porcelain", "--untracked-files=all"], "git status failed"
    )
    if changed:
        files = ", ".join(line[3:] for line in changed.splitlines()[:5])
        raise ReleaseError(
            f"a release is built from a commit, and this checkout has changes: {files}",
            "commit them (or stash them) first",
        )
    branch = _output(run, ["git", "rev-parse", "--abbrev-ref", "HEAD"], "git rev-parse failed")
    if branch == "HEAD":
        raise ReleaseError("this checkout is not on a branch", "git switch <branch>")
    commit = _output(run, ["git", "rev-parse", "HEAD"], "git rev-parse failed")
    origin = _output(run, ["git", "remote", "get-url", "origin"], "this checkout has no origin")
    match = GITHUB_REMOTE.search(origin)
    if match is None:
        raise ReleaseError(f"origin is not a GitHub repository: {origin}")
    repository = match.group(1)
    remote = _output(
        run,
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
        "cannot reach the remote",
    ).split()
    if not remote or remote[0] != commit:
        raise ReleaseError(
            f"the workflow builds what origin/{branch} holds, and that is not this commit",
            f"git push origin {branch}",
        )
    _output(
        run,
        ["gh", "auth", "status"],
        "gh is not logged in",
        "gh auth login (and gh auth refresh -s workflow if it lacks that scope)",
    )
    return Release(
        strategy=strategy.strip("/"),
        version=version,
        branch=branch,
        commit=commit,
        repository=repository,
        root=root,
    )


def start(
    release: Release,
    run: Run = _run,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Start the workflow and return the id of the run it created."""

    _output(
        run,
        [
            "gh",
            "workflow",
            "run",
            WORKFLOW,
            "--repo",
            release.repository,
            "--ref",
            release.branch,
            "-f",
            f"strategy={release.directory}",
        ],
        "the release workflow could not be started",
        "see docs/releasing.md, 'If the workflow cannot start'",
    )
    deadline = clock() + RUN_APPEAR_SECONDS
    while True:
        listed = json.loads(
            _output(
                run,
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    release.repository,
                    "--workflow",
                    WORKFLOW,
                    "--branch",
                    release.branch,
                    "--event",
                    "workflow_dispatch",
                    "--limit",
                    "10",
                    "--json",
                    "databaseId,headSha,status",
                ],
                "cannot list the release runs",
            )
            or "[]"
        )
        # Newest first: the first run on this commit that has not finished is ours.
        for entry in listed:
            if entry.get("headSha") == release.commit and entry.get("status") != "completed":
                return str(entry["databaseId"])
        if clock() >= deadline:
            raise ReleaseError(
                "the release run did not appear", f"gh run list --workflow {WORKFLOW}"
            )
        sleep(2)


def follow(
    release: Release,
    run_id: str,
    run: Run = _run,
    watch: Callable[[str, str], int] = _watch,
) -> Path:
    """Wait for the run and download its receipt; raise with the failed jobs if it failed."""

    if watch(release.repository, run_id) != 0:
        view = json.loads(
            _output(
                run,
                ["gh", "run", "view", run_id, "--repo", release.repository, "--json", "jobs,url"],
                "the release failed, and its details could not be read",
            )
        )
        failed = [job["name"] for job in view.get("jobs", []) if job.get("conclusion") == "failure"]
        where = ", ".join(failed) or "an unknown job"
        raise ReleaseError(
            f"the release run failed in {where}: {view.get('url', '')}",
            "if the version is already published, raise it in the strategy's pyproject.toml",
        )
    release.receipt_dir.mkdir(parents=True, exist_ok=True)
    _output(
        run,
        [
            "gh",
            "run",
            "download",
            run_id,
            "--repo",
            release.repository,
            "--pattern",
            f"{RECEIPT_ARTIFACT_PREFIX}*",
            "--dir",
            str(release.receipt_dir),
        ],
        "the release was published but its receipt could not be downloaded",
        f"gh run download {run_id} --repo {release.repository} "
        f"--pattern '{RECEIPT_ARTIFACT_PREFIX}*'",
    )
    receipts = sorted(release.receipt_dir.rglob(RECEIPT_FILE))
    if not receipts:
        raise ReleaseError(f"the downloaded artifact has no {RECEIPT_FILE}")
    return receipts[-1]


def summarize(release: Release, receipt_path: Path) -> None:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    ui.ok(f"released {release.strategy} {release.version}", tag="release")
    ui.table(
        "Release",
        [
            ("package", str(receipt.get("repository", ""))),
            ("tag", str(receipt.get("discovery_tag", ""))),
            ("manifest", str(receipt.get("release_manifest_digest", ""))),
            ("commit", release.commit),
            ("receipt", str(receipt_path.relative_to(release.root))),
        ],
    )
    _next(
        [
            (f"cat {receipt_path.relative_to(release.root)}", "the receipt a deployment refers to"),
            (
                f"make arx-evidence STRATEGY={release.strategy} VERSION={release.version}",
                "read it back and check it, to deploy it through ARX (docs/deploying.md)",
            ),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("strategy", help="<category>/<name>, the directory under strategies/")
    args = parser.parse_args(argv)
    try:
        release = prepare(args.strategy)
        ui.info(
            f"releasing {release.strategy} {release.version} from {release.commit[:12]} "
            f"on {release.branch}",
            tag="release",
        )
        run_id = start(release)
        receipt = follow(release, run_id)
    except ReleaseError as failure:
        ui.error(str(failure), tag="release")
        if failure.fix:
            _next([(failure.fix, "")])
        return 1
    summarize(release, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
