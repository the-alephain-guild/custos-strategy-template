# sma_cross

Long when a fast moving average crosses above a slow one; out when it crosses back. A worked example, not a trading idea.

| | |
|---|---|
| Pair | BTC-USDT |
| Connector | binance_perpetual |
| Bar | 1-HOUR |

## Where things are

- `insight/notes.md` — the market hypothesis and why you believe it
- `modeling/model.md` — the rule written down precisely, before any code
- `refinement/nautilus/strategy.py` — the implementation
- `config.yaml` — default parameters
- `tests/` — tests for this strategy alone
- `backtests/` — backtest summaries worth keeping

## Rule

Enter long when the 10-bar simple moving average crosses above the 30-bar one;
exit when it crosses back below. Both periods are in `config.yaml`.
