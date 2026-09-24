# Running a strategy locally

`make run` runs one strategy on a Custos runner in Docker, next to a NATS
server, against the exchange's sandbox or testnet. The repository is mounted
read-only into the runner, so an edited strategy takes effect the next time it
starts; nothing is built or published.

## What you need

- Docker.
- `age` and `age-keygen` on your `PATH` (`brew install age` on macOS).
- The runner image named under `[runner]` in `toolchain.lock.toml`, available to
  Docker. Until Custos publishes a release image, build it from the Custos
  repository at the revision the lock names, under the tag it names:

      git -C /path/to/custos checkout <revision from toolchain.lock.toml>
      make -C /path/to/custos docker-build-local-v030 LOCAL_IMAGE=<image from toolchain.lock.toml>

  `make runner-init` and `make run` check that the image exists, reports the
  expected package version, and accepts the version of deployment spec this
  repository renders. The runner reports that with `arx-runner deployment schema`;
  an image too old to have the command, or one that accepts another version, is
  refused with the version each side expects.

Runner state lives in `.runner/`, which is not committed: the machine's age key,
its runner identity, the sealed exchange keys, the last rendered deployment and
saved logs.

## Once per machine

    make runner-init

Creates the age key and a runner identity inside the image, because the identity
records paths as the runner sees them. It refuses to replace an existing identity:
the identity and its machine credential are bound to each other, so delete both
on purpose if you need a new one.

## Once per strategy

    make runner-vault STRATEGY=trend/my_idea

Seals the exchange key for the credential named in the strategy's `run.yaml`.
The secret is typed at a hidden prompt, or read from an environment variable you
name with `API_SECRET_ENV=...`; it never appears on a command line. An OKX key
also has a passphrase, asked for the same way or read from `API_PASSPHRASE_ENV=...`.
Sandbox mode does not send the key to the exchange, so placeholder values work
there. Testnet needs a real key from the exchange's test environment: Binance's
futures testnet, OKX's demo trading, or SoDEX testnet. See
[exchanges.md](exchanges.md).

## Every day

    make run STRATEGY=trend/my_idea MODE=sandbox     # or MODE=testnet
    make run-logs STRATEGY=trend/my_idea
    make run-stop STRATEGY=trend/my_idea

Before starting, `make run` checks the identity and the sealed key, renders the
deployment from `config.yaml` and `run.yaml`, and has the runner validate it.
It then clears the deployment the runner remembers from its last start and waits
for that to land: a changed strategy published on top of a stale record is
refused. `run-stop` saves the logs to `.runner/logs/` after the containers stop
and before they are removed, so the shutdown is kept.

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
