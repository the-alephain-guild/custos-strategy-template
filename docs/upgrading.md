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

## A strategy's skeleton

Each strategy records the template commit it was made from in its
`.copier-answers.yml`. After merging an update that changed
`templates/strategy/`, commit your own work first (copier refuses a directory with
uncommitted changes), then bring the change into a strategy with:

    uvx copier update strategies/trend/my_idea

Where you and the template changed the same lines, copier marks a conflict in the
file instead of overwriting your edit; resolve it and commit. Run the strategy's
tests afterwards with `make test`.
