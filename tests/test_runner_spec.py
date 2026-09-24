from pathlib import Path

import pytest

from tools.runner import spec

CONFIG = """
strategy:
  name: "demo"
trading:
  connector:
    value: "binance_perpetual"
  pairs:
    value: ["BTC-USDT"]
  leverage:
    value: 2
"""
RUN = """
credential_id: binance-demo
risk_config:
  max_total_notional: 1000
"""


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(spec, "ROOT", tmp_path)
    directory = tmp_path / "strategies" / "trend" / "demo"
    directory.mkdir(parents=True)
    (directory / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (directory / "run.yaml").write_text(RUN, encoding="utf-8")
    return directory


def test_a_sandbox_spec_points_at_the_mounted_strategy(strategy: Path) -> None:
    rendered = spec.build_spec(strategy, mode="sandbox", generation=7, lifecycle_state="running")
    assert rendered["strategy_path"] == "/opt/repo/strategies/trend/demo"
    assert rendered["strategy_registry_name"] == "demo"
    assert rendered["spec_id"] == "demo-sandbox"
    assert rendered["provenance_ref"] == {"credential_id": "binance-demo"}
    assert rendered["connector"] == "binance_perpetual"
    assert rendered["pairs"] == ["BTC-USDT"]
    assert rendered["leverage"] == 2
    assert rendered["risk_config"] == {"max_total_notional": 1000}
    assert rendered["sandbox"] == {"starting_balances": ["10000 USDT"]}
    assert rendered["strategy_config"] == {}
    assert "nautilus_config" not in rendered


def test_exchange_account_settings_reach_the_runner_unchanged(strategy: Path) -> None:
    (strategy / "run.yaml").write_text(
        RUN + "venue:\n  settlement_currency: vUSDC\n  sodex_account_id: '42'\n",
        encoding="utf-8",
    )
    rendered = spec.build_spec(strategy, mode="sandbox", generation=1, lifecycle_state="running")
    assert rendered["nautilus_config"] == {
        "venue": {"settlement_currency": "vUSDC", "sodex_account_id": "42"}
    }


def test_a_testnet_spec_has_no_simulated_account(strategy: Path) -> None:
    rendered = spec.build_spec(strategy, mode="testnet", generation=7, lifecycle_state="stopped")
    assert "sandbox" not in rendered
    assert rendered["trading_mode"] == "testnet"
    assert rendered["lifecycle_state"] == "stopped"


def test_live_is_refused(strategy: Path) -> None:
    with pytest.raises(spec.SpecError, match="live needs an enrolled runner"):
        spec.build_spec(strategy, mode="live", generation=7, lifecycle_state="running")


@pytest.mark.parametrize("generation", [0, -1, True])
def test_a_generation_must_be_a_positive_integer(strategy: Path, generation: object) -> None:
    with pytest.raises(spec.SpecError, match="positive integer"):
        spec.build_spec(strategy, mode="sandbox", generation=generation, lifecycle_state="running")


def test_a_strategy_without_run_yaml_says_what_is_missing(strategy: Path) -> None:
    (strategy / "run.yaml").unlink()
    with pytest.raises(spec.SpecError, match="names the credential"):
        spec.build_spec(strategy, mode="sandbox", generation=1, lifecycle_state="running")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("credential_id: x\nsurprise: 1\n", "unknown keys"),
        ("credential_id: ''\n", "non-empty string"),
        ("credential_id: x\nrisk_config: 5\n", "mapping of ceilings"),
        ("credential_id: x\nvenue: global\n", "mapping of exchange account settings"),
    ],
)
def test_malformed_run_yaml_is_refused(strategy: Path, body: str, message: str) -> None:
    (strategy / "run.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(spec.SpecError, match=message):
        spec.build_spec(strategy, mode="sandbox", generation=1, lifecycle_state="running")


def test_strategies_are_found_by_their_short_path(strategy: Path) -> None:
    assert spec.strategy_dir("trend/demo") == strategy.resolve()
    with pytest.raises(spec.SpecError, match="no strategy"):
        spec.strategy_dir("trend/absent")
