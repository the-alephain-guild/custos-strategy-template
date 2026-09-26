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

- **`[custos] source`** and **`revision`**: a Custos repository and the commit
  in it to run. The commit is checked out on its own under
  `.toolchain/dev-sources/`, and the toolkit wheels and the runner image are built
  from that checkout. The repository's working directory is never built from, so
  you, or anyone else working in it, can keep uncommitted changes there and make
  new commits without blocking a build or making this one out of date. A commit
  that exists only locally is fine; uncommitted changes cannot be run, because
  every build here is of a commit. To try a newer Custos, change `revision`.
- **`[custos] runner_image`**: the tag for the runner image built from that
  commit, one of its own so the pinned image is left alone. It is labelled with
  the commit it was built from, and not rebuilt while that still matches.
- **`[nautilus_trader] wheel`**: a wheel built from a checkout of the
  NautilusTrader fork, with the fork's own script, which also records where the
  wheel came from:

      scripts/guild/build-local-wheel.sh ~/.cache/alephain-wheels/nautilus_trader

Then build the environment:

    make toolkit-dev

This checks out the Custos commit, builds the runner image and the toolkit from
it, and creates `.venv-dev/`. It leaves `.venv`, `pyproject.toml` and `uv.lock`
as they are, and fails if any of them changed while it ran.

## Use it

Add `TOOLCHAIN=dev` to the commands:

    make test TOOLCHAIN=dev
    make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01 TOOLCHAIN=dev
    make run STRATEGY=trend/my_idea MODE=sandbox TOOLCHAIN=dev

Each of them first says where every part comes from:

    Dev toolchain
     toolkit          local ~/src/custos @ 4f3c736f0d60
     nautilus_trader  local 2.0.0rc5+sodex.2 built from ~/src/nautilus_trader @ 73936dd56a01
     runner image     local custos-runner:dev (its NautilusTrader is the released one)

and warns when what is built no longer matches what you asked for: a Custos
revision in `toolchain.local.toml` other than the one built, or a NautilusTrader
wheel that changed after it was installed or whose checkout has moved on. New
commits in the Custos repository are not such a change; the revision is pinned
on purpose. Rebuild with `make toolkit-dev`, or the wheel first if it is what
changed.

A backtest summary records the toolchain it ran on under `toolchain`.
`make run TOOLCHAIN=dev` refuses an image built from a Custos commit other than
the one `toolchain.local.toml` names.

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
