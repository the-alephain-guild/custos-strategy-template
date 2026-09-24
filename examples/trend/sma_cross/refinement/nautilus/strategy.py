"""SmaCross strategy for NautilusTrader.

Goes long when the fast simple moving average crosses above the slow one and
exits when it crosses back below. It exists to show a complete strategy built
from the template; it is not a trading idea.

Both averages are placed in the pair's indicators, so the framework waits for
them to fill before the first signal, and history warmup fills them at start.
"""

from __future__ import annotations

from typing import Unpack, cast

import msgspec
from custos_toolkit.config import ConfigWrapper
from custos_toolkit.signals import Signal
from custos_toolkit_nautilus.adapter import (
    NautilusTradingStrategy,
    NautilusTradingStrategyConfig,
    PairContext,
    register_strategy,
)
from custos_toolkit_nautilus.adapter.trading_config import NautilusBaseConfigSections
from nautilus_trader.indicators import SimpleMovingAverage
from nautilus_trader.model import Bar

STRATEGY_NAME = "sma_cross"


class SmaCrossParameters(msgspec.Struct, frozen=True):
    """Values read from the `parameters` section of config.yaml."""

    fast_period: int = 10
    slow_period: int = 30


def _positive_int(params: dict, key: str, default: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"parameters.{key} must be a positive integer")
    return value


def build_parameters(config_wrapper: ConfigWrapper) -> SmaCrossParameters:
    params = config_wrapper.parameters or {}
    fast = _positive_int(params, "fast_period", 10)
    slow = _positive_int(params, "slow_period", 30)
    if fast >= slow:
        raise ValueError("parameters.fast_period must be shorter than parameters.slow_period")
    return SmaCrossParameters(fast_period=fast, slow_period=slow)


class SmaCrossConfig(NautilusTradingStrategyConfig):
    # NautilusTrader's StrategyConfig is not a msgspec Struct: the field is set
    # before super().__init__(), which freezes the instance.
    parameters: SmaCrossParameters

    def __init__(
        self,
        *,
        parameters: SmaCrossParameters | None = None,
        **kwargs: Unpack[NautilusBaseConfigSections],
    ) -> None:
        object.__setattr__(self, "parameters", parameters or SmaCrossParameters())
        super().__init__(**kwargs)


def crossed(previous_gap: float | None, gap: float) -> int:
    """+1 when the fast average moved above the slow one, -1 when below, else 0."""
    if previous_gap is None:
        return 0
    if previous_gap <= 0 < gap:
        return 1
    if previous_gap >= 0 > gap:
        return -1
    return 0


class SmaCrossStrategy(NautilusTradingStrategy):
    def on_strategy_start(self) -> None:
        params = cast(SmaCrossConfig, self.config).parameters
        self._previous_gap: dict[str, float] = {}
        for pair in self.config.trading.pairs:
            ctx = self._get_context(pair)
            if ctx is None:
                raise RuntimeError(f"strategy context is absent for pair: {pair}")
            fast = SimpleMovingAverage(params.fast_period)
            slow = SimpleMovingAverage(params.slow_period)
            self.register_indicator_for_bars(ctx.bar_type, fast)
            self.register_indicator_for_bars(ctx.bar_type, slow)
            ctx.indicators["sma_fast"] = fast
            ctx.indicators["sma_slow"] = slow
        self.log.info(
            f"{STRATEGY_NAME} started: fast={params.fast_period}, slow={params.slow_period}"
        )

    def on_strategy_stop(self) -> None:
        # Open orders are already cancelled by the framework when this runs.
        self.log.info(f"{STRATEGY_NAME} stopped")

    def calculate_signal(self, ctx: PairContext, bar: Bar) -> Signal:
        gap = ctx.indicators["sma_fast"].value - ctx.indicators["sma_slow"].value
        direction = crossed(self._previous_gap.get(ctx.pair), gap)
        self._previous_gap[ctx.pair] = gap
        if direction > 0:
            return Signal.enter_long(price=bar.close, pair=ctx.pair)
        if direction < 0:
            return Signal.exit_long(price=bar.close, pair=ctx.pair)
        return Signal.neutral(price=bar.close, pair=ctx.pair)

    def get_indicator_history(self) -> dict[str, object]:
        # Indicator values recorded for plotting after a backtest.
        return {}


register_strategy(
    name=STRATEGY_NAME,
    strategy_class=SmaCrossStrategy,
    config_class=SmaCrossConfig,
    parameters_builder=build_parameters,
)
