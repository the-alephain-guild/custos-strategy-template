"""A venue profile lays an exchange over a strategy's config.yaml without touching it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tools import venues

CONFIG = """
strategy:
  name: "demo"
parameters:
  lookback:
    value: 20
trading:
  connector:
    value: "binance"
  pairs:
    value: ["BTC-USDT", "ETH-USDT"]
  leverage:
    value: 1
  fees:
    maker:
      value: 0.001
    taker:
      value: 0.001
warmup:
  mode:
    value: "warmup"
platforms:
  nautilus:
    bar_type:
      value: "4-HOUR"
"""
RUN = """
credential_id: binance-demo
sandbox:
  starting_balances: ["10000 USDT"]
risk_config:
  max_total_notional: 1000
"""
SODEX = """
trading:
  connector:
    value: "sodex"
  pairs:
    value: ["vBTC_vUSDC"]
  fees:
    taker:
      value: 0.0005
"""


@pytest.fixture
def strategy(tmp_path: Path) -> Path:
    directory = tmp_path / "strategies" / "trend" / "demo"
    (directory / "venues").mkdir(parents=True)
    (directory / "refinement" / "nautilus").mkdir(parents=True)
    (directory / "refinement" / "nautilus" / "strategy.py").write_text("# code\n")
    (directory / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (directory / "run.yaml").write_text(RUN, encoding="utf-8")
    (directory / "venues" / "sodex.yaml").write_text(SODEX, encoding="utf-8")
    return directory


# Merging


def test_mappings_merge_key_by_key_and_lists_are_replaced_whole(strategy: Path) -> None:
    merged = venues.render_config(strategy, "sodex")
    trading = merged["trading"]
    assert trading["connector"]["value"] == "sodex"
    assert trading["pairs"]["value"] == ["vBTC_vUSDC"], "a list is what the profile says"
    assert trading["leverage"]["value"] == 1, "a key the profile leaves out is kept"
    assert trading["fees"] == {"maker": {"value": 0.001}, "taker": {"value": 0.0005}}
    assert merged["parameters"] == {"lookback": {"value": 20}}
    assert merged["platforms"]["nautilus"]["bar_type"]["value"] == "4-HOUR"


def test_merging_leaves_the_inputs_alone() -> None:
    default = {"trading": {"pairs": ["A"], "fees": {"maker": 1}}}
    profile = {"trading": {"pairs": ["B"]}}
    merged = venues.merge(default, profile)
    merged["trading"]["fees"]["maker"] = 9
    assert default["trading"]["fees"]["maker"] == 1
    assert default["trading"]["pairs"] == ["A"]


def test_the_profile_may_set_only_the_exchange_sections(strategy: Path) -> None:
    (strategy / "venues" / "sodex.yaml").write_text(
        SODEX + "parameters:\n  lookback:\n    value: 5\nrisk:\n  x: 1\n", encoding="utf-8"
    )
    with pytest.raises(venues.VenueError, match=r"sodex\.yaml sets parameters, risk") as caught:
        venues.load_profile(strategy, "sodex")
    assert "backtesting, platforms, trading, warmup" in str(caught.value)


def test_an_unknown_profile_is_refused_with_the_ones_there_are(strategy: Path) -> None:
    (strategy / "venues" / "okx.yaml").write_text("trading: {}\n", encoding="utf-8")
    with pytest.raises(venues.VenueError, match="no venue profile 'bybit'; it has: okx, sodex"):
        venues.load_profile(strategy, "bybit")


def test_a_strategy_without_profiles_says_so(strategy: Path) -> None:
    (strategy / "venues" / "sodex.yaml").unlink()
    with pytest.raises(venues.VenueError, match="it has: none"):
        venues.load_profile(strategy, "sodex")


@pytest.mark.parametrize("venue_id", ["Sodex", "1x", "so-dex", "", "so/dex"])
def test_a_profile_id_is_lowercase_letters_digits_and_underscores(strategy, venue_id) -> None:
    with pytest.raises(venues.VenueError, match="lowercase letters, digits and underscores"):
        venues.load_profile(strategy, venue_id)


def test_profiles_are_listed_by_file_name(strategy: Path) -> None:
    (strategy / "venues" / "okx_eth.yaml").write_text("trading: {}\n", encoding="utf-8")
    (strategy / "venues" / "Not-One.yaml").write_text("trading: {}\n", encoding="utf-8")
    (strategy / "venues" / "notes.md").write_text("", encoding="utf-8")
    assert venues.venue_ids(strategy) == ["okx_eth", "sodex"]
    assert venues.venue_ids(strategy.parent) == []


# run.yaml


def test_the_run_slice_falls_back_to_the_top_level_but_never_for_the_venue_block() -> None:
    settings = yaml.safe_load(RUN)
    settings["venue"] = {"region": "eea"}
    sliced = venues.run_slice(settings, "demo", "sodex", "sodex")
    assert sliced == {
        "credential_id": "sodex-demo-sodex",
        "sandbox": {"starting_balances": ["10000 USDT"]},
        "risk_config": {"max_total_notional": 1000},
    }


def test_a_profile_block_in_run_yaml_wins_over_the_top_level() -> None:
    settings = yaml.safe_load(RUN)
    settings["venues"] = {
        "sodex": {
            "credential_id": "sodex-demo",
            "sandbox": {"starting_balances": ["10000 vUSDC"]},
            "venue": {"settlement_currency": "vUSDC"},
        }
    }
    sliced = venues.run_slice(settings, "demo", "sodex", "sodex")
    assert sliced == {
        "credential_id": "sodex-demo",
        "sandbox": {"starting_balances": ["10000 vUSDC"]},
        "risk_config": {"max_total_notional": 1000},
        "venue": {"settlement_currency": "vUSDC"},
    }


def test_the_default_credential_names_the_exchange_the_strategy_and_the_profile() -> None:
    assert venues.default_credential_id("demo", "okx_perpetual", "okx_eth") == "okx-demo-okx_eth"


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("venues: 5\n", "mapping of profile id"),
        ("venues:\n  So-Dex: {}\n", "lowercase letters"),
        ("venues:\n  sodex: 3\n", "venues.sodex must be a mapping"),
        ("venues:\n  sodex:\n    surprise: 1\n", "unknown keys: surprise"),
        ("venues:\n  sodex:\n    credential_id: ''\n", "non-empty string"),
        ("venues:\n  sodex:\n    sandbox: 1\n", "venues.sodex.sandbox must be a mapping"),
        (
            "venues:\n  sodex:\n    credential_id: binance-demo\n",
            "reuses the default profile's credential",
        ),
        (
            "venues:\n  sodex:\n    credential_id: k\n  okx:\n    credential_id: k\n",
            "venues.okx and venues.sodex share the credential 'k'",
        ),
    ],
)
def test_a_malformed_venues_block_is_refused(block: str, message: str) -> None:
    settings = yaml.safe_load(RUN + block)
    with pytest.raises(venues.VenueError, match=message):
        venues.check_run_venues(settings, Path("run.yaml"))


# Rendering


def test_the_profile_is_rendered_as_a_directory_the_runner_reads_like_a_strategy(
    strategy: Path, tmp_path: Path
) -> None:
    rendered = venues.render_run_dir(tmp_path, strategy, "sodex")

    assert rendered == tmp_path / ".runner" / "strategies" / "sodex" / "demo"
    config = yaml.safe_load((rendered / "config.yaml").read_text(encoding="utf-8"))
    assert config["trading"]["connector"]["value"] == "sodex"
    assert config["trading"]["pairs"]["value"] == ["vBTC_vUSDC"]
    assert config["parameters"] == {"lookback": {"value": 20}}
    assert "Do not edit" in (rendered / "config.yaml").read_text(encoding="utf-8")
    run = yaml.safe_load((rendered / "run.yaml").read_text(encoding="utf-8"))
    assert run["credential_id"] == "sodex-demo-sodex"
    assert run["risk_config"] == {"max_total_notional": 1000}


def test_the_code_is_linked_back_to_the_source_by_a_relative_path(
    strategy: Path, tmp_path: Path
) -> None:
    rendered = venues.render_run_dir(tmp_path, strategy, "sodex")
    link = rendered / "refinement"

    assert link.is_symlink()
    assert not os.path.isabs(os.readlink(link)), "absolute paths would not resolve in a container"
    assert link.resolve() == (strategy / "refinement").resolve()
    assert (link / "nautilus" / "strategy.py").read_text() == "# code\n"
    assert not (rendered / "tests").exists() and not (rendered / "venues").exists()


def test_rendering_again_replaces_what_was_there(strategy: Path, tmp_path: Path) -> None:
    venues.render_run_dir(tmp_path, strategy, "sodex")
    (strategy / "venues" / "sodex.yaml").write_text(
        SODEX.replace("vBTC_vUSDC", "vETH_vUSDC"), encoding="utf-8"
    )
    rendered = venues.render_run_dir(tmp_path, strategy, "sodex")
    config = yaml.safe_load((rendered / "config.yaml").read_text(encoding="utf-8"))
    assert config["trading"]["pairs"]["value"] == ["vETH_vUSDC"]
    assert (rendered / "refinement").is_symlink()


def test_rendering_refuses_to_replace_a_real_directory_where_the_link_goes(
    strategy: Path, tmp_path: Path
) -> None:
    (tmp_path / ".runner" / "strategies" / "sodex" / "demo" / "refinement").mkdir(parents=True)
    with pytest.raises(venues.VenueError, match="not a link"):
        venues.render_run_dir(tmp_path, strategy, "sodex")


def test_two_profiles_render_apart(strategy: Path, tmp_path: Path) -> None:
    (strategy / "venues" / "okx.yaml").write_text(
        'trading:\n  connector:\n    value: "okx"\n', encoding="utf-8"
    )
    sodex = venues.render_run_dir(tmp_path, strategy, "sodex")
    okx = venues.render_run_dir(tmp_path, strategy, "okx")
    assert sodex != okx
    assert venues.connector_of(yaml.safe_load((okx / "config.yaml").read_text())) == "okx"


# The Makefile carries the profile through every name

RUNNER_MK = (Path(__file__).resolve().parents[1] / "tools" / "runner" / "runner.mk").read_text(
    encoding="utf-8"
)


def test_every_run_name_carries_the_profile() -> None:
    import re

    assert re.search(r"^VENUE_TAG = \$\(if \$\(VENUE\),-\$\(VENUE\)\)$", RUNNER_MK, re.M)
    assert re.search(r"^SPEC_ID = \$\(STRATEGY_NAME\)\$\(VENUE_TAG\)-\$\(MODE\)$", RUNNER_MK, re.M)
    assert re.search(r"^RUNNER_LABEL = local-\$\(STRATEGY_NAME\)\$\(VENUE_TAG\)$", RUNNER_MK, re.M)
    project = (
        r"custos-\$\(REPO_NAME\)-\$\(subst _,-,\$\(STRATEGY_NAME\)\$\(VENUE_TAG\)\)-\$\(MODE\)"
    )
    assert re.search(rf"^COMPOSE_PROJECT = {project}$", RUNNER_MK, re.M)
    # Every call of the spec tool about the strategy passes the profile on, however
    # many lines the call is written over.
    calls = re.findall(
        r"\$\(SPEC_TOOL\) (?:render|credential-id|container-path)(?:[^\n]*\\\n)*[^\n]*", RUNNER_MK
    )
    assert len(calls) == 3 and all("$(VENUE_ARG)" in call for call in calls), calls


def _dry_run(*variables: str) -> str:
    """What `make` would run for the steps that name a run, on the example strategy.

    `start` itself recurses into sub-makes, which `make -n` runs for real, so the
    steps it is made of are dry-run instead: rendering the spec, and stopping.
    """
    import subprocess

    root = Path(__file__).resolve().parents[1]
    out = ""
    for target in ("_render", "stop"):
        out += subprocess.run(
            ["make", "-n", target, "STRATEGY=examples/trend/sma_cross", *variables],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
    return out


def test_a_dry_run_with_a_profile_names_it_everywhere() -> None:
    out = _dry_run("VENUE=okx", "MODE=sandbox")
    assert "--venue okx" in out
    assert "custos-custos-strategy-template-sma-cross-okx-sandbox" in out
    assert "SPEC_ID=sma_cross-okx-sandbox" in out
    assert "RUNNER_LABEL=local-sma_cross-okx" in out
    assert "STRATEGY_CONTAINER_PATH=/opt/repo/.runner/strategies/okx/sma_cross" in out
    assert "VENUE=okx MODE=sandbox" in out, "the next steps name the profile"


def test_a_dry_run_without_a_profile_names_nothing_differently() -> None:
    out = _dry_run("MODE=sandbox")
    assert "--venue" not in out
    assert "custos-custos-strategy-template-sma-cross-sandbox" in out
    assert "SPEC_ID=sma_cross-sandbox" in out
    assert "STRATEGY_CONTAINER_PATH=/opt/repo/examples/trend/sma_cross" in out
