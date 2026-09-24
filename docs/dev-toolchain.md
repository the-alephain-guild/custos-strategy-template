# The dev toolchain

Everything in this repository normally runs on released builds: the strategy
toolkit and NautilusTrader pinned in `toolchain.lock.toml` and `pyproject.toml`,
and the runner image named there. That is what CI runs and what `make verify`
checks.

The dev toolchain lets you run your strategies on builds that are not released
yet: a strategy toolkit built from a Custos checkout, a NautilusTrader wheel built
on your machine, and a runner image built from Custos. Use it to try a fix before
it is published, and to find out whether an upcoming release breaks your
strategies while there is still time to change it.

## What it replaces, and what it cannot

| | `make test`, `make backtest` | `make run` (local runner) |
|---|---|---|
| Strategy toolkit | from your Custos checkout | from the image |
| NautilusTrader | your local wheel | **the released one**, inside the image |
| Runner | none | your image built from Custos |

The local runner runs in a Linux container, and a wheel built on macOS cannot go
into it. The image installs the NautilusTrader its own lock names, so a
NautilusTrader change that matters only while a strategy runs, such as how an
exchange adapter loads history, cannot be tried on the runner before it is
released. Tests and backtests do use your wheel.

## Set it up

Copy the example and keep the entries you want; anything you leave out stays
pinned:

    cp toolchain.local.toml.example toolchain.local.toml

`toolchain.local.toml` names paths on your machine and is not committed.

- **`[custos] source`**: a Custos checkout. The two toolkit wheels are built
  from it.
- **`[custos] runner_image`**: an image built from that checkout, under a tag of
  its own so the pinned image is left alone:

      make -C /path/to/custos docker-build-local-v030 LOCAL_IMAGE=custos-runner:dev

  That target refuses a checkout with uncommitted changes, and labels the image
  with the commit it was built from.
- **`[nautilus_trader] wheel`**: a wheel built from a checkout of the
  NautilusTrader fork, with the fork's own script, which also records where the
  wheel came from:

      scripts/guild/build-local-wheel.sh ~/.cache/alephain-wheels/nautilus_trader

Then build the environment:

    make toolkit-dev

This creates `.venv-dev/`. It leaves `.venv`, `pyproject.toml` and `uv.lock` as
they are, and fails if any of them changed while it ran.

## Use it

Add `TOOLCHAIN=dev` to the commands:

    make test TOOLCHAIN=dev
    make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01 TOOLCHAIN=dev
    make run STRATEGY=trend/my_idea MODE=sandbox TOOLCHAIN=dev

Each of them first says where every part comes from:

    [toolchain] dev
      toolkit          local /path/to/custos @ 7cb4604a1b2c
      nautilus_trader  local 2.0.0rc5+sodex.2 built from /path/to/nautilus_trader @ 73936dd56a01
      runner image     local custos-runner:dev (its NautilusTrader is the released one)

and warns when a source has moved on since the environment was built, so a
result is never taken from a build other than the one you think. Rebuild with
`make toolkit-dev`, or the wheel or image first if they are what changed.

A backtest summary records the toolchain it ran on under `toolchain`.
`make run TOOLCHAIN=dev` refuses an image built from a different Custos commit
than the checkout names.

`make verify` refuses `TOOLCHAIN=dev`: it is the check CI runs, on released
builds only.

## Before a release

The order that keeps a release honest:

1. Your strategies pass `make test`, `make backtest` and a sandbox run with
   `TOOLCHAIN=dev`.
2. NautilusTrader and Custos are released.
3. This template pins the released versions, and you merge it.
4. Run the same checks again **without** `TOOLCHAIN=dev`. A wheel built on your
   machine and the one published are not the same bytes, so passing on the first
   says little for certain about the second.
