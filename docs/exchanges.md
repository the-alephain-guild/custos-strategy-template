# Exchanges

A strategy trades on one exchange and one market, chosen when it is created and
recorded as `trading.connector` in its `config.yaml`. Everything else follows
from that choice: where backtest data comes from, which venue the runner
connects to, what the exchange key looks like and which account settings
`run.yaml` carries.

## What each exchange needs

| Connector | Market | Pairs are written | Key | `venue:` in run.yaml |
|---|---|---|---|---|
| `binance_perpetual` | Binance USDⓈ-M perpetuals | `BTC-USDT` | key, secret | none |
| `binance` | Binance spot | `BTC-USDT` | key, secret | none |
| `okx_perpetual` | OKX perpetual swaps | `BTC-USDT` | key, secret, passphrase | `region`, `margin_mode` |
| `okx` | OKX spot | `BTC-USDT` | key, secret, passphrase | `region`, `margin_mode` |
| `sodex_perpetual` | SoDEX perpetuals | `BTC-USD` | key, secret | `settlement_currency`; for testnet also `wallet_address`, `sodex_account_id` |
| `sodex` | SoDEX spot | `vBTC_vUSDC` | key, secret | `settlement_currency`; for testnet also `wallet_address`, `sodex_account_id` |

Pairs are written the way the exchange lists them. SoDEX has no common
`BASE-QUOTE` form to translate from: its spot engine lists its own v-prefixed
tokens joined by an underscore, and its perpetuals are quoted in USD and settled
in vUSDC. A pair the exchange does not list does not fail loudly on the runner;
it subscribes to nothing. `make backtest` is the quickest check, because it
refuses a pair the exchange does not list.

Spot trading is limited to BTC and ETH as the base asset, because the runner
accounts for spot holdings only in those. OKX perpetuals must settle in USDT or
USDC. `make new-strategy` refuses a pair that breaks either rule.

### Account settings

OKX: `region` is `global`, `eea` or `us`, after the site the account is
registered on; `margin_mode` is `cross` or `isolated`. Both default to the first.

SoDEX: `settlement_currency` is required in every mode and is `vUSDC` for the
pairs listed here. Testnet also needs the wallet the API key was registered for
and that wallet's numeric account id on the engine the strategy trades: spot and
perpetuals are separate accounts at SoDEX.

### Bar sizes

Every exchange serves 1, 5, 15 and 30 minutes, 1 and 4 hours and 1 day. Binance
and OKX also serve 3 minutes, 2, 6 and 12 hours. SoDEX spot serves 3 minutes, 6
and 12 hours, and SoDEX perpetuals none of those. `make backtest` names the sizes
an exchange has when asked for one it lacks.

## One strategy, several exchanges

A strategy names its exchange in `config.yaml`, and that file is complete on its
own: it is what backtests, the runner and every tool read by default. To run the
same strategy on another exchange as well, give it a **venue profile**:

    make add-venue STRATEGY=trend/my_idea CONNECTOR=sodex PAIR=vBTC_vUSDC

This writes `venues/sodex.yaml` in the strategy's directory and a `venues.sodex`
block in its `run.yaml`, and refuses when the pair is not spelt the way the
exchange lists it or when a bar the strategy uses is one the exchange does not
serve. The profile's id is the connector's name unless `VENUE=<id>` names
another, so one exchange can carry two profiles with different pairs.

A profile is laid over `config.yaml`: mappings key by key, lists and scalars
whole. It may set only the sections that depend on the exchange -- `trading`,
`platforms`, `warmup` and `backtesting` -- and is refused if it sets anything
else. A strategy's parameters and risk are the same on every exchange; if they
would have to differ, that is another strategy.

    # venues/sodex.yaml
    trading:
      connector:
        value: "sodex"
      pairs:
        value: ["vBTC_vUSDC"]
      leverage:
        value: 1
      fees:
        taker:
          value: 0.0005

Every command that names a run takes `VENUE=<id>`; without it, the strategy runs
as `config.yaml` says, exactly as before:

    make backtest STRATEGY=trend/my_idea VENUE=sodex START=2025-01-01 END=2025-04-01
    make setup-key STRATEGY=trend/my_idea VENUE=sodex MODE=testnet
    make start    STRATEGY=trend/my_idea VENUE=sodex MODE=sandbox
    make status   STRATEGY=trend/my_idea VENUE=sodex
    make stop     STRATEGY=trend/my_idea VENUE=sodex

A profiled run is named after the profile everywhere -- its spec, its containers,
its state and its saved logs -- so the same strategy runs on two exchanges at
once, and each profile seals a key of its own, under the `credential_id` in its
`run.yaml` block (`<exchange>-<name>-<id>` unless written).

Before a start or a backtest, the profile is rendered into
`.runner/strategies/<id>/<name>/`: a complete `config.yaml` with the profile laid
over the strategy's, the profile's slice of `run.yaml`, and a link to the
strategy's code. The runner reads that directory the way it reads a strategy's
own, so what it runs is exactly what the rendered file says; it is rendered
afresh on every start and never edited by hand.

## Backtest data

`make backtest` downloads bars and trading rules from the exchange's public API;
no account is needed. They are cached under `.data/<connector>/`. Only closed bars
are cached, so a range ending now never stores a bar that is still moving.

Two exchanges report things differently, and the data tool converts them so a
backtest sees the same units everywhere:

- OKX sizes perpetual swaps in contracts. Step size, minimum size and volume are
  converted to the base asset, the unit a strategy sizes in.
- OKX aligns 6-hour, 12-hour and daily candles to Hong Kong time unless asked
  for UTC. They are requested in UTC, so a daily bar opens at midnight UTC as on
  the other exchanges.

A SoDEX perpetual is priced and settled in vUSDC in a backtest. It is named in
USD, but the simulated account has no USD/vUSDC rate to convert with.

## Spot markets and position pricing

A spot market publishes no mark price, and SoDEX spot publishes no order book
either. The runner values a spot position and balance at the mark if there is
one, then the middle of the book, then the last trade, and the strategy toolkit
subscribes a spot pair's trades for that reason. With no price of any kind the
runner still refuses to value the position and stops the deployment rather than
trade blind. Runners before 0.5.2 had no last-trade step and stopped a SoDEX
spot deployment on its first fill.

## Adding an exchange

The runner decides which exchanges exist. An exchange is added in this order:

1. **Custos** executes on it: a venue module in the runner, and the connector in
   the runner's list of supported venues for each trading mode.
2. **The strategy toolkit** maps the connector to its venue and derives its
   instrument ids, in `VENUE_MAP` and `instrument_id_str`.
3. **This template**, in four places:
   - `copier.yml`: the connector in the `connector` choices, its common pairs,
     and a rule for the pairs typed under "other";
   - `tools/data/`: a module beside `binance.py` with the same five names
     (`NAME`, `CONNECTORS`, `symbol_for`, `fetch_rules`, `fetch_klines`), listed
     in `SOURCES` in `tools/data/sources.py`;
   - `templates/strategy/run.yaml.jinja`: a `venue:` block, if the exchange has
     account settings;
   - `tools/runner/vault.sh`: any credential field beyond a key and a secret.

`tests/test_data_sources.py` fails when a connector offered by `copier.yml` has
no data source, or is unknown to the toolkit, so a half-finished addition does
not pass `make verify`. CI then creates a strategy for every connector and runs
its tests.

Polymarket is not supported: the runner cannot execute on it yet.
