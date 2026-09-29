"""`make add-venue` gives a strategy a venue profile it can run on."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from tools import ui, venues

ROOT = Path(__file__).resolve().parents[1]


def _load():
    location = ROOT / "scripts" / "add-venue.py"
    spec = importlib.util.spec_from_file_location("add_venue", location)
    module = importlib.util.module_from_spec(spec)
    sys.modules["add_venue"] = module
    spec.loader.exec_module(module)
    return module


add_venue = _load()

CONFIG = """\
strategy:
  name: "demo"
parameters:
  lookback:
    value: 20
trading:
  connector:
    value: "binance_perpetual"
  pairs:
    value: ["BTC-USDT"]
  leverage:
    value: 1
platforms:
  nautilus:
    bar_type:
      value: "1-HOUR"
"""
RUN = """\
# The name this strategy's exchange keys are sealed under.
credential_id: binance-demo

# The simulated account a sandbox run starts with.
sandbox:
  starting_balances: ["10000 USDT"]

risk_config:
  max_total_notional: 1000
"""


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tools.runner import spec

    monkeypatch.setattr(spec, "ROOT", tmp_path)
    monkeypatch.setattr(add_venue, "ROOT", tmp_path)
    monkeypatch.setattr(ui, "Console", None)
    directory = tmp_path / "strategies" / "trend" / "demo"
    directory.mkdir(parents=True)
    (directory / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (directory / "run.yaml").write_text(RUN, encoding="utf-8")
    return directory


def _run(*argv: str) -> int:
    return add_venue.main(["add-venue.py", *argv])


def test_a_profile_is_written_with_its_run_settings(strategy: Path) -> None:
    assert _run("trend/demo", "--connector", "sodex", "--pair", "vBTC_vUSDC") == 0

    profile = yaml.safe_load((strategy / "venues" / "sodex.yaml").read_text(encoding="utf-8"))
    assert profile == {
        "trading": {
            "connector": {"value": "sodex"},
            "pairs": {"value": ["vBTC_vUSDC"]},
            "leverage": {"value": 1},
        }
    }
    text = (strategy / "run.yaml").read_text(encoding="utf-8")
    assert text.startswith(RUN), "what was there, comments included, is kept"
    settings = yaml.safe_load(text)
    assert settings["venues"] == {
        "sodex": {
            "credential_id": "sodex-demo-sodex",
            "sandbox": {"starting_balances": ["10000 vUSDC"]},
            "venue": {"settlement_currency": "vUSDC"},
        }
    }
    assert "# wallet_address:" in text and "# sodex_account_id:" in text
    # The profile renders and names its own credential.
    rendered = venues.render_run_dir(strategy.parents[2], strategy, "sodex")
    assert (
        yaml.safe_load((rendered / "run.yaml").read_text())["credential_id"] == "sodex-demo-sodex"
    )


def test_an_okx_profile_carries_the_account_settings_okx_needs(strategy: Path) -> None:
    assert _run("trend/demo", "--connector", "okx_perpetual", "--pair", "ETH-USDT") == 0
    settings = yaml.safe_load((strategy / "run.yaml").read_text(encoding="utf-8"))
    assert settings["venues"]["okx_perpetual"]["venue"] == {
        "region": "global",
        "margin_mode": "cross",
    }
    assert settings["venues"]["okx_perpetual"]["sandbox"] == {"starting_balances": ["10000 USDT"]}


def test_a_binance_profile_needs_no_account_settings(strategy: Path) -> None:
    assert _run("trend/demo", "--connector", "binance", "--pair", "BTC-USDT") == 0
    settings = yaml.safe_load((strategy / "run.yaml").read_text(encoding="utf-8"))
    assert settings["venues"]["binance"] == {
        "credential_id": "binance-demo-binance",
        "sandbox": {"starting_balances": ["10000 USDT"]},
    }


def test_a_second_profile_joins_the_first_under_venues(strategy: Path) -> None:
    assert _run("trend/demo", "--connector", "sodex", "--pair", "vBTC_vUSDC") == 0
    assert _run("trend/demo", "--connector", "okx", "--pair", "BTC-USDT") == 0
    settings = yaml.safe_load((strategy / "run.yaml").read_text(encoding="utf-8"))
    assert set(settings["venues"]) == {"sodex", "okx"}
    assert settings["venues"]["sodex"]["credential_id"] == "sodex-demo-sodex"
    assert settings["credential_id"] == "binance-demo"


def test_a_profile_may_be_named_and_list_several_pairs(strategy: Path) -> None:
    pairs = "vBTC_vUSDC,vETH_vUSDC"
    assert _run("trend/demo", "--connector", "sodex", "--pair", pairs, "--venue", "sodex_two") == 0
    profile = yaml.safe_load((strategy / "venues" / "sodex_two.yaml").read_text())
    assert profile["trading"]["pairs"]["value"] == ["vBTC_vUSDC", "vETH_vUSDC"]
    settings = yaml.safe_load((strategy / "run.yaml").read_text(encoding="utf-8"))
    assert settings["venues"]["sodex_two"]["credential_id"] == "sodex-demo-sodex_two"


def test_a_profile_that_exists_is_refused(strategy: Path, capsys) -> None:
    assert _run("trend/demo", "--connector", "sodex", "--pair", "vBTC_vUSDC") == 0
    before = (strategy / "run.yaml").read_text(encoding="utf-8")
    assert _run("trend/demo", "--connector", "sodex", "--pair", "vETH_vUSDC") == 1
    assert "already has a venue profile 'sodex'" in capsys.readouterr().err
    assert (strategy / "run.yaml").read_text(encoding="utf-8") == before


def test_a_pair_spelt_for_another_exchange_is_refused_and_nothing_is_written(
    strategy: Path, capsys
) -> None:
    assert _run("trend/demo", "--connector", "sodex", "--pair", "BTC-USDT") == 1
    assert "written like vBTC_vUSDC" in capsys.readouterr().err
    assert not (strategy / "venues").exists()
    assert (strategy / "run.yaml").read_text(encoding="utf-8") == RUN


def test_a_bar_the_exchange_does_not_serve_is_refused(strategy: Path, capsys) -> None:
    (strategy / "config.yaml").write_text(CONFIG.replace("1-HOUR", "3-MINUTE"), encoding="utf-8")
    assert _run("trend/demo", "--connector", "sodex_perpetual", "--pair", "BTC-USD") == 1
    err = capsys.readouterr().err
    assert "no 3m bars" in err and "1m, 5m" in err
    assert not (strategy / "venues").exists()


def test_an_extra_timeframe_the_exchange_does_not_serve_is_refused(strategy: Path, capsys) -> None:
    (strategy / "config.yaml").write_text(
        CONFIG + 'backtesting:\n  additional_timeframes:\n    value: ["6h"]\n', encoding="utf-8"
    )
    assert _run("trend/demo", "--connector", "sodex_perpetual", "--pair", "BTC-USD") == 1
    assert "no 6h bars" in capsys.readouterr().err


def test_a_credential_already_used_by_another_profile_is_refused(strategy: Path, capsys) -> None:
    assert _run("trend/demo", "--connector", "sodex", "--pair", "vBTC_vUSDC") == 0
    assert _run(
        "trend/demo", "--connector", "sodex", "--pair", "vETH_vUSDC", "--venue", "sodex_eth",
        "--credential-id", "sodex-demo-sodex",
    ) == 1  # fmt: skip
    assert "share the credential" in capsys.readouterr().err
    assert not (strategy / "venues" / "sodex_eth.yaml").exists()


@pytest.mark.parametrize("venue_id", ["Sodex", "so-dex", "1st"])
def test_a_profile_name_is_lowercase_letters_digits_and_underscores(strategy, venue_id, capsys):
    assert (
        _run("trend/demo", "--connector", "sodex", "--pair", "vBTC_vUSDC", "--venue", venue_id) == 1
    )
    assert "lowercase letters" in capsys.readouterr().err


def test_an_unknown_strategy_is_refused(strategy: Path, capsys) -> None:
    assert _run("trend/absent", "--connector", "sodex", "--pair", "vBTC_vUSDC") == 1
    assert "no strategy" in capsys.readouterr().err
