# Running a strategy locally

`make run` runs one strategy on a Custos runner in Docker, next to a NATS
server, against the exchange's sandbox or testnet. The repository is mounted
read-only into the runner, so an edited strategy takes effect the next time it
starts; nothing is built or published.

## What you need

- Docker.
- `age` and `age-keygen` on your `PATH` (`brew install age` on macOS).
- Network access to GitHub Container Registry the first time. The runner is the
  published Custos release named under `[runner]` in `toolchain.lock.toml`,
  pinned by digest; `make runner-init` and `make run` pull it when Docker does
  not have it yet, and no account is needed.

  Before starting anything they check that the image reports the expected
  package version and accepts the version of deployment spec this repository
  renders. The runner reports that with `arx-runner deployment schema`; an image
  too old to have the command, or one that accepts another version, is refused
  with the version each side expects. To run on a Custos build that is not
  released yet, see [dev-toolchain.md](dev-toolchain.md).

Runner state lives in `.runner/`, which is not committed: the machine's age key,
its runner identity, the sealed exchange keys, the last rendered deployment and
saved logs. Each run -- one strategy in one mode -- also keeps its own state in
`.runner/state/`, so several can run at once without reading each other's
readiness. Their containers are named after the repository, the strategy and
the mode, so runs started from other repositories on the same machine never
collide with these.

## Once per machine

    make runner-init

Creates the age key and a runner identity inside the image, because the identity
records paths as the runner sees them. It refuses to replace an existing identity:
the identity and its machine credential are bound to each other, so delete both
on purpose if you need a new one.

## Once per strategy, for testnet

Each mode runs with a key of its own, sealed under the `credential_id` in the
strategy's `run.yaml` with the mode appended, so sealing one never replaces the
other. Local runs are sandbox or testnet, never live, so a live account's key is
never asked for or used here.

- **Sandbox** fills orders on this machine and never sends a key to the
  exchange. `make run MODE=sandbox` seals a placeholder as `<credential_id>-sandbox`
  the first time; there is nothing to do.
- **Testnet** trades on the exchange's own test environment, whose keys are
  separate from a live account's: the Binance spot or USDⓈ-M futures testnet,
  OKX demo trading, or the SoDEX testnet. Seal one as `<credential_id>-testnet`:

      make runner-vault STRATEGY=trend/my_idea MODE=testnet

  It names the test environment for the exchange `trading.connector` in
  `config.yaml` names, then asks for that key. The secret is typed at a hidden
  prompt, or read from an environment variable you name with
  `API_SECRET_ENV=...`; it never appears on a command line. An OKX key also has a
  passphrase, asked for the same way or read from `API_PASSPHRASE_ENV=...`. Give
  the key trading permission only. See [exchanges.md](exchanges.md).

A sealed key is not overwritten. To seal another for the same mode, add
`REPLACE=1`; the old one is removed only after the new one has been read.

## Every day

    make run STRATEGY=trend/my_idea MODE=sandbox     # or MODE=testnet
    make run-report STRATEGY=trend/my_idea
    make run-logs STRATEGY=trend/my_idea
    make run-stop STRATEGY=trend/my_idea

Add the same `MODE=` to `run-report`, `run-logs` and `run-stop` as to `run`;
left out, it is sandbox.

Before starting, `make run` checks the identity and the sealed key, renders the
deployment from `config.yaml` and `run.yaml`, and has the runner validate it.
It then clears the deployment the runner remembers from its last start and waits
for that to land: a changed strategy published on top of a stale record is
refused. `run-stop` saves the logs to `.runner/logs/` after the containers stop
and before they are removed, so the shutdown is kept.

Stopping gives the strategy time to clean up: it cancels the orders it left
resting, keeps the protective orders on a position it holds, and the runner
waits for the exchange to confirm that before it exits. That can take up to 90
seconds on a testnet. `run-stop` then says whether every strategy confirmed it
stopped; if not, check the exchange for orders it left behind.

## What a running strategy holds

`make run-report STRATEGY=trend/my_idea` shows, for the run that is up now:

- the account: equity, its change since the run's first report, the peak,
  drawdown and open notional;
- open positions, with their unrealized result;
- open orders;
- the latest fills, and the run's realized result and fees so far.

The runner reports every 10 seconds, starting about 10 seconds after the
strategy starts; fills and closed positions are reported as they happen. Add
`JSON=1` for the same as JSON. For a view that refreshes, run it under
`watch -n 10`.

What the runner reported lasts only as long as the run: `run-stop` saves the
last report next to the logs, as `.report.json`, before it stops the run.

The pinned runner release does not report any of this yet. Until a release that
does is pinned, run on a Custos build that does, with `TOOLCHAIN=dev` (see
[dev-toolchain.md](dev-toolchain.md)); `run-report` says so when the runner
cannot answer.

`make run-smoke STRATEGY=trend/my_idea` starts and stops the strategy on a
simulated engine that never contacts an exchange: a quick check that the lane
works.

## run.yaml

    credential_id: binance-my_idea
    sandbox:
      starting_balances: ["10000 USDT"]
    risk_config:
      max_total_notional: 1000

A strategy on OKX or SoDEX also has a `venue:` block with that exchange's account
settings; [exchanges.md](exchanges.md) lists them.

`max_total_notional` is enforced by the runner itself, on top of the strategy's
risk settings. Left out, the runner applies its strictest default of 200, which
an ordinary position exceeds on its first trade. Set it somewhat above the
largest exposure your position sizing can reach.

## When nothing seems to happen

A strategy decides only when a bar closes, and only once its indicators have
enough history. Generated strategies load that history at start
(`warmup.mode: warmup` in `config.yaml`), so the first signal comes with the
first bar that closes after the start: within an hour on hourly bars. With
`warmup.mode: none` the strategy waits for every indicator period to pass live,
and logs nothing while it waits.
