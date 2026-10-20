"""Generate explicitly synthetic multi-pane data for manual browser checks.

Run: uv run python -m tests.chart_browser_fixture
The output is a rendering fixture, not a strategy backtest or indicator benchmark.
"""

from pathlib import Path

from tools.backtest.report import write_report

START = 1_735_689_600_000_000_000
HOUR = 3_600_000_000_000


def fixture() -> dict:
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
    return dict(
        schema_version=1, candles=candles, indicators=indicators, signals=[], fills=[], orders=[]
    )


if __name__ == "__main__":
    print(
        write_report(
            Path("examples/trend/sma_cross"),
            {"strategy": "Synthetic rendering fixture — not a backtest"},
            fixture(),
        )
    )
