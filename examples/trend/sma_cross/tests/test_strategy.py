from pathlib import Path

import pytest
from custos_toolkit.config import ConfigWrapper, load_config
from refinement.nautilus.strategy import STRATEGY_NAME, build_parameters, crossed

STRATEGY_DIR = Path(__file__).resolve().parents[1]


def _config() -> ConfigWrapper:
    return ConfigWrapper(load_config(STRATEGY_DIR / "config.yaml").raw)


def test_parameters_come_from_config_yaml() -> None:
    params = build_parameters(_config())
    assert (params.fast_period, params.slow_period) == (10, 30)


def test_a_fast_period_not_shorter_than_the_slow_one_is_refused() -> None:
    config = _config().raw
    config["parameters"]["fast_period"] = {"value": 30, "type": "integer"}
    with pytest.raises(ValueError, match="shorter"):
        build_parameters(ConfigWrapper(config))


@pytest.mark.parametrize(
    ("previous", "gap", "expected"),
    [
        (None, 1.0, 0),  # no previous bar, so no cross
        (-1.0, 1.0, 1),  # fast moved above slow
        (0.0, 0.5, 1),  # from touching to above counts as a cross up
        (1.0, -1.0, -1),  # fast moved below slow
        (1.0, 2.0, 0),  # still above
        (-2.0, -1.0, 0),  # still below
    ],
)
def test_crosses_are_detected_once(previous: float | None, gap: float, expected: int) -> None:
    assert crossed(previous, gap) == expected


def test_the_registered_strategy_can_be_built_from_config_yaml() -> None:
    from custos_toolkit_nautilus.adapter import create_strategy

    strategy = create_strategy(STRATEGY_NAME, config_wrapper=_config())
    assert strategy.config.parameters.slow_period == 30
