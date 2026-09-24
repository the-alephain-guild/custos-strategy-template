# sma_cross: backtests

## 2025 Q1, BTC-USDT perpetual, 1-hour bars

Reproduce with:

    make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-04-01

| | |
|---|---|
| Bars | 2160 |
| Starting balance | 10000 USDT |
| Final balance | 9792.26 USDT |
| PnL | -2.08% |
| Positions | 37 |
| Win rate | 18.9% |
| Sharpe (252 days) | -2.14 |

Default parameters (fast 10, slow 30), default toolkit fees and position sizing.
The strategy loses money over this period. That is expected of a bare moving
average crossover and is not what the example is for: it shows how a strategy
is built, tested, backtested and run, not what to trade.
