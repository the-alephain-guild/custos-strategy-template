import json
from pathlib import Path

import pytest

from tools import next as next_step
from tools import ui

REPO = "officina"
ROOT = Path(__file__).resolve().parents[1]


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


# Venue profiles


def _profile(root: Path, strategy: str, venue_id: str) -> None:
    folder = root / "strategies" / strategy / "venues"
    folder.mkdir(exist_ok=True)
    (folder / f"{venue_id}.yaml").write_text('trading:\n  connector:\n    value: "sodex"\n')


def test_a_profiled_run_has_its_own_project_name() -> None:
    assert next_step.project_name("officina", "super_trend", "sandbox", "sodex") == (
        "custos-officina-super-trend-sodex-sandbox"
    )
    assert next_step.project_name("officina", "super_trend", "sandbox") == (
        "custos-officina-super-trend-sandbox"
    )


def test_a_strategys_profiles_are_listed(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _profile(root, "trend/supertrend", "sodex")
    _profile(root, "trend/supertrend", "okx")

    assessment = _assess(root)

    venues = [check for check in assessment.checks if check.name == "venue profiles"]
    assert venues and venues[0].detail == "okx, sodex (VENUE=<id> runs one)"


def test_a_profile_is_looked_at_on_its_own(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _profile(root, "trend/supertrend", "sodex")

    assessment = _assess(root, running={"custos-officina-supertrend-sodex-sandbox"}, venue="sodex")

    assert (
        _commands(assessment)[0] == "make status STRATEGY=trend/supertrend VENUE=sodex MODE=sandbox"
    )
    assert any(check.name == "running" and check.done for check in assessment.checks)


def test_a_profile_that_does_not_exist_is_refused(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    with pytest.raises(next_step.NextError, match="no venue profile 'sodex'"):
        _assess(root, venue="sodex")


def _release(root: Path, strategy: str, version: str) -> None:
    run = root / ".releases" / strategy / version / "strategy-release-receipt-1-1"
    run.mkdir(parents=True)
    (run / "strategy-release-publication-receipt-v1.json").write_text("{}")


def test_a_released_strategy_is_pointed_at_signing_in_to_arx(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    _release(root, "trend/supertrend", "0.10.0")

    assessment = _assess(root)

    assert _commands(assessment) == [
        "make start STRATEGY=trend/supertrend MODE=sandbox",
        "make arx-login ARX_URL=https://arx.example.com",
    ]
    assert ("release", True, "0.10.0 released") in [
        (check.name, check.done, check.detail) for check in assessment.checks
    ]


def test_a_released_strategy_with_an_arx_session_is_pointed_at_it(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")

    assessment = _assess(root, arx_signed_in=lambda: True)

    assert "make arx-evidence STRATEGY=trend/supertrend VERSION=0.2.0" in _commands(assessment)


def test_a_released_strategy_with_a_deploy_file_is_pointed_at_the_preview(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    (root / "strategies" / "trend" / "supertrend" / "deploy.yaml").write_text("log_level: INFO\n")

    assessment = _assess(root, arx_signed_in=lambda: True)

    assert _commands(assessment)[-3:] == [
        "make arx-evidence STRATEGY=trend/supertrend VERSION=0.2.0",
        "make deploy-preview STRATEGY=trend/supertrend MODE=sandbox VERSION=0.2.0 "
        "RUNNER=<runner id>",
        "make deploy STRATEGY=trend/supertrend MODE=sandbox VERSION=0.2.0 RUNNER=<runner id>",
    ]


def test_a_deployed_release_is_pointed_at_stopping_it(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    (root / "strategies" / "trend" / "supertrend" / "deploy.yaml").write_text("log_level: INFO\n")
    deployed = root / ".deployments" / "trend" / "supertrend" / "0.2.0"
    deployed.mkdir(parents=True)
    (deployed / "sandbox-5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b.json").write_text("{}")

    assessment = _assess(root, arx_signed_in=lambda: True)

    assert _commands(assessment)[-1] == (
        "make deploy-stop STRATEGY=trend/supertrend MODE=sandbox VERSION=0.2.0"
    )
    assert not [c for c in _commands(assessment) if c.startswith("make deploy ")]
    assert ("deployment", True, "0.2.0 deployed in sandbox") in [
        (check.name, check.done, check.detail) for check in assessment.checks
    ]


def test_the_preview_is_not_offered_before_signing_in(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    (root / "strategies" / "trend" / "supertrend" / "deploy.yaml").write_text("log_level: INFO\n")

    assessment = _assess(root)

    assert not [c for c in _commands(assessment) if "deploy" in c]


def test_a_strategy_never_released_is_not_pointed_at_arx(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)

    assessment = _assess(root, arx_signed_in=lambda: True)

    assert not [c for c in _commands(assessment) if "arx" in c]


def _deployed(root: Path, version: str, state: str | None, mode: str = "sandbox") -> None:
    directory = root / ".deployments" / "trend" / "supertrend" / version
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {} if state is None else {"state": state}
    (directory / f"{mode}-5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b.json").write_text(
        json.dumps(receipt)
    )
    (directory / ".progress.json").write_text("{}")


def _deploy_file(root: Path) -> None:
    (root / "strategies" / "trend" / "supertrend" / "deploy.yaml").write_text("log_level: INFO\n")


def test_an_older_release_still_running_is_stopped_before_the_new_one_deploys(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.1.0")
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)
    _deployed(root, "0.1.0", "running")

    commands = _commands(_assess(root, arx_signed_in=lambda: True))

    stop = "make deploy-stop STRATEGY=trend/supertrend MODE=sandbox VERSION=0.1.0"
    deploy = "make deploy STRATEGY=trend/supertrend MODE=sandbox VERSION=0.2.0 RUNNER=<runner id>"
    assert stop in commands and deploy in commands
    assert commands.index(stop) < commands.index(deploy)


def test_a_stopped_deployment_is_not_offered_a_stop_again(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)
    _deployed(root, "0.2.0", "stopped")
    _deployed(root, "0.2.0", "stopped", mode="testnet")

    assessment = _assess(root, arx_signed_in=lambda: True)

    assert not [c for c in _commands(assessment) if c.startswith("make deploy-stop")]
    assert _commands(assessment)[-1].startswith("make deploy STRATEGY=trend/supertrend")
    assert "change deploy.yaml" in assessment.steps[-1][1]
    assert ("deployment", False, "0.2.0 stopped in sandbox") in [
        (check.name, check.done, check.detail) for check in assessment.checks
    ]


def test_a_refused_spec_or_another_mode_is_not_running(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)
    _deployed(root, "0.1.0", "refused")
    _deployed(root, "0.1.0", "running", mode="testnet")

    commands = _commands(_assess(root, arx_signed_in=lambda: True))

    assert not [c for c in commands if c.startswith("make deploy-stop")]


def test_a_receipt_that_cannot_be_read_counts_as_running(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)
    _deployed(root, "0.2.0", None)
    directory = root / ".deployments" / "trend" / "supertrend" / "0.2.0"
    (directory / "sandbox-5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b.json").write_text("{not json")

    commands = _commands(_assess(root, arx_signed_in=lambda: True))

    assert commands[-1] == "make deploy-stop STRATEGY=trend/supertrend MODE=sandbox VERSION=0.2.0"


def test_a_released_strategy_without_a_deploy_file_is_told_where_to_get_one(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")

    commands = _commands(_assess(root, arx_signed_in=lambda: True))

    assert commands[-1] == "cp examples/trend/sma_cross/deploy.yaml strategies/trend/supertrend/"
    assert (ROOT / "examples" / "trend" / "sma_cross" / "deploy.yaml").is_file()
    assert not [c for c in commands if c.startswith("make deploy")]


def test_arx_steps_do_not_wait_for_a_local_runner_identity(tmp_path) -> None:
    root = _repo(tmp_path)
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)

    commands = _commands(_assess(root, arx_signed_in=lambda: True))

    assert commands[0] == "make setup-runner"
    assert commands[-1].startswith("make deploy STRATEGY=trend/supertrend MODE=sandbox")


def test_arx_steps_do_not_wait_for_a_sealed_testnet_key(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)

    commands = _commands(_assess(root, mode="testnet", arx_signed_in=lambda: True))

    assert commands[0].startswith("make setup-key")
    assert commands[-1].startswith("make deploy STRATEGY=trend/supertrend MODE=testnet")


def test_arx_steps_are_offered_beside_a_local_run(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    project = next_step.project_name(REPO, "supertrend", "sandbox")

    commands = _commands(_assess(root, running={project}))

    assert commands[:3] == [
        "make status STRATEGY=trend/supertrend MODE=sandbox",
        "make logs STRATEGY=trend/supertrend MODE=sandbox",
        "make stop STRATEGY=trend/supertrend MODE=sandbox",
    ]
    assert commands[-1] == "make arx-login ARX_URL=https://arx.example.com"


def test_the_deployment_is_offered_without_a_product_to_name(tmp_path) -> None:
    root = _repo(tmp_path)
    _identity(root)
    _release(root, "trend/supertrend", "0.2.0")
    _deploy_file(root)

    step = _assess(root, arx_signed_in=lambda: True).steps[-1]

    assert "PRODUCT" not in step[0]
    assert "creates the strategy's product" in step[1]
