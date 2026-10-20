# Inspecting a backtest

Run the same command as before:

```sh
make backtest STRATEGY=examples/trend/sma_cross START=2025-01-01 END=2025-02-01
```

The command prints the statistics and an absolute path to `report.html`. Open that
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
trends; events are in the table below. Click a candle to populate **Selected candle
event details**, which can be expanded below the table. Each event row has a
**Locate** action; **Visible candles only** can be
turned off to inspect the full event history. Original metadata is expandable.
The value tables provide every exported candle and indicator observation.

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
| `source_time_ns` | Optional descriptive source/pivot time, no later than the observation. Never changes its position. |

Labels, panes, styles, base colors and levels stay constant during a run. Use
`None`, segment changes or segment colors to express discontinuities; do not
rename series every bar. Unknown fields, non-finite values, future source times,
repeated snapshot times or descriptor changes fail chart export explicitly.

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
