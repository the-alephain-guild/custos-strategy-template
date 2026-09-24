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

## Known limits of runner 0.3.0

- **SoDEX history does not load when a strategy starts.** The NautilusTrader
  build in this runner predates a fix to how SoDEX historical bars are
  timestamped, and the history a strategy asks for at start arrives empty. The
  runner logs one warning, `Received empty Bar response`, and the strategy then
  waits for its indicators to fill from live bars: at 1-hour bars, as many hours
  as its longest indicator period, with nothing in the log meanwhile. Backtests
  are not affected; they read the exchange's data directly.
- **OKX strategies run on the runner's own toolkit.** The toolkit pinned in
  `toolchain.lock.toml` names OKX instruments differently from the runner
  (`BTCUSDT-PERP.OKX` rather than `BTC-USDT-SWAP.OKX`). Backtests and tests are
  consistent with themselves, and the runner uses the naming it trades with, so
  nothing breaks; it matters only to a strategy that writes an OKX instrument id
  out by hand. The next toolkit release aligns the two.

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
