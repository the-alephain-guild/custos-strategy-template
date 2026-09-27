# sma_cross

Long when a fast moving average crosses above a slow one; out when it crosses back. A worked example, not a trading idea.

| | |
|---|---|
| Pair | BTC-USDT |
| Connector | binance_perpetual |
| Bar | 1-HOUR |
| Direction | long only |

## How it works

Enter long when the 10-bar simple moving average crosses above the 30-bar one;
exit when it crosses back below. Both periods are in `config.yaml`.

## Strengths and weaknesses

It is the smallest rule that exercises indicators, entries and exits, which is
all it is for. A bare crossover lags and is whipsawed in sideways markets.

## Performance

It loses money over the one period tested; see `backtests/README.md`.

## Where things are

- `insight/notes.md` — the market hypothesis, and what would prove it wrong
- `design.md` — signals, sizing, risk and validation, written down before the code
- `refinement/nautilus/strategy.py` — the implementation
- `config.yaml` — default parameters
- `run.yaml` — credential, exposure ceiling and exchange account settings for local runs
- `tests/` — tests for this strategy alone
- `backtests/` — backtest summaries worth keeping
