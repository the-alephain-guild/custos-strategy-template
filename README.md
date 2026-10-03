# custos-strategy-template

A starting point for your own repository of trading strategies that run on
[Custos](https://github.com/the-alephain-guild/custos), the self-hosted strategy
runner. Strategies are written in Python for NautilusTrader.

From a copy of this repository you can:

- create a strategy with one command, from a template that already builds and tests;
- backtest it on public market data from Binance, OKX or SoDEX;
- run it on a local Custos runner against the exchange's sandbox or testnet;
- give it a venue profile and run the same strategy on another exchange too;
- publish a signed release of it and deploy that release to a runner through ARX;
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
| [GitHub CLI](https://cli.github.com/) (`gh`) | publishing a release, and reading it back to deploy it |
| An ARX account with an authenticator app | deploying a release through ARX |

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

A private copy that needs targets of its own puts them in `local.mk`, which the
template never ships; `VERIFY_CHECKS` there chooses what `make verify` runs
(`docs/upgrading.md`).

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
| `make add-venue STRATEGY=trend/my_idea CONNECTOR=sodex PAIR=vBTC_vUSDC` | Give it a venue profile: the same strategy on another exchange ([docs/exchanges.md](docs/exchanges.md)) |
| `make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01` | Backtest on the exchange's public data; add `JSON=1` for the summary as JSON |
| `make test` | Tool tests, then each strategy's tests |

Running on a sandbox or testnet (add the same `MODE=` to each; left out, it is
sandbox; `VENUE=<id>` runs a venue profile instead of `config.yaml`'s exchange):

| Command | What it does |
|---|---|
| `make start STRATEGY=trend/my_idea MODE=sandbox` | Start the strategy on a local runner |
| `make status STRATEGY=trend/my_idea` | Its account, positions, open orders and fills; `REFRESH=10` redraws every 10 seconds, `JSON=1` for JSON |
| `make logs STRATEGY=trend/my_idea` | Follow its log |
| `make stop STRATEGY=trend/my_idea` | Stop it, letting it cancel its resting orders, and keep its logs and last report |
| `make smoke STRATEGY=trend/my_idea` | Start and stop it on a simulated engine that never reaches an exchange |

Publishing:

| Command | What it does |
|---|---|
| `make release STRATEGY=trend/my_idea` | Publish a signed release of the strategy's version through GitHub Actions, from the commit you have pushed ([docs/releasing.md](docs/releasing.md)) |

Deploying through ARX ([docs/deploying.md](docs/deploying.md)):

| Command | What it does |
|---|---|
| `make arx-login ARX_URL=https://arx.example.com` | Sign in to ARX with your email, password and authenticator code, and keep the session on this machine |
| `make arx-status` | Show the kept session: organisation, roles and when it ends; then each deployment made from this directory that is not stopped, with the state ARX wants it in, then what its runner said: started it, refused the start, or not answered yet |
| `make arx-logout` | End the session on ARX and remove it from this machine |
| `make arx-evidence STRATEGY=trend/my_idea` | Read the released version back from its package by digest and check it against its receipt, as ARX will need it |
| `make enroll-runner RUNNER=<id> NAME="Box 1" SCOPE=3` | Issue a runner's enrollment token into a file only you can read; once the runner has enrolled with it on its own machine, run it again to name the runner |
| `make authorize-runner-transport RUNNER=<id> MODE=sandbox` | Authorise the runner's message-transport credential for one mode and print the intent id the runner completes it with |
| `make runner-safety-policy ACTION=submit\|approve\|activate RUNNER=<id> MODE=sandbox` | Ask for the cap on what a runner may hold; a second person, holding `FINANCE`, approves and activates it |
| `make deploy-preview STRATEGY=trend/my_idea MODE=sandbox RUNNER=<id>` | Build the DeploymentSpec the release would be deployed with, from the release's trading scope and the strategy's `deploy.yaml`, and show it with its digests and the strategy's product, or the one that would be created; only reads from ARX |
| `make deploy STRATEGY=trend/my_idea MODE=sandbox RUNNER=<id>` | Deploy the release: make the strategy definition and the release in ARX if they are missing, find the strategy's product in the mode (or create it with one authenticator code and stop until it has capital and is active), show the spec and create it with one authenticator code, which starts its first instance, then check that the runner started it: a new instance ends at once with the runner-side steps due (exit status 3), and running it again waits up to `TIMEOUT=` seconds (180 by default) for the runner; a rejected start, no answer in time, or an ARX that does not report the runner's answer ends with an error (exit status 1); safe to run again, and keeps a receipt in `.deployments/` |
| `make deploy-stop STRATEGY=trend/my_idea MODE=sandbox` | Stop the instance a deployment receipt names, with one authenticator code; changing release is stop, then deploy |

`make verify` runs every check, as CI does; `make help` lists every command by group,
and `make help CMD=start` explains one in full: its variables and their defaults,
examples, and what to run after it.

## What this version does not do

- **Live trading.** A locally created runner identity is refused for live mode,
  and `make deploy` deploys to sandbox or testnet only; a live deployment comes
  only from a promotion approved in ARX.
- **Capital and activation.** `make deploy` creates a strategy's product the
  first time it is deployed in a mode, but puts no capital into it and does
  not activate it: that is done in the ARX console, by an investor, a second
  person who approves, and someone who activates it (docs/deploying.md, The
  product). Until then it stops before deploying.
- **The runner's side.** Enrolling a runner, completing its transport
  credential, storing its exchange key and publishing its capability happen on
  the runner's own machine with the runner's `arx-runner` commands. So does
  finishing a deployment: the new instance is added to the runner's capability
  bindings, the capability is published again and the runner is restarted.
  The deployment receipt lists these steps. Until they are done the runner
  cannot answer the start, so the `make deploy` that creates an instance ends
  at once with them (exit status 3); run it again once they are done.
- **Switching release in one step.** Changing release is `make deploy-stop`,
  then `make deploy`; the two never run side by side.
- **Strategies in Rust.** Custos runs NautilusTrader strategies written in Python
  only. The Rust engine is listed when you create a strategy, and choosing it is
  refused.

## Contributing

Improvements to the template, the tools and the docs are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
