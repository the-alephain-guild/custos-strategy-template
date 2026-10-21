# Inspecting a backtest

Run the same command as before:

```sh
make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-02-01
```

The command prints the statistics and an absolute path to `report.html`. The HTML
shows whole-run realized return, net P&L, daily-return drawdown, win rate, profit
factor, Sharpe and balances above the chart. These are the original engine
statistics and do not change with chart or event filters. Balances exclude open
positions' unrealized P&L, and are not ending equity. Missing statistics are not
estimated; download `summary.json` for all original values and precision. Open that
file in a modern browser. Keep it with its `assets/` directory: all scripts, data,
styles and fonts are local, so the report works offline. No account or web service
is required. The chart library is TradingView Lightweight Charts, not a connection
to a chart on tradingview.com.

Each run gets a new directory under the strategy's `backtests/output/`, containing
`report.html`, `chart.json`, `summary.json` and assets. Earlier reports are not
overwritten. The existing top-level summary JSON remains available; `JSON=1` still
prints its original fields, plus a `chart` object with status and the report path.
Use `CHART=0` (or the Python CLI's `--no-chart`) to skip observation and reporting.

Choose an instrument/timeframe, toggle indicators, signals and fills, and inspect
the event table. The chart initially shows a width-dependent recent window (up to
250 candles); **Fit all data** shows the whole window and **Recent candles**
returns to the recent view. Narrow screens start with indicator controls folded.
Tables are paginated across all rows; JSON retains all data.
Signal and fill layers have both different labels and different shapes.

Indicator controls are grouped by pane with matching color samples. Point at a
candle to read prices and indicator values, or focus the chart and use Left/Right
to inspect it with the keyboard. The default layout prioritizes price and indicator
trends; events are in the table below. Click a candle to populate **Selected events**,
which opens between the chart and the table when several events need choosing. Each event row has a
**Locate** action; **Visible candles only** can be
turned off to inspect the full event history. Original metadata is expandable.
The value tables provide every exported candle and indicator observation.

## One signal, many fills

The report has one UI language. Use `REPORT_LANG=zh` (the default) or
`REPORT_LANG=en` with `make backtest`, or `--report-lang zh|en` with the Python
CLI. New report URLs also accept `?lang=zh` and `?lang=en` without changing the
exported trading data. Original JSON fields and identifiers retain their values;
strategies can supply `label_i18n` on arbitrary indicator snapshots for translated
series names. Old archived viewers remain unchanged.

The event table uses each Signal as a parent row. The arrow **left of its number** expands its
child executions, including multiple partial fills of one order or fills from
different orders. Every fill keeps its actual time, price, quantity, fee, original
order/trade identity and Locate action. No execution is collapsed into an average
or deduplicated merely because its time or price matches another fill. Large
groups reveal 50 child records at a time. The Fills column shows the count only.

Signal numbers are `1`, `2`, and so on. Fill numbers restart at `1` within each
parent; table fill `3` under signal `1` corresponds to chart marker `FILL1.3`.
Actions, side, time, price, quantity, fee, order ID and position ID have separate
columns. Position before and Position after are signed net instrument quantities:
positive is long, negative is short, and zero is flat. They are distinct from the
native position ID. These values are recorded from execution inventory, not from
the signal's requested size.

Order type comes from the native fill event or an explicitly matching cached order.
It retains Market, Limit, Stop limit and other native types without reducing stops
to ordinary market/limit orders. Older reports without a recorded type display
Unknown; prices, side and liquidity roles are not used to guess it. New backtests
export optional `order_type` fields on fills and orders within chart schema 1.
Wide fill tables scroll independently; narrow screens show each field separately
with complete UTC timestamps.

Entry fills have an expandable **定仓明细** column. It records available quote
balance, reliable account equity, the actual sizing capital and base budget,
post-limit notional, actual order input, reference price, multiplier, created and
submitted quantity, increment and fee rates. Before-rounding quantity is explicitly
recomputed from the frozen linear order inputs, not inferred from the eventual
fill price. Custom sizing may leave standard-base fields unrecorded. In compound
mode the toolkit normally sizes from free balance; risk equity is a separate value.

The snapshot belongs to the sizing call and order construction, which can precede
a limit fill. Partial fills of that order share it, while each Fill shows only its
own actual commission. A taker-fee estimate is a reference, not proof of a reserve
or an additional debit. Unknown fields and old snapshots show 未记录. No later
balance, later fill or final order-fee total is written back into earlier rows.

Chart labels use `SIG1` for a parent and `FILL1.1`, `FILL1.2` for its children. If
several fills of one parent align to the same candle, a count marker represents
them visually; expand the parent to inspect every original record. A parent with
no fill remains visible. Fills with missing or unknown `signal_id` appear in an
explicit **Unlinked fills** group and are never attached by nearest timestamp.

Visible-range filtering keeps a group when its signal or any fill is in view.
Expanding it shows the whole relationship, including executions after the signal.
This is retrospective inspection, not a claim that the signal knew later fills.
Switching candle timeframes preserves source event timestamps and forward-aligns
them only for display. This hierarchy changes no strategy or trading records.

New reports include this viewer. Archived reports retain their original viewer;
rerendering an archive should create a new report directory and preserve its
chart data, rather than rewriting the old report.

## Local HTTP preview

For an in-app browser, start the report-only HTTP preview instead of opening a
`file://` URL:

```sh
make preview-backtest STRATEGY=trend/my_idea
# Or select an exact historical report, without rerunning it:
make preview-backtest REPORT=strategies/trend/my_idea/backtests/output/<run>/report.html
```

Open the printed `http://127.0.0.1:<port>/<random-path>/report.html` URL. Keep the
terminal running while viewing, and press **Ctrl+C** there when finished. The
default `PORT=0` chooses a free port; use `PORT=8765`, for example, when you want
a fixed local port. `make help CMD=preview-backtest` explains every option.

`STRATEGY` selects the report.html with the newest file modification time under
`backtests/output/*/`; the directory name only breaks equal-time ties. Copying or
touching an older HTML can therefore make it the latest selection. The printed
Report path identifies the exact file. The running preview stays pinned to that
directory: refreshing its URL does not switch to a later backtest. Start another
preview to select a new latest report. `REPORT` selects an exact
report file or its containing directory and takes precedence when both Make
variables are set. Preview does not run a backtest, recalculate indicators, or
edit results. A missing chart requires a new `make backtest` without `CHART=0`.

The server binds only IPv4 loopback and exposes the selected report, its JSON
files and its local assets. It has no directory listing, parent-file access,
symbolic-link access, write endpoints or cross-origin access. Each process has a
new random URL path. Stop the process to close access; no persistent service is
installed. This entry works independently of the browser's file-URL policy.

To backtest and open **that run's report** automatically:

```sh
make backtest STRATEGY=trend/my_idea START=2025-01-01 END=2025-04-01 PREVIEW=1
# The Python CLI also accepts the flag directly:
uv run python tools/backtest/run.py trend/my_idea --start 2025-01-01 --end 2025-04-01 --preview
```

GNU Make does not accept a custom `--preview` option; its entry uses `PREVIEW=1`.
Automatic preview uses the exact path returned by the successful run, never the
latest-report selector. It starts the same read-only loopback server and opens its
URL in the system's default browser. For an in-app browser, use the printed URL.
Keep the command running while viewing; Ctrl+C stops only the preview. Reports and
summary files remain saved. `CHART=0` and `PREVIEW=1` are mutually exclusive, and a
failed export does not start preview. If no browser can be launched, the server
stays available with an explicit message and URL to open manually.

## Net realized P&L, closing return and cumulative P&L

New Fill tables add three independent columns. Values are calculated during
export with 50-digit Decimal arithmetic, stored as decimal strings, and formatted
in the browser without converting monetary values to binary floating point.
Money display rounds to the observed currency precision; closing percentages
display four decimals. Exact values and their components remain in Original
record and the JSON download.

The ledger starts flat and keeps a weighted-average remaining cost pool for each
instrument. Same-direction fills add to the pool; opposite fills consume it in
proportion to their closed quantity. A fill crossing zero closes the old exposure
and opens its residual at the current fill price. Same-timestamp fills preserve
callback order. Each row uses only earlier observations, never future entry prices.

- **Realized P&L (net)** is newly realized closing gross P&L minus this fill's
  entire commission. Pure entry fills therefore show a fee expense, not zero.
- **Return (net %)** applies only to the closed portion. It equals closing gross
  P&L minus its allocated original entry fees and allocated current closing fees,
  divided by the matched entry notional, times 100. The denominator is not leverage
  margin or account starting balance. Pure opening fills show a dash.
- **Cumulative P&L (net)** sums per-fill net realized P&L over this instrument's
  full chronological history. Filters, pages, signal groups and chart timeframes
  do not reset it. Other instruments and other currencies have separate ledgers.

Entry fees in the closing-return numerator were already recognized in their
opening fill rows; cumulative P&L never charges them again. For example, buy 1 at
100 with a fee of 1, then sell 1 at 110 with a fee of 1: the two fill P&Ls are -1
and +9, cumulative is +8, and the net closing return is 8%. Reversal commissions
are split by closing/opening quantity for return and remaining entry-fee pools,
while the full commission affects per-fill P&L exactly once. Negative fees are rebates.

The supported scope is explicit linear, quote-settled instrument economics,
including the template's ordinary spot and perpetual instruments. Missing contract
information, inverse/unsupported price models, inconsistent position history or a
nonzero fee in another currency produce Unavailable with a reason. No FX rate,
contract multiplier or missing fee is guessed. Once a history becomes incomplete,
later cumulative values for that instrument also remain unavailable. Other
instruments are unaffected. Funding, transfers, position adjustments and unrealized
P&L are outside this fill-derived metric.

The accounting is consistent with the [position concepts](https://nautilustrader.io/docs/latest/concepts/positions/)
of fee recognition per fill and P&L realization on closed quantity. It is verified
against the pinned local engine; the closing-return column deliberately includes
both allocated entry and exit fees rather than using an engine's gross price return.

## Time and execution

The horizontal axis uses candle **close time in UTC**. A signal marker records the
bar on which `calculate_signal` actually returned the signal, before entry gates.
A filtered or refused signal can therefore have no fill. The declared signal
timestamp is retained separately; it cannot override the observation time.

A fill marker uses the native `OrderFilled` event, its execution price and quantity.
BUY can open a long or close a short; SELL can open a short or close a long. The
exporter follows signed inventory to distinguish these, including partial exits
and a fill crossing through zero. Such a fill has both closing and opening legs.
No synthetic closing fill is added to an open position at the end of a backtest.
Inventory classification assumes the local backtest starts flat, as this runner
does. It is not an importer for an account that starts with existing positions.

The toolkit's order tags/map provide signal lineage. The observer captures tags
before dispatch, since filled market orders can lose their tags and position
cleanup can clear the map. Unknown associations remain `null`; protection orders
may refer to the entry signal that created them, rather than a fresh exit signal.

An off-grid marker is placed at the first candle close at or after its event time.
Events outside candle coverage stay in the table. The table retains original
nanosecond timestamps as decimal strings, avoiding JavaScript integer precision
loss. Multiple events on one candle are retained. An arrow indicates the event's
candle and direction; its exact execution price is in the table and tooltip.

Confirmed-pivot indicators must keep `pivot_time` separate from `confirmed_at`.
Put `confirmed_at_ns` in signal metadata when available: export rejects a signal
whose observation precedes that confirmation. Pivot time is descriptive metadata,
never a transaction timestamp. Candidate indicator values may change on later
bars without editing earlier observations or transactions.

## Recording current indicator values

The optional, read-only `get_chart_snapshot(self, ctx, bar)` method is called once
after the strategy's `calculate_signal` returns. Return current scalar values,
not accumulated historical arrays. The observer immediately copies each value
and assigns the current bar's close time. This avoids quadratic history copying
and makes values available only when they were observed. Keep this method free
of changes to trading state. Strategies without it still run.

```python
def get_chart_snapshot(self, ctx, bar):
    return {
        "average": {
            "label": "Moving average",
            "value": ctx.indicators["average"].value,
            "color": "#60a5fa",
        },
        "oscillator": {
            "value": ctx.indicators["oscillator"].value,
            "pane": "oscillator",
            "levels": [30, 70],
        },
        "difference": {
            "value": ctx.indicators["difference"].value,
            "pane": "momentum",
            "style": "histogram",
        },
    }
```

Every mapping key identifies one series for this instrument/timeframe. An indicator
with several outputs, such as MACD, returns several keys sharing one pane. There
is no built-in indicator-name registry or browser-side indicator recalculation.
Only bars reaching `calculate_signal` are sampled; framework warmup and short-
circuited auxiliary feeds have no strategy snapshot. Candles for all feeds remain
selectable, and snapshots carry their own instrument and bar type.

| Field | Meaning |
|---|---|
| `value` | Required finite number, or `None` for a gap/unavailable observation. |
| `label` | Human-readable label; defaults to the series key. |
| `pane` | `price` for the candle pane; any other string creates a shared sub-pane. |
| `style` | `line` (default), `area`, or `histogram`. |
| `color` | Stable series color in `#RRGGBB` format. |
| `levels` | Stable finite reference-line values. |
| `segment` | Optional string/integer identifying a continuous segment. Changes split the line. |
| `segment_color` | Optional `#RRGGBB` color for the current segment; changes split the line. |
| `source_time_ns` | Source/pivot time, no later than the observation. Used as a drawing coordinate only by explicit `render="path"`; never moves a signal or fill. |
| `render` | `observations` (default) plots every observation at its recorded time. `path` connects non-null confirmed vertices at source coordinates, spanning null observations. Requires observed line snapshots. |
| `projection_from` | Optional key of a confirmed path in the same feed and pane. Draws only its latest known vertex to this series’ latest candidate; no anchor, a null candidate or a non-forward endpoint draws no leg. |
| `line_style` | `solid` (default) or `dashed`. |
| `default_visible` | Boolean, default `True`. Set `False` for optional diagnostic trajectories. |

Drawing options, labels, panes, styles, base colors and levels stay constant during a run.
Confirmed path source coordinates must strictly increase. A path cannot silently
replace an old confirmed vertex. Every non-null path value needs a source time;
projection sources must exist and cannot be other projections. No indicator name
is special-cased by the viewer.

For pivot-based indicators, export a sparse confirmed `path`, a second `path`
with `projection_from` referencing it and `line_style="dashed"`, and optionally
the existing per-bar trajectory with `default_visible=False`. The strategy supplies
observations, not completed future geometry. Source coordinates describe where a
pivot occurred; observation times determine when that geometry becomes visible.

The report includes a bar cutoff slider and Previous bar, Next bar and Show full
run controls. Candles, indicator values, paths, signals and fills are restricted
to observations at or before the selected close. Paths are rebuilt from that
prefix, so a future confirmation cannot introduce an earlier vertex. Dashed legs
use the last candidate available at that cutoff, never the final candidate in
the run. Locate selects existing events without moving the cutoff. Layer choices
survive replay steps. Cached terminal orders and unverified historical indicator
values are omitted during partial replay because their availability cannot be
established. Full-run statistics and raw downloads remain full-run data, explicitly
labelled as such; replay is a visualization of recorded observations, not an engine
rerun or a simulation of intrabar prices.
 Use
`None`, segment changes or segment colors to express discontinuities; do not
rename series every bar. Unknown fields, non-finite values, future source times,
repeated snapshot times or descriptor changes fail chart export explicitly.

## Finding signals, orders and partial fills

The chart places signal labels above candles and actual fill labels below them.
Enter long uses a teal up arrow, exit long an amber square, enter short a rose down
arrow, and exit short a blue circle. A legend and full action labels make the
meaning available without relying on color. Signal label icons are outlined and
fill label icons are solid. Mixed groups use a neutral icon and list action
counts; an atomic reversal retains both actions. Native candle markers also
retain each action when several occur at one timestamp.

When labels
would overlap at the current zoom, an explicit event-count button replaces the crowded labels (for example,
“3 signals · enter long 2 · exit short 1”). This count is not a record number.
It opens a list of every event in that cluster; choose an individual signal or
fill to reveal its exact record. Display grouping never changes event times.

Locate in a record highlights its aligned candle with a persistent vertical line
and selects that record. Clicking a chart label highlights the matching row;
fills expand their signal parent automatically, including fills beyond the first
50 and signals on later table pages. A cluster or candle with several events
offers individual selection instead of guessing which order was intended.
Selection identifies a fill by its own event ID, so different orders or partial
fills sharing a timestamp remain separate. The order ID is visible in each fill.

On narrow screens, event fields stack vertically with full single-line UTC
timestamps. Each fill retains its price, quantity, fee, order, metadata and Locate
control. Complete raw nanosecond times remain available in Original times and
metadata. Table filters and layer switches remain available on every screen size.

## Existing history exporters

Without a snapshot method, the exporter reads `get_indicator_history()` after the
backtest. Its mapping format is `{name: {type, config, points}}`; points use UTC
seconds in `time` and a numeric `value`. An optional `direction` splits segments.
`config` can provide `pane`, `style` and `color`. No period is subtracted from time.

These series are marked **historical, unverified** because an end-of-run array
cannot prove when a value became available. They cannot generate signals or fills.
With multiple candle feeds each series must explicitly supply `instrument_id`
and `bar_type`; the exporter refuses to guess their owner. If both methods exist,
the snapshot method is authoritative. Remove an empty snapshot stub if you intend
to keep using a legacy history exporter.

## Failures and verification

Observation failures are recorded without replacing the original strategy
callbacks or suppressing their trading behavior. The command preserves the
completed backtest's summary but exits unsuccessfully when chart export fails,
with `chart.status = "failed"` and an error. A missing or invalid report must not
be interpreted as a backtest with no signals or no trades. A browser loading error
also appears explicitly; the JSON files remain available for diagnosis.

`make verify` covers Python protocol and real-engine observation tests and runs
`make check-chart` (Node.js 18+) for browser time
alignment and segment transformations, and inspect an actual report in a browser,
including offline loading and a narrow viewport. Node is only a test tool; users
running a backtest or opening a report do not need it.

The observer does not establish that a strategy is causal just by recording it.
For that claim, compare cutoff runs against longer runs and runs with altered
future data, and ensure a deliberately backfilled fixture fails. Compare the
economic records, not randomly generated native UUIDs across separate runs.
