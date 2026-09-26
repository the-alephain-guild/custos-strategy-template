# Quickstart

This walks the included example, `examples/trend/sma_cross`, from a fresh copy
of the repository to a running sandbox, then creates a strategy of your own.

## 1. Install

    make setup
    make verify

`make setup` downloads the strategy toolkit by the digest pinned in
`toolchain.lock.toml`, checks every byte against it, and installs it with
NautilusTrader into `.venv/`. On an unsupported platform it stops and says so.

Every command ends by saying what to run next. To see where the repository
stands at any point, and the one command to run next, use:

    make next

## 2. Backtest the example

    make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-04-01

The first run downloads hourly BTC-USDT perpetual klines and the symbol's trading
rules from Binance's public API into `.data/`; later runs reuse them. A strategy
on OKX or SoDEX downloads from that exchange instead; no account is needed for
any of them. The summary
is printed and written to `examples/trend/sma_cross/backtests/output/`. For this
range it reports 2160 bars and a loss of about 2%: the example shows the workflow,
it is not a trading idea.

## 3. Run it on a local runner

Running needs Docker, `age`, and the Custos runner image named in
`toolchain.lock.toml`; see [local-run.md](local-run.md).

    make setup-runner
    make start STRATEGY=examples/trend/sma_cross MODE=sandbox

`setup-runner` creates this machine's runner identity, once. Sandbox never sends
a key to the exchange, so `start` seals a placeholder for it the first time; a
testnet run needs a key from the exchange's test environment, sealed with
`make setup-key STRATEGY=... MODE=testnet`. `start` starts the runner and
returns once it reports the strategy running. The strategy loads recent history
as it starts, so its first signal comes when the first live bar closes. Look at
it, follow its log, and stop it with:

    make status STRATEGY=examples/trend/sma_cross
    make logs STRATEGY=examples/trend/sma_cross
    make stop STRATEGY=examples/trend/sma_cross

## 4. Create your own strategy

    make new-strategy NAME=my_idea CATEGORY=trend

This asks for a description, the engine (NautilusTrader in Python is the one Custos
runs today), the exchange and market, the pair and the bar, then writes
`strategies/trend/my_idea/` and registers it in `registry/strategies.toml`.
The exchanges are Binance, OKX and SoDEX, each with spot and perpetuals. The pair
question lists the common pairs for the market you chose, in that exchange's own
spelling, and "other" lets you type any pair it lists; see
[exchanges.md](exchanges.md) for what each exchange needs:

    config.yaml                      parameters, pair and bar
    run.yaml                         credential, exposure ceiling and exchange
                                     account settings for local runs
    insight/notes.md                 the hypothesis
    modeling/model.md                the rule, written down before the code
    refinement/nautilus/strategy.py  the implementation
    tests/                           its own tests
    backtests/                       summaries worth keeping

The generated strategy builds, registers and passes its tests, and never trades:
`calculate_signal` returns a neutral signal until you write the rule. The example's
`strategy.py` shows a complete one.

    make test
    make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01

## 5. Bring in a strategy you already have

Create it with `make new-strategy` first, so it gets the directory, `run.yaml` and
registry entry, then replace the generated `refinement/nautilus/strategy.py` and
`config.yaml` with your own. Three things decide whether it will load:

- It must call `register_strategy(name=...)` with the directory's name. The runner
  and the backtest look it up by that name.
- It must not rely on anything outside the repository, such as a local `shared`
  package; the strategy toolkit provides the common parts.
- A `config.yaml` written for another setup may set `warmup.mode` to `none`, and
  the strategy will then wait silently for its indicators to fill before its first
  signal. Set it to `warmup`.

Drop anything written only for another deployment system, such as an extra
factory function at the end of the module. `make test` runs the generated test,
which checks exactly the first point: the strategy registers under its directory
name and builds from its `config.yaml`.
