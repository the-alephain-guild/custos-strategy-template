"""Generate explicitly synthetic multi-pane data for manual browser checks.

Run: uv run python -m tests.chart_browser_fixture
The output is a rendering fixture, not a strategy backtest or indicator benchmark.
"""

import argparse
from decimal import Decimal
from pathlib import Path

from tools.backtest.report import write_report

START = 1_735_689_600_000_000_000
HOUR = 3_600_000_000_000


def fixture(dense: bool = False) -> dict:
    candles, indicators = [], []
    for instrument, step in (("BTC.TEST", HOUR), ("ETH.TEST", HOUR), ("BTC.TEST", 4 * HOUR)):
        bar_type = f"{instrument}-{step}"
        for i in range(80):
            value = 100 + i % 10
            candles.append(
                dict(
                    instrument_id=instrument,
                    bar_type=bar_type,
                    open_time_ns=str(START + i * step),
                    close_time_ns=str(START + (i + 1) * step),
                    open=value,
                    high=value + 3,
                    low=value - 2,
                    close=value + 1,
                    volume=100 + i,
                )
            )
        for key, pane, style in (
            ("MA fixture", "price", "line"),
            ("RSI fixture", "oscillator", "area"),
            ("MACD value fixture", "momentum", "line"),
            ("MACD signal fixture", "momentum", "line"),
            ("MACD histogram fixture", "momentum", "histogram"),
            ("Segment fixture", "price", "line"),
        ):
            points = [
                dict(
                    time_ns=str(START + (i + 1) * step),
                    value=None if i % 13 == 0 else (100 + i % 10 if pane == "price" else i % 20),
                    segment=i // 20 if key == "Segment fixture" else None,
                    color="#ef5350" if i // 20 % 2 else "#26a69a",
                )
                for i in range(80)
            ]
            indicators.append(
                dict(
                    key=key,
                    label=key,
                    pane=pane,
                    style=style,
                    color="#60a5fa",
                    levels=[30, 70] if pane == "oscillator" else [],
                    instrument_id=instrument,
                    bar_type=bar_type,
                    provenance="bar_snapshot",
                    points=points,
                )
            )
    signals, fills = [], []
    inventory = Decimal(0)
    actions = (
        ("enter_long", ("0.3", "0.4", "0.3")),
        ("exit_long", ("1",)),
        ("enter_short", ("0.4", "0.6")),
        ("exit_short", ("1",)),
        ("enter_long", ()),
    )
    if dense:
        actions = (
            ("enter_long", tuple("0.01" for _ in range(60))),
            ("exit_long", ("0.6",)),
            ("enter_short", ("0.4", "0.6")),
            ("exit_short", ("1",)),
        ) * 9
    for index, (action, quantities) in enumerate(actions, 1):
        stamp = START + index * (2 if dense else 10) * HOUR
        signal_id = f"fixture-s{index}"
        signals.append(
            dict(
                id=signal_id,
                instrument_id="BTC.TEST",
                bar_type=f"BTC.TEST-{HOUR}",
                event_time_ns=str(stamp),
                declared_time_ns=str(stamp),
                action=action,
                price="105",
                metadata={"synthetic_fixture": True},
                native_signal_id=None,
            )
        )
        for child, quantity in enumerate(quantities, 1):
            before = inventory
            inventory += Decimal(quantity) * (1 if action in ("enter_long", "exit_short") else -1)
            fills.append(
                dict(
                    id=f"fixture-f{index}-{child}",
                    trade_id=f"fixture-trade-{index}-{child}",
                    order_id=f"fixture-order-{index}-{1 if child < 3 else 2}",
                    position_id="fixture-position",
                    instrument_id="BTC.TEST",
                    event_time_ns=str(stamp + (1 if child == 1 else 2) * 1_000_000),
                    side="BUY" if action in ("enter_long", "exit_short") else "SELL",
                    order_type="MARKET" if child < 3 else "LIMIT",
                    quantity=quantity,
                    price="105.1",
                    commission="0.01 TEST",
                    signal_id=signal_id,
                    position_before=str(before),
                    position_after=str(inventory),
                    legs=[{"action": action, "quantity": quantity}],
                )
            )
    return dict(
        schema_version=1,
        candles=candles,
        indicators=indicators,
        signals=signals,
        fills=fills,
        orders=[],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dense", action="store_true", help="36 signals with 60 partial fills per entry"
    )
    args = parser.parse_args()
    print(
        write_report(
            Path("examples/trend/sma_cross"),
            {"strategy": "Synthetic rendering fixture — not a backtest"},
            fixture(dense=args.dense),
        )
    )
