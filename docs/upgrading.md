# Taking updates from the template

## Pivot paths and replay from 0.24.0

Newly generated reports have a bar cutoff slider. The chart, indicator values and
event records show only observations up to that close; whole-run metrics and raw
downloads still cover the complete run. Old report directories are not rewritten.

Ordinary indicators keep their existing rendering. Strategies can opt into
`render="path"` to connect confirmed vertices at `source_time_ns`, add a dashed
`projection_from` series for the current candidate leg, and hide a diagnostic
trajectory by default with `default_visible=False`. Availability remains tied to
the recorded observation, so a historical coordinate cannot introduce a future
confirmation into replay. See [the chart contract](backtesting.md#recording-current-indicator-values).

During partial replay, legacy unverified histories and terminal order cache data
are withheld. Without an observation-time history their availability cannot be
proven. Full-run view and downloaded records remain available.

## Single-language reports from 0.23.0

Use `make backtest ... REPORT_LANG=zh` or `REPORT_LANG=en` (CLI `--report-lang`).
The default is Chinese, independently of the system `LANG`. Each new report has
one interface language covering statistics, tables, actions, orders, sizing,
markers, help and accessibility labels. Its URL may override the saved default
with `?lang=zh` or `?lang=en`; invalid values show a localized warning and use
the saved default. Data exports do not change with the selected language.

Strategy names, instrument/order IDs, brands, currency codes and original JSON
fields remain original data. Indicator snapshot descriptors may provide
`label_i18n: {"zh": "...", "en": "..."}`; both nonempty labels are required.
Without that optional mapping, the strategy's original series name is retained.
The common viewer has no strategy-specific translation rules.

Existing archived HTML retains its own viewer. Generate a new report to obtain
language support; previewing an old directory does not rewrite its assets.

## Expandable entry sizing details from 0.22.0

New reports observe entry sizing inputs and expose an expandable column in each
entry Fill: available balance, account equity, capital basis, base target notional,
post-limit sizing notional, actual order input notional, reference price, multiplier,
quantity before rounding, created/submitted quantity, increment and fees. The
pre-rounding quantity is explicitly derived from the frozen linear-order inputs;
created and submitted quantities are actual observations, including custom rounding.

Fixed capital and compound sizing retain the capital actually read by the sizing
method; available balance and mark-to-market equity are separate. Unknown custom
inputs and unpriced equity remain unrecorded. An order's partial fills share a
frozen plan, while each Fill retains its own actual commission. The taker estimate
is a reference value, not a reservation or another deduction. Strategies and their
execution methods run once, with observation wrappers restored on detach.

These optional `sizing` objects are exported on signals, orders and fills. Signals
only carry their sizing-call snapshot; per-order inputs belong to orders/fills.
Archived viewers/data are unchanged and missing snapshots are never reconstructed
from final balances. Regenerate a report to collect this evidence.

## Whole-run performance and callback signals from 0.21.0

New HTML reports show the full engine summary above the chart, including realized
return, net P&L, daily-return drawdown, win rate, profit factor and Sharpe, plus
starting and ending balances. These values remain independent of chart/event
filters. They are engine statistics, not a reconstructed equity curve; balances
exclude unrealized P&L. Missing statistics remain unavailable. New summaries name
their settlement currency. Archived viewers remain unchanged.

Strategies that emit signals outside `calculate_signal` may expose
`on_execution_signal(ctx, signal, bar)` as a notification hook and invoke it once
before admission/execution. The collector observes it at the current strategy
clock, retaining native signal correlation without repeating indicator snapshots.
The hook must not execute orders; the strategy owns its normal entry gates.

## Net fill accounting and automatic preview from 0.20.0

New reports include per-fill net realized P&L, net closing return and cumulative
instrument P&L. The observer exports explicit contract economics and structured
commissions. Partial exits and reversals use weighted-average remaining cost and
allocate entry/exit fees without charging cumulative fees twice. Opening fees
are expensed immediately; pure opening fills have no closing return. Archived
reports without these observations display Unavailable rather than guessed P&L.
See [backtesting.md](backtesting.md) for the exact calculation and scope.

`make backtest ... PREVIEW=1` (Python CLI `--preview`) opens the exact report from
that completed run in the default browser, served over foreground loopback HTTP.
It is incompatible with `CHART=0`. No strategy skeleton or toolchain change is
needed. Existing archived report directories remain unchanged.

## Event table columns from 0.19.0

New reports use separate columns for each event attribute and numeric table IDs.
The expansion arrow sits to the left of the signal number. Fill rows show side,
execution price, quantity, signed position before and after, order type, order ID
and position ID independently. New backtests observe native order types; archived
data without that field displays Unknown. No strategy skeleton change is needed.

## Responsive chart selection from 0.18.0

New reports combine crowded marker labels into clickable counts and retain every
event in a selection list. Locate highlights the exact event's candle; selecting
a chart label reveals and highlights the corresponding signal or fill row,
including its order identity. Narrow screens stack event fields vertically and
keep UTC timestamps on one line. No strategy or data schema update is needed.
Existing archived reports retain their original viewer and data.

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
