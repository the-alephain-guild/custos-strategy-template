#!/usr/bin/env python3
"""Lay out what a running strategy holds and has traded, for a terminal.

Reads the summary tools/runner/telemetry_read.py printed inside the runner
container from stdin. With --json the summary is printed as it came.

Usage:
    ... telemetry_read.py ... | python3 tools/runner/report.py --strategy trend/my_idea \\
        --mode sandbox [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

TAG = "report"


def change(first: str, latest: str) -> str:
    """The move from `first` to `latest`, signed, with its percentage where there is one."""
    start, end = Decimal(first), Decimal(latest)
    delta = end - start
    # Fixed-point: a difference of two equal 8-place amounts is otherwise "0E-8".
    text = f"+{delta:f}" if delta > 0 else f"{delta:f}"
    if start == 0:
        return text
    percent = (delta / start * 100).quantize(Decimal("0.01"))
    sign = "+" if percent > 0 else ""
    return f"{text} ({sign}{percent:f}%)"


def _when(stamp: str | None) -> str:
    """An RFC 3339 time as a readable UTC time; nanoseconds are more than a person needs."""
    if not stamp:
        return "unknown"
    try:
        seconds = stamp.rstrip("Z").partition(".")[0]
        moment = datetime.fromisoformat(seconds).replace(tzinfo=UTC)
    except ValueError:
        return stamp
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _totals(amounts: dict[str, str]) -> str:
    if not amounts:
        return "0"
    return ", ".join(f"{value} {currency}".strip() for currency, value in amounts.items())


def render(summary: dict[str, Any], *, strategy: str, mode: str) -> None:
    title = f"{strategy} ({mode})"
    latest = summary.get("latest_snapshot")
    if latest is None:
        if not summary.get("runner_publishes", True):
            ui.panel(
                title,
                [
                    "This runner does not publish positions, orders or fills.",
                    "The pinned runner release predates them; run with TOOLCHAIN=dev on a "
                    "Custos build that does (see docs/dev-toolchain.md).",
                ],
                kind="warn",
            )
        else:
            ui.panel(
                title,
                [
                    "No snapshot yet: the runner publishes the first one about 10 seconds "
                    "after the strategy starts, and one every 10 seconds after that."
                ],
            )
        return

    status = latest.get("status") or {}
    first = summary.get("first_snapshot")
    if first is None or not status.get("reliable", True):
        moved = "not known yet: the runner has not valued the account"
    else:
        moved = (
            f"{change(first['status']['current_equity'], status['current_equity'])} "
            f"since {_when(first.get('occurred_at'))}"
        )
    rows = [
        ("Equity", str(status.get("current_equity"))),
        ("Change", moved),
        ("Peak equity", str(status.get("peak_equity"))),
        ("Drawdown", f"{status.get('drawdown_pct')}%"),
        ("Open notional", str(status.get("open_notional"))),
        ("As of", _when(latest.get("occurred_at"))),
    ]
    ui.table(title, rows)
    if not status.get("reliable", True):
        ui.warn(
            f"the runner could not value this account ({status.get('unreliable_reason')}); "
            "positions are left out until it can",
            tag=TAG,
        )

    ui.grid(
        "Positions",
        ["Instrument", "Quantity", "Avg price", "Unrealized PnL", "Notional"],
        [
            [p["instrument_id"], p["quantity"], p["avg_px"], p["unrealized_pnl"], p["notional"]]
            for p in latest.get("positions") or []
        ],
    )
    ui.grid(
        "Open orders",
        ["Order", "Instrument", "Side", "Quantity", "Price", "Status"],
        [
            [
                o["client_order_id"],
                o["instrument_id"],
                o["side"],
                o["quantity"],
                o["price"] if o.get("price") is not None else "no limit",
                o["status"],
            ]
            for o in latest.get("orders") or []
        ],
    )
    shown = summary.get("fills") or []
    ui.grid(
        f"Recent fills ({len(shown)} of {summary.get('fill_count', len(shown))})",
        ["Time", "Instrument", "Side", "Quantity", "Price", "Fee"],
        [
            [
                _when(f.get("ts_event")),
                f.get("instrument_id", ""),
                f.get("order_side", ""),
                f.get("last_qty", ""),
                f.get("last_px", ""),
                f.get("commission", ""),
            ]
            for f in shown
        ],
    )
    ui.table(
        "Totals",
        [
            ("Fills", str(summary.get("fill_count", 0))),
            ("Closed positions", str(summary.get("closed_positions", 0))),
            ("Realized PnL", _totals(summary.get("realized_pnl") or {})),
            ("Fees", _totals(summary.get("fees") or {})),
        ],
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])
    text = sys.stdin.read()
    try:
        summary = json.loads(text)
    except json.JSONDecodeError:
        ui.error("the runner did not answer with a report; its output is below", tag=TAG)
        print(text, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    try:
        render(summary, strategy=args.strategy, mode=args.mode)
    except (KeyError, TypeError, InvalidOperation) as failure:
        ui.error(f"the report is not in a shape this repository reads: {failure!r}", tag=TAG)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
