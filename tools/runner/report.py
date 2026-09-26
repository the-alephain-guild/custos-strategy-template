#!/usr/bin/env python3
"""Lay out what a running strategy holds and has traded, for a terminal.

Reads the summary tools/runner/telemetry_read.py printed inside the runner
container from stdin; the Makefile adds when the runner started and which image
it runs. With --json the summary is printed as JSON instead.

Numbers are formatted from their decimal strings and never pass through a float:
amounts in the quote currency get thousands separators and two places, prices
and sizes lose trailing zeros and anything past eight places, which is where a
float-derived average price keeps its noise.

Usage:
    ... telemetry_read.py ... | python3 tools/runner/report.py --strategy trend/my_idea \\
        --mode sandbox [--started-at T --image I --revision R] [--json]
    python3 tools/runner/report.py --strategy trend/my_idea --mode sandbox --not-running
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

TAG = "status"
# How often the runner publishes a snapshot; refreshing faster only moves "updated … ago".
RUNNER_REPORTS_EVERY = 10
# Shown rounded half up, as people round; Decimal's default rounds half to even.
_CENT = Decimal("0.01")
_PLACES = Decimal("1e-8")


@dataclass(frozen=True)
class RunnerInfo:
    started_at: str
    image: str
    revision: str


# Numbers


def money(text: Any) -> str:
    """An amount in the quote currency: thousands separators, two places."""
    return f"{Decimal(str(text)).quantize(_CENT, ROUND_HALF_UP):,.2f}"


def signed_money(text: Any) -> ui.Cell:
    value = Decimal(str(text)).quantize(_CENT, ROUND_HALF_UP)
    if value > 0:
        return ui.Cell(f"+{value:,.2f}", "up")
    if value < 0:
        return ui.Cell(f"{value:,.2f}", "down")
    return ui.Cell(f"{value:,.2f}")


def number(text: Any) -> str:
    """A price or size, without trailing zeros or a float's tail past eight places."""
    rendered = f"{Decimal(str(text)).quantize(_PLACES, ROUND_HALF_UP):,f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def percent(text: Any) -> str:
    value = Decimal(str(text))
    if value != 0 and abs(value) < _CENT:
        return "<0.01%"
    return f"{value.quantize(_CENT, ROUND_HALF_UP):.2f}%"


def fee(text: Any) -> str:
    amount, _, currency = str(text or "").strip().partition(" ")
    try:
        return f"{number(amount)} {currency}".strip()
    except InvalidOperation:
        return str(text)


def totals(amounts: dict[str, str], signed: bool = False) -> str | ui.Cell:
    if not amounts:
        return "0"
    if signed and len(amounts) == 1:
        ((currency, value),) = amounts.items()
        cell = signed_money(value)
        return ui.Cell(f"{cell.text} {currency}".strip(), cell.tone)
    return ", ".join(fee(f"{value} {currency}") for currency, value in amounts.items())


def change(first: Any, latest: Any) -> ui.Cell:
    """The move from `first` to `latest`: arrow, signed amount, percentage where there is one."""
    start, end = Decimal(str(first)), Decimal(str(latest))
    delta = end - start
    moved = delta.quantize(_CENT, ROUND_HALF_UP)
    if moved > 0:
        arrow, tone, text = "▲ ", "up", f"+{moved:,.2f}"
    elif moved < 0:
        arrow, tone, text = "▼ ", "down", f"{moved:,.2f}"
    else:
        arrow, tone, text = "", "", f"{moved:,.2f}"
    if start == 0:
        return ui.Cell(f"{arrow}{text}", tone)
    share = delta / start * 100
    shown = percent(share)
    if shown != "<0.01%" and share > 0:
        shown = f"+{shown}"
    return ui.Cell(f"{arrow}{text} ({shown})", tone)


# Time


def _parse(stamp: str) -> datetime:
    """An RFC 3339 UTC time; nanoseconds are more than a person needs."""
    seconds = stamp.rstrip("Z").partition(".")[0]
    return datetime.fromisoformat(seconds).replace(tzinfo=UTC)


def when(stamp: str | None, now: datetime) -> str:
    """A time in the reader's zone, with the date only when it is not today."""
    if not stamp:
        return "unknown"
    try:
        moment = _parse(stamp).astimezone(now.tzinfo)
    except ValueError:
        return stamp
    if moment.date() == now.date():
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%m-%d %H:%M:%S")


def duration(span: timedelta) -> str:
    seconds = max(int(span.total_seconds()), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _since(stamp: str | None, now: datetime) -> str | None:
    if not stamp:
        return None
    try:
        return duration(now - _parse(stamp))
    except ValueError:
        return None


# Names


def short_id(identifier: str) -> str:
    return identifier if len(identifier) <= 10 else f"{identifier[:8]}…"


def runner_label(runner: RunnerInfo) -> str:
    image = runner.image.split("@", 1)[0].rsplit("/", 1)[-1]
    return f"{image} @ {runner.revision[:7]}" if runner.revision else image


def instrument(identifier: str) -> str:
    """An instrument without its venue, which the header names once for the whole report."""
    return identifier.rpartition(".")[0] or identifier


def venues(summary: dict[str, Any]) -> list[str]:
    latest = summary.get("latest_snapshot") or {}
    identifiers = [
        *(p.get("instrument_id", "") for p in latest.get("positions") or []),
        *(o.get("instrument_id", "") for o in latest.get("orders") or []),
        *(f.get("instrument_id", "") for f in summary.get("fills") or []),
    ]
    return sorted({i.rpartition(".")[2] for i in identifiers if "." in i})


def _side(quantity: str) -> ui.Cell:
    return ui.Cell("SHORT", "down") if Decimal(quantity) < 0 else ui.Cell("LONG", "up")


def _order_side(side: str) -> ui.Cell:
    upper = str(side).upper()
    return ui.Cell(upper, "up" if upper == "BUY" else "down" if upper == "SELL" else "")


def _title(strategy: str, mode: str) -> str:
    return f"{strategy.rsplit('/', 1)[-1]} · {mode}"


def _start_command(strategy: str, mode: str, toolchain: str) -> str:
    suffix = " TOOLCHAIN=dev" if toolchain == "dev" else ""
    return f"make start STRATEGY={strategy} MODE={mode}{suffix}"


# Layout


def render_not_running(*, strategy: str, mode: str, toolchain: str = "pinned") -> None:
    ui.header(_title(strategy, mode), [ui.Cell("not running", "muted")])
    ui.next_steps([(_start_command(strategy, mode, toolchain), "start it")])


def render(
    summary: dict[str, Any],
    *,
    strategy: str,
    mode: str,
    runner: RunnerInfo | None = None,
    now: datetime | None = None,
    refresh: int | None = None,
) -> None:
    now = now or datetime.now().astimezone()
    latest = summary.get("latest_snapshot")
    facts: list[str | ui.Cell] = []
    if runner is not None:
        up = _since(runner.started_at, now)
        if up:
            facts.append(ui.Cell(f"running for {up}", "up"))
    if latest is not None:
        ago = _since(latest.get("occurred_at"), now)
        if ago:
            facts.append(ui.Cell(f"updated {ago} ago", "muted"))
    facts.extend(venues(summary))
    if runner is not None:
        facts.append(ui.Cell(runner_label(runner), "muted"))
    if refresh:
        facts.append(ui.Cell(f"refreshing every {refresh}s · Ctrl-C to stop watching", "muted"))
        if refresh < RUNNER_REPORTS_EVERY:
            facts.append(ui.Cell(f"the runner reports every {RUNNER_REPORTS_EVERY}s", "warn"))
    ui.header(_title(strategy, mode), facts)

    if latest is None:
        if not summary.get("runner_publishes", True):
            ui.warn(
                "this runner does not publish positions, orders or fills; the pinned release "
                "predates them, so run with TOOLCHAIN=dev on a Custos build that does "
                "(see docs/dev-toolchain.md)",
                tag=TAG,
            )
        else:
            ui.info(
                "no snapshot yet: the runner publishes the first one about 10 seconds after "
                "the strategy starts, and one every 10 seconds after that",
                tag=TAG,
            )
        return

    status = latest.get("status") or {}
    reliable = status.get("reliable", True)
    first = summary.get("first_snapshot")
    if first is None or not reliable:
        moved: str | ui.Cell = ui.Cell("not known yet", "muted")
        moved_label = "Change"
    else:
        moved = change(first["status"]["current_equity"], status["current_equity"])
        moved_label = f"Change since {when(first.get('occurred_at'), now)[:-3]}"
    ui.space()
    ui.stats(
        [
            ("Equity", money(status.get("current_equity", 0))),
            (moved_label, moved),
            ("Drawdown", percent(status.get("drawdown_pct", 0))),
            ("Open notional", money(status.get("open_notional", 0))),
        ]
    )
    if not reliable:
        ui.warn(
            f"the runner could not value this account ({status.get('unreliable_reason')}); "
            "positions are left out until it can",
            tag=TAG,
        )

    ui.space()
    ui.grid(
        "Positions",
        ["Instrument", "Side", "Size", "Entry", "Unrealized PnL", "Notional"],
        [
            [
                instrument(p["instrument_id"]),
                _side(p["quantity"]),
                number(abs(Decimal(p["quantity"]))),
                number(p["avg_px"]),
                signed_money(p["unrealized_pnl"]),
                money(p["notional"]),
            ]
            for p in latest.get("positions") or []
        ],
        align=["left", "left", "right", "right", "right", "right"],
    )
    ui.space()
    ui.grid(
        "Open orders",
        ["Order", "Instrument", "Side", "Size", "Limit", "Status"],
        [
            [
                short_id(o["client_order_id"]),
                instrument(o["instrument_id"]),
                _order_side(o["side"]),
                number(o["quantity"]),
                number(o["price"]) if o.get("price") is not None else "—",
                o["status"],
            ]
            for o in latest.get("orders") or []
        ],
        align=["left", "left", "left", "right", "right", "left"],
    )
    shown = summary.get("fills") or []
    ui.space()
    ui.grid(
        f"Recent fills ({len(shown)} of {summary.get('fill_count', len(shown))})",
        ["Time", "Instrument", "Side", "Size", "Price", "Fee"],
        [
            [
                when(f.get("ts_event"), now),
                instrument(f.get("instrument_id", "")),
                _order_side(f.get("order_side", "")),
                number(f.get("last_qty", "0")),
                number(f.get("last_px", "0")),
                fee(f.get("commission", "")),
            ]
            for f in reversed(shown)
        ],
        align=["left", "left", "left", "right", "right", "right"],
    )
    ui.space()
    ui.stats(
        [
            ("Fills", str(summary.get("fill_count", 0))),
            ("Closed positions", str(summary.get("closed_positions", 0))),
            ("Realized PnL", totals(summary.get("realized_pnl") or {}, signed=True)),
            ("Fees", totals(summary.get("fees") or {})),
        ],
        title="Totals",
    )

    dropped = latest.get("dropped_events") or 0
    if dropped:
        ui.warn(
            f"{dropped} fills or closed positions were not reported: the runner was busier "
            "than it could report, so the totals above are short",
            tag=TAG,
        )
    if summary.get("unreadable_amounts"):
        ui.warn(
            f"{summary['unreadable_amounts']} amounts could not be read and are not in the totals",
            tag=TAG,
        )


def _with_runner(summary: dict[str, Any], runner: RunnerInfo | None) -> dict[str, Any]:
    document = dict(summary)
    if runner is not None:
        document["runner"] = asdict(runner)
    return document


def follow(
    lines: Iterable[str],
    *,
    strategy: str,
    mode: str,
    runner: RunnerInfo | None,
    toolchain: str,
    refresh: int,
    as_json: bool,
    live: bool,
) -> int:
    """Show each summary as it arrives, until the run ends or the watch is stopped.

    `live` redraws in place on a terminal; anywhere else each report follows the
    last under a rule, so a copy written to a file reads in order.
    """
    suffix = " TOOLCHAIN=dev" if toolchain == "dev" else ""
    target = f"STRATEGY={strategy} MODE={mode}{suffix}"
    shown = 0
    try:
        for line in lines:
            if not line.strip():
                continue
            try:
                summary = json.loads(line)
            except json.JSONDecodeError:
                ui.error(
                    f"the runner answered with something other than a report: {line!r}", tag=TAG
                )
                continue
            if as_json:
                print(json.dumps(_with_runner(summary, runner)), flush=True)
                continue
            silent = summary.get("latest_snapshot") is None and not summary.get(
                "runner_publishes", True
            )
            if live:
                ui.clear()
            elif shown:
                ui.rule()
            render(summary, strategy=strategy, mode=mode, runner=runner, refresh=refresh)
            shown += 1
            if silent:
                # Nothing will arrive from a runner that does not report; waiting
                # would only redraw the same explanation.
                return 0
    except KeyboardInterrupt:
        if not as_json:
            ui.space()
            ui.info("stopped watching; the strategy is still running", tag=TAG)
            ui.next_steps([(f"make stop {target}", "stop the strategy")])
        return 0
    if not as_json:
        ui.space()
        ui.info("the run has ended: its runner is gone", tag=TAG)
        ui.next_steps([(f"make start {target}", "start it again")])
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--toolchain", default="pinned")
    parser.add_argument("--not-running", action="store_true")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--revision", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--follow", type=int, metavar="SECONDS")
    args = parser.parse_args(argv[1:])

    if args.not_running:
        if args.json:
            print(json.dumps({"running": False}))
        else:
            render_not_running(strategy=args.strategy, mode=args.mode, toolchain=args.toolchain)
        return 0

    runner = RunnerInfo(args.started_at, args.image, args.revision) if args.image else None
    if args.follow:
        return follow(
            iter(sys.stdin.readline, ""),
            strategy=args.strategy,
            mode=args.mode,
            runner=runner,
            toolchain=args.toolchain,
            refresh=args.follow,
            as_json=args.json,
            live=sys.stdout.isatty(),
        )

    text = sys.stdin.read()
    try:
        summary = json.loads(text)
    except json.JSONDecodeError:
        ui.error("the runner did not answer with a report; its output is below", tag=TAG)
        print(text, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_with_runner(summary, runner), indent=2))
        return 0
    try:
        render(summary, strategy=args.strategy, mode=args.mode, runner=runner)
    except (KeyError, TypeError, InvalidOperation) as failure:
        ui.error(f"the report is not in a shape this repository reads: {failure!r}", tag=TAG)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
