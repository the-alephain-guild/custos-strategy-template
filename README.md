# custos-strategy-template

A starting point for your own repository of trading strategies that run on
[Custos](https://github.com/the-alephain-guild/custos), the self-hosted strategy
runner. Strategies are written in Python for NautilusTrader.

From a copy of this repository you can:

- create a strategy with one command, from a template that already builds and tests;
- backtest it on public market data from Binance, OKX or SoDEX;
- run it on a local Custos runner against the exchange's sandbox or testnet;
- keep taking improvements from this template without them touching your strategies.

Start with [docs/quickstart.md](docs/quickstart.md), which takes the included
example from a fresh copy to a running sandbox.
[docs/exchanges.md](docs/exchanges.md) lists the exchanges and what each needs.

## Before you start

| You need | For |
|---|---|
| [uv](https://docs.astral.sh/uv/) | everything; it provides Python 3.12 |
| macOS on Apple silicon, or Linux on x86_64 or aarch64 | NautilusTrader wheels exist for these platforms only |
| Docker | running a strategy locally |
| [age](https://github.com/FiloSottile/age) | sealing your exchange key for the local runner |

## Two ways to use it

**A public fork** is the quickest start, but everything you commit to it is
public, strategies included:

    gh repo fork the-alephain-guild/custos-strategy-template --clone

**A private copy** keeps your strategies private and can still take updates from
this template:

    git clone https://github.com/the-alephain-guild/custos-strategy-template.git my-strategies
    cd my-strategies
    git remote rename origin upstream
    gh repo create my-strategies --private --source . --push

Either way, run `make toolkit` once to install the pinned strategy toolkit and
NautilusTrader, then `make verify`.

## Layout

    strategies/          yours: one directory per strategy
    registry/            yours: the list of your strategies
    examples/            a worked example, maintained here
    templates/strategy/  what `make new-strategy` generates
    tools/               backtest, market data and local runner
    scripts/             the checks `make verify` runs
    docs/                guides

Everything outside `strategies/` and `registry/` belongs to this template.
[OWNERSHIP.toml](OWNERSHIP.toml) records the split, and a check enforces it in both
directions: updates from this template never touch your strategies, and pull
requests to it never carry them. That is what lets you merge updates without
conflicts; see [docs/upgrading.md](docs/upgrading.md).

## Commands

| Command | What it does |
|---|---|
| `make toolkit` | Download the pinned toolkit and install the environment |
| `make toolkit-dev` | Build `.venv-dev` from unreleased sources; add `TOOLCHAIN=dev` to other commands to use it ([docs/dev-toolchain.md](docs/dev-toolchain.md)) |
| `make new-strategy NAME=my_idea CATEGORY=trend` | Create a strategy from the template |
| `make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01` | Backtest on the exchange's public data |
| `make runner-init` | Create this machine's local runner identity, once |
| `make runner-vault STRATEGY=trend/my_idea` | Seal the strategy's exchange key |
| `make run STRATEGY=trend/my_idea MODE=sandbox` | Run the strategy on a local runner |
| `make run-stop STRATEGY=trend/my_idea` | Stop it, keeping its logs |
| `make test` | Tool tests, then each strategy's tests |
| `make verify` | Every check, as CI runs it |

`make help` lists the rest.

## What this version does not do

- **Live trading.** A locally created runner identity is refused for live mode;
  live needs a runner enrolled with a deployment service.
- **Publishing signed strategy artifacts.** Strategies run from their source
  directory. Signed publication is planned for a later version.
- **Strategies in Rust.** Custos runs NautilusTrader strategies written in Python
  only. The Rust engine is listed when you create a strategy, and choosing it is
  refused.

## Contributing

Improvements to the template, the tools and the docs are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
