# sma_cross: model

## Inputs

Closing prices of 1-hour bars.

## Entry rule

Let `gap = SMA(fast) - SMA(slow)`. Enter long on the bar where `gap` turns from
`<= 0` to `> 0`.

## Exit rule

Exit on the bar where `gap` turns from `>= 0` to `< 0`. Stop loss, take profit and
position size come from the toolkit defaults.

## Parameters and their plausible ranges

- `fast_period`: 5 to 20
- `slow_period`: 20 to 100, always longer than `fast_period`
