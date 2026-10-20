# Taking updates from the template

## Signal and fill hierarchy from 0.17.0

New reports group actual fills under their parent signal. One signal can have
multiple child fills from partial executions or several orders; each retains its
own execution time and original record. Chart labels match the parent numbering.
Missing lineage is kept in an unlinked group. No strategy skeleton, signal schema
or execution rule changes are needed. Old report directories remain unchanged.

## Local report preview from 0.16.0

`make preview-backtest STRATEGY=trend/my_idea` serves the latest exported chart
over read-only loopback HTTP for a browser that does not accept local file URLs.
Use `REPORT=path/to/report.html` for a specific run and optional `PORT=` for a
fixed port. The foreground process stops with Ctrl+C. It does not change strategy
code or historical results; no strategy skeleton update is required.

## Offline backtest charts from 0.15.0

The backtest now writes a local HTML chart in addition to its existing summary.
Merge the template update to get the viewer and data exporter; no toolkit pin
change is needed. `CHART=0` retains execution without chart observation.

The strategy skeleton adds an optional `get_chart_snapshot(ctx, bar)` method for
current indicator values. Existing strategies can add it manually using
[backtesting.md](backtesting.md), or review the change during `copier update`.
The existing `get_indicator_history()` remains supported as explicitly unverified
historical display when there is no snapshot method. Trading decisions are not
changed by the skeleton update. Do not replace a strategy's own implementation
with the empty generated stub.

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

    uvx copier update --vcs-ref=HEAD strategies/trend/my_idea

`--vcs-ref=HEAD` matters in a fork: without it copier reads the template from the
tag that sorts highest, and a tag the fork made before it adopted the template
(a `v1.0` sorts above `v0.10.0`) has no `copier.yml`, so copier renders that whole
old commit into the strategy directory. `make new-strategy` passes the flag since
0.10.1.

Where you and the template changed the same lines, copier marks a conflict in the
file instead of overwriting your edit; resolve it and commit. Run the strategy's
tests afterwards with `make test`.

## Venue profiles and VENUE= from 0.11.0

A strategy can carry venue profiles, `venues/<id>.yaml`, each laying another
exchange over its `config.yaml`; `make add-venue` writes one, and every command
that names a run takes `VENUE=<id>` to use it. `run.yaml` gains an optional
`venues:` mapping with a block per profile. Nothing changes for a strategy
without profiles: `config.yaml` is read as before, and every run keeps the names
it had. The rules are in [exchanges.md](exchanges.md).

## Custos 0.8.0 from 0.14.0

The runner image is Custos 0.8.0, the strategy toolkit 0.1.0rc10 and
NautilusTrader 2.0.0rc5+sodex.3, and releases are published through the 0.8.0
workflow. Run `make setup` after merging. Three things follow:

- A 0.8.0 runner refuses a strategy release built on the previous engine. Before
  a runner moves to 0.8.0, raise the version of each strategy it runs and publish
  it again with `make release`, since a version is published once; whoever trusts
  your releases adds the v0.8.0 workflow tag ([releasing.md](releasing.md)).
- A SoDEX strategy starts only with `risk.trade.sl_tp_mode` set to `tick`, which
  has no stop loss ([exchanges.md](exchanges.md#stops-on-sodex)). Strategies made
  before 0.14.0 do not set it: write it in `config.yaml` when SoDEX is the
  strategy's own exchange, or in the SoDEX venue profile when it is not.
- A venue profile may now set `risk.trade.sl_tp_mode`, and no other risk setting.
