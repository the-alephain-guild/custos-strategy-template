from pathlib import Path

import pytest

from tools import next as next_step
from tools import ui

REPO = "officina"


def _repo(tmp_path: Path, strategies: tuple[str, ...] = ("trend/supertrend",)) -> Path:
    for name in strategies:
        directory = tmp_path / "strategies" / name
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text("trading: {}\n")
        (directory / "run.yaml").write_text("credential_id: binance-supertrend\n")
    return tmp_path


def _identity(root: Path) -> None:
    vault = root / ".runner" / ".arx" / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (root / ".runner" / ".arx" / "runner.toml").write_text("tenant_id = 'local'\n")
    (vault / "runner-machine.enc").write_text("sealed")


def _key(root: Path, credential: str) -> None:
    (root / ".runner" / ".arx" / "vault" / f"{credential}.enc").write_text("sealed")


def _assess(root: Path, *, running=frozenset(), environment=True, **overrides):
    def docker():
        if isinstance(running, Exception):
            raise running
        return set(running)

    arguments = {
        "strategy": None,
        "mode": "sandbox",
        "toolchain": "pinned",
        "repo_name": REPO,
        "environment_ready": lambda: environment,
        "running_projects": docker,
    }
    arguments.update(overrides)
    return next_step.assess(root, **arguments)


def _commands(assessment) -> list[str]:
    return [command for command, _ in assessment.steps]


def test_an_environment_that_is_not_installed_comes_first(tmp_path) -> None:
    assessment = _assess(_repo(tmp_path), environment=False)

    assert _commands(assessment) == ["make setup"]


def test_the_dev_toolchain_is_asked_for_its_own_setup(tmp_path) -> None:
    assessment = _assess(_repo(tmp_path), environment=False, toolchain="dev")

    assert _commands(assessment) == ["make setup-dev"]


def test_no_strategy_yet_means_creating_one(tmp_path) -> None:
    assessment = _assess(_repo(tmp_path, strategies=()))

    assert _commands(assessment)[0].startswith("make new-strategy NAME=")


def test_no_runner_identity_yet_means_setting_one_up(tmp_path) -> None:
    assessment = _assess(_repo(tmp_path))

    assert _commands(assessment) == ["make setup-runner"]


def test_testnet_needs_its_key_sealed(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)

    assessment = _assess(root, mode="testnet")

    assert _commands(assessment) == ["make setup-key STRATEGY=trend/supertrend MODE=testnet"]


def test_sandbox_needs_no_key(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)

    assessment = _assess(root)

    assert _commands(assessment) == ["make start STRATEGY=trend/supertrend MODE=sandbox"]


def test_a_running_strategy_is_pointed_at_status_logs_and_stop(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _key(root, "binance-supertrend-testnet")

    assessment = _assess(
        root, mode="testnet", toolchain="dev", running={"custos-officina-supertrend-testnet"}
    )

    assert _commands(assessment) == [
        "make status STRATEGY=trend/supertrend MODE=testnet TOOLCHAIN=dev",
        "make logs STRATEGY=trend/supertrend MODE=testnet TOOLCHAIN=dev",
        "make stop STRATEGY=trend/supertrend MODE=testnet TOOLCHAIN=dev",
    ]


def test_docker_that_cannot_be_asked_is_said_and_not_fatal(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)

    assessment = _assess(root, running=next_step.DockerUnavailable("not running"))

    assert _commands(assessment) == ["make start STRATEGY=trend/supertrend MODE=sandbox"]
    assert any("docker" in check.detail for check in assessment.checks)


def test_several_strategies_without_a_choice_are_listed(tmp_path) -> None:
    root = _repo(tmp_path, strategies=("trend/supertrend", "mean/reversion"))
    _identity(root)

    assessment = _assess(root, running={"custos-officina-supertrend-testnet"})

    assert assessment.strategies == {"mean/reversion": [], "trend/supertrend": ["testnet"]}
    assert _commands(assessment) == ["make next STRATEGY=mean/reversion"]


def test_a_named_strategy_that_does_not_exist_is_refused(tmp_path) -> None:
    with pytest.raises(next_step.NextError, match="no strategy"):
        _assess(_repo(tmp_path), strategy="trend/missing")


def test_the_project_name_matches_the_makefile() -> None:
    assert next_step.project_name("officina", "super_trend", "sandbox") == (
        "custos-officina-super-trend-sandbox"
    )


def test_the_report_shows_the_checks_and_the_next_step(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(ui, "Console", None)
    root = _repo(tmp_path)
    _identity(root)

    next_step.show(_assess(root, mode="testnet"))

    out = capsys.readouterr().out
    assert "runner identity" in out and "testnet key" in out
    assert "binance-supertrend-testnet" in out
    assert "make setup-key STRATEGY=trend/supertrend MODE=testnet" in out
