"""`make release` starts the release workflow on a pushed commit and fetches its receipt.

Git runs for real, in a repository with a bare remote next to it; `gh` is a fake
that answers the few commands the release tool sends.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tools import release

COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=COMMIT_ENV
        | {
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(repo),
        },
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    strategy = work / "strategies" / "trend" / "my_idea"
    strategy.mkdir(parents=True)
    (strategy / "pyproject.toml").write_text(
        '[project]\nname = "strategy-my-idea"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "a strategy")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    # A fork also tracks the template; gh must not act on it.
    git(work, "remote", "add", "upstream", "https://github.com/template-owner/template.git")
    return work


class FakeGh:
    """Answers gh commands; records what it was asked."""

    def __init__(self, repo: Path, *, logged_in: bool = True, run_listed: bool = True) -> None:
        self.repo = repo
        self.logged_in = logged_in
        self.run_listed = run_listed
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        if command[0] == "git":
            if command[1:4] == ["remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(
                    command, 0, "git@github.com:fork-owner/strategies.git\n", ""
                )
            return subprocess.run(command, cwd=self.repo, capture_output=True, text=True)
        self.calls.append(command)
        out, code = "", 0
        if command[1:3] == ["auth", "status"]:
            code = 0 if self.logged_in else 1
        elif command[1:3] == ["run", "list"]:
            head = git(self.repo, "rev-parse", "HEAD")
            runs = [{"databaseId": 42, "headSha": head, "status": "queued"}]
            out = json.dumps(runs if self.run_listed else [])
        elif command[1:3] == ["run", "view"]:
            out = json.dumps(
                {
                    "url": "https://github.com/o/r/actions/runs/42",
                    "jobs": [
                        {"name": "identity", "conclusion": "success"},
                        {"name": "candidate", "conclusion": "failure"},
                    ],
                }
            )
        elif command[1:3] == ["run", "download"]:
            target = Path(command[command.index("--dir") + 1]) / "strategy-release-receipt-42-1"
            target.mkdir(parents=True)
            (target / release.RECEIPT_FILE).write_text(
                json.dumps(
                    {
                        "repository": "ghcr.io/o/r/strategy-releases",
                        "discovery_tag": "trend-my_idea-0.2.0",
                        "release_manifest_digest": "sha256:" + "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, code, out, "not logged in" if code else "")


def test_a_pushed_strategy_is_released_and_its_receipt_kept(repo: Path) -> None:
    gh = FakeGh(repo)

    prepared = release.prepare("trend/my_idea", run=gh, root=repo)
    run_id = release.start(prepared, run=gh, sleep=lambda _: None)
    receipt = release.follow(prepared, run_id, run=gh, watch=lambda *_: 0)

    assert (prepared.version, prepared.branch) == ("0.2.0", "main")
    assert ["gh", "workflow", "run", "release-strategy.yml", "--repo", "fork-owner/strategies",
            "--ref", "main", "-f", "strategy=strategies/trend/my_idea"] in gh.calls  # fmt: skip
    assert run_id == "42"
    assert receipt.is_relative_to(repo / ".releases" / "trend" / "my_idea" / "0.2.0")
    assert json.loads(receipt.read_text())["discovery_tag"] == "trend-my_idea-0.2.0"


def test_a_failed_run_names_the_failed_job_and_the_run(repo: Path) -> None:
    gh = FakeGh(repo)
    prepared = release.prepare("trend/my_idea", run=gh, root=repo)

    with pytest.raises(release.ReleaseError, match="failed in candidate: https://github.com/o/r"):
        release.follow(prepared, "42", run=gh, watch=lambda *_: 1)


def test_a_commit_the_remote_does_not_have_is_refused(repo: Path) -> None:
    (repo / "notes.txt").write_text("more", encoding="utf-8")
    git(repo, "add", "notes.txt")
    git(repo, "commit", "-q", "-m", "not pushed")

    with pytest.raises(release.ReleaseError, match="origin/main") as refused:
        release.prepare("trend/my_idea", run=FakeGh(repo), root=repo)
    assert refused.value.fix == "git push origin main"


def test_uncommitted_changes_are_refused(repo: Path) -> None:
    (repo / "strategies" / "trend" / "my_idea" / "config.yaml").write_text("x: 1\n")

    with pytest.raises(release.ReleaseError, match="config.yaml"):
        release.prepare("trend/my_idea", run=FakeGh(repo), root=repo)


def test_gh_must_be_logged_in(repo: Path) -> None:
    with pytest.raises(release.ReleaseError, match="gh is not logged in") as refused:
        release.prepare("trend/my_idea", run=FakeGh(repo, logged_in=False), root=repo)
    assert "gh auth login" in refused.value.fix


@pytest.mark.parametrize("strategy", ["my_idea", "trend/", "trend/my_idea/extra"])
def test_strategy_must_be_category_and_name(repo: Path, strategy: str) -> None:
    with pytest.raises(release.ReleaseError, match="<category>/<name>"):
        release.prepare(strategy, run=FakeGh(repo), root=repo)


def test_a_missing_strategy_is_refused(repo: Path) -> None:
    with pytest.raises(release.ReleaseError, match="no strategy at strategies/trend/other"):
        release.prepare("trend/other", run=FakeGh(repo), root=repo)


def test_a_strategy_without_a_version_is_refused(repo: Path) -> None:
    pyproject = repo / "strategies" / "trend" / "my_idea" / "pyproject.toml"
    pyproject.write_text('[project]\nname = "strategy-my-idea"\n', encoding="utf-8")
    git(repo, "commit", "-q", "-am", "no version")
    git(repo, "push", "-q", "origin", "main")

    with pytest.raises(release.ReleaseError, match="has no \\[project\\] version"):
        release.prepare("trend/my_idea", run=FakeGh(repo), root=repo)


def test_a_run_that_never_appears_is_reported(repo: Path) -> None:
    gh = FakeGh(repo, run_listed=False)
    prepared = release.prepare("trend/my_idea", run=gh, root=repo)
    ticks = iter(range(0, 1000, 30))

    with pytest.raises(release.ReleaseError, match="did not appear"):
        release.start(prepared, run=gh, clock=lambda: next(ticks), sleep=lambda _: None)


def test_every_gh_call_names_origin_not_the_template(repo: Path) -> None:
    gh = FakeGh(repo)

    prepared = release.prepare("trend/my_idea", run=gh, root=repo)
    run_id = release.start(prepared, run=gh, sleep=lambda _: None)
    watched: list[tuple[str, str]] = []
    release.follow(prepared, run_id, run=gh, watch=lambda *args: watched.append(args) or 0)

    assert prepared.repository == "fork-owner/strategies"
    assert watched == [("fork-owner/strategies", "42")]
    repo_scoped = [call for call in gh.calls if call[1] != "auth"]
    assert repo_scoped
    for call in repo_scoped:
        assert call[call.index("--repo") + 1] == "fork-owner/strategies", call


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:owner/name.git", "owner/name"),
        ("https://github.com/owner/name.git", "owner/name"),
        ("https://github.com/owner/name", "owner/name"),
    ],
)
def test_origin_urls_name_the_repository(url: str, expected: str) -> None:
    match = release.GITHUB_REMOTE.search(url)

    assert match is not None and match.group(1) == expected


def test_a_finished_release_points_at_reading_it_back_for_arx(tmp_path, monkeypatch) -> None:
    shown: list[list[tuple[str, str]]] = []
    monkeypatch.delenv("CUSTOS_NO_NEXT", raising=False)
    monkeypatch.setattr(release.ui, "next_steps", lambda steps: shown.append(list(steps)))
    receipt = tmp_path / ".releases" / "trend" / "my_idea" / "0.2.0" / "receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}", encoding="utf-8")
    finished = release.Release("trend/my_idea", "0.2.0", "main", "0" * 40, "o/r", root=tmp_path)

    release.summarize(finished, receipt)

    assert [command for command, _ in shown[-1]][-1] == (
        "make arx-evidence STRATEGY=trend/my_idea VERSION=0.2.0"
    )
