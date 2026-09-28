# Taking updates from the template

Two kinds of update reach a repository made from this template, and each has its
own command.

## The template's own files

Tools, scripts, docs, the example and the toolchain pins all belong to the
template, so they merge without touching your strategies:

    git fetch upstream
    git merge upstream/main
    make setup
    make verify

`upstream` is this template: a fork has it after `git remote add upstream
https://github.com/the-alephain-guild/custos-strategy-template.git`, and a
private copy made as the README describes already has it. Run `make setup`
after merging, since an update may pin a newer toolkit or NautilusTrader.

## Commands renamed in 0.5.0

The commands were renamed so that each says what it does; the old names are gone.

| Before 0.5.0 | From 0.5.0 |
|---|---|
| `make toolkit` | `make setup` |
| `make toolkit-dev` | `make setup-dev` |
| `make runner-init` | `make setup-runner` |
| `make runner-vault` | `make setup-key` |
| `make run` | `make start`, which returns once the strategy runs; follow its log with `make logs` |
| `make run-detached` | `make start` |
| `make run-report`, `make run-status` | `make status` |
| `make run-logs` | `make logs` |
| `make run-stop` | `make stop` |
| `make run-smoke` | `make smoke` |

`make next` is new: it says where the repository stands and what to run next.
Strategies made before 0.5.0 mention the old names in the comments of their
`run.yaml`; `uvx copier update` brings in the new wording, or edit the comments
by hand.

## Documents reshaped in 0.8.0

A strategy's documents changed shape in 0.8.0: `design.md` replaces
`modeling/model.md` and adds position size, risk, data, validation and live
trading to the rule, `insight/notes.md` asks what would prove the idea wrong, and
`README.md` became a one-page card. They can also be written in Chinese.

`uvx copier update` adds the new files and asks the new language question
(English unless you choose otherwise; `--data language=zh` answers it). Move what
you wrote in `modeling/model.md` into sections 2 and 6 of `design.md`, then delete
`model.md`.

## Prototypes moved into modeling/ in 0.9.0

0.9.0 gives a strategy a `modeling/` directory for what comes before the code: a
`README.md` saying what belongs there, a `prototype.py` that loads the strategy's
parameters outside any engine, and `analysis/` for notebooks and charts. `uvx
copier update` adds them. A `modeling/` kept from before 0.8.0 stays where it is;
only its `model.md` belongs in `design.md`.

## A fork's own targets live in local.mk from 0.10.0

The Makefile is the template's: a fork that edits it conflicts on every update.
0.10.0 reads an optional `local.mk` next to it, never shipped by the template, where
a fork adds its own targets and, by setting `VERIFY_CHECKS`, chooses which checks
`make verify` runs:

    # local.mk
    VERIFY_CHECKS = check-ownership check-publisher-pin lint test my-gate
    ##@ Mine
    my-gate:  ## A check only this repository runs
    	...

Targets under a `##@` heading in `local.mk` appear in `make help` like the
template's own, and the `#> usage:` lines above them feed `make help CMD=<command>`;
`make verify` refuses a listed command without them, as it does for the template's. The default list is every check CI runs; a fork that drops the
disclosure or public-surface checks takes on what they refused.

## A strategy's skeleton

Each strategy records the template commit it was made from in its
`.copier-answers.yml`. After merging an update that changed
`templates/strategy/`, commit your own work first (copier refuses a directory with
uncommitted changes), then bring the change into a strategy with:

    uvx copier update strategies/trend/my_idea

Where you and the template changed the same lines, copier marks a conflict in the
file instead of overwriting your edit; resolve it and commit. Run the strategy's
tests afterwards with `make test`.
