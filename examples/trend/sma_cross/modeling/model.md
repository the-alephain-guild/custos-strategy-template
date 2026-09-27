# sma_cross: model

## Inputs

Closing prices of 1-hour bars.

## Entry rule

Let `gap = SMA(fast) - SMA(slow)`. Enter long on the bar where `gap` turns from
`<= 0` to `> 0`.

## Exit rule

Exit on the bar where `gap` turns from `>= 0` to `< 0`. Stop loss and take profit
come from the toolkit defaults.

## Position size and risk

`config.yaml` has no `position` section, so each position is the toolkit default
of 10% of equity. The strategy holds at most one position, which makes its
largest exposure 10% of equity. On the `10000 USDT` sandbox account that is
`1000 USDT` at entry, and `run.yaml` sets `risk_config.max_total_notional` to
the same `1000`.

## Parameters and their plausible ranges

- `fast_period`: 5 to 20
- `slow_period`: 20 to 100, always longer than `fast_period`
