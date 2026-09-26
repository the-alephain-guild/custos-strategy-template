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

Either way, run `make setup` once to install the pinned strategy toolkit and
NautilusTrader, then `make verify`. After that, `make next` says where the
repository stands and what to run next, and every command ends by saying the same.

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

Getting started:

| Command | What it does |
|---|---|
| `make setup` | Download the pinned toolkit and install the environment |
| `make setup-dev` | Build `.venv-dev` from unreleased sources; add `TOOLCHAIN=dev` to other commands to use it ([docs/dev-toolchain.md](docs/dev-toolchain.md)) |
| `make next` | Say where the repository stands and the one command to run next |
| `make setup-runner` | Create this machine's local runner identity, once |
| `make setup-key STRATEGY=trend/my_idea MODE=testnet` | Seal the strategy's testnet key; sandbox seals its placeholder by itself |

Strategies:

| Command | What it does |
|---|---|
| `make new-strategy NAME=my_idea CATEGORY=trend` | Create a strategy from the template |
| `make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01` | Backtest on the exchange's public data; add `JSON=1` for the summary as JSON |
| `make test` | Tool tests, then each strategy's tests |

Running on a sandbox or testnet (add the same `MODE=` to each; left out, it is sandbox):

| Command | What it does |
|---|---|
| `make start STRATEGY=trend/my_idea MODE=sandbox` | Start the strategy on a local runner |
| `make status STRATEGY=trend/my_idea` | Its account, positions, open orders and fills; `JSON=1` for JSON |
| `make logs STRATEGY=trend/my_idea` | Follow its log |
| `make stop STRATEGY=trend/my_idea` | Stop it, letting it cancel its resting orders, and keep its logs and last report |
| `make smoke STRATEGY=trend/my_idea` | Start and stop it on a simulated engine that never reaches an exchange |

`make verify` runs every check, as CI does; `make help` lists every command by group.

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
