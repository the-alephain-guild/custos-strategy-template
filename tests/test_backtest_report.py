"""Time provenance and generic chart contracts, including deliberate backfills."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tools.backtest.report import (
    ChartError,
    indicator_snapshot,
    legacy_indicators,
    split_fill,
    validate_report,
    write_report,
)

T = 1_735_689_600_000_000_000


@pytest.mark.parametrize(
    "before,side,qty,actions,after",
    [
        ("0", "BUY", "2", ["enter_long"], "2"),
        ("2", "SELL", "1", ["exit_long"], "1"),
        ("1", "SELL", "3", ["exit_long", "enter_short"], "-2"),
        ("-2", "BUY", "3", ["exit_short", "enter_long"], "1"),
        ("-2", "SELL", "1", ["enter_short"], "-3"),
    ],
)
def test_fill_actions_follow_inventory_not_buy_sell_names(before, side, qty, actions, after):
    legs, inventory = split_fill(Decimal(before), side, Decimal(qty))
    assert [leg["action"] for leg in legs] == actions
    assert inventory == Decimal(after)
    assert sum(Decimal(leg["quantity"]) for leg in legs) == Decimal(qty)


@pytest.mark.parametrize("style,pane", [("line", "price"), ("histogram", "macd"), ("area", "rsi")])
def test_snapshots_support_arbitrary_names_styles_and_panes(style, pane):
    series = {}
    ctx = SimpleNamespace(instrument_id="BTC.TEST")
    for offset, value in enumerate((10, None, 12)):
        indicator_snapshot(
            series,
            {
                "any_name": {
                    "style": style,
                    "pane": pane,
                    "value": value,
                    "levels": [30, 70],
                    "segment": offset,
                }
            },
            ctx,
            SimpleNamespace(ts_event=T + offset, bar_type="daily"),
        )
    assert [p["value"] for p in next(iter(series.values()))["points"]] == [10, None, 12]


@pytest.mark.parametrize(
    "raw",
    [
        {"value": float("nan")},
        {"value": True},
        {"value": 1, "time_ns": str(T - 1)},
        {"value": 1, "source_time_ns": str(T + 1)},
    ],
)
def test_a_snapshot_cannot_inject_bad_values_or_override_observation_time(raw):
    with pytest.raises(ChartError):
        indicator_snapshot(
            {},
            {"test": raw},
            SimpleNamespace(instrument_id="BTC.TEST"),
            SimpleNamespace(ts_event=T, bar_type="daily"),
        )


def test_history_is_explicitly_unverified_and_not_shifted_to_bar_open():
    series = legacy_indicators(
        {"MA": {"type": "SMA", "points": [{"time": T // 10**9, "value": 42}]}}
    )
    assert series[0]["provenance"] == "historical_unverified"
    assert series[0]["points"][0]["time_ns"] == str(T)


def data():
    return {
        "schema_version": 1,
        "candles": [
            {
                "instrument_id": "BTC.TEST",
                "bar_type": "daily",
                "open_time_ns": str(T - 10),
                "close_time_ns": str(T),
                "open": 100,
                "high": 110,
                "low": 90,
                "close": 105,
                "volume": 1,
            }
        ],
        "indicators": [],
        "fills": [],
        "orders": [],
        "signals": [
            {
                "id": "s1",
                "instrument_id": "BTC.TEST",
                "bar_type": "daily",
                "event_time_ns": str(T),
                "action": "enter_short",
                "price": "105",
                "metadata": {"pivot_time_ns": str(T - 10), "confirmed_at_ns": str(T)},
            }
        ],
    }


def test_a_deliberate_backfill_to_pivot_time_is_refused():
    report = data()
    validate_report(report)
    report["signals"][0]["event_time_ns"] = str(T - 10)
    with pytest.raises(ChartError, match="confirmation"):
        validate_report(report)


def test_report_runs_never_overwrite_and_embedded_metadata_cannot_close_script(tmp_path):
    report = data()
    report["signals"][0]["metadata"]["label"] = '</script><script>alert("x")</script>'
    summary = {"strategy": "demo", "returns": {"missing": float("nan")}}
    first = write_report(tmp_path, summary, report)
    old = first.read_bytes()
    second = write_report(tmp_path, summary, deepcopy(report))
    assert first != second and first.read_bytes() == old
    assert "</script><script>alert" not in first.read_text()
    assert (first.parent / "chart.json").is_file()
    assert (first.parent / "assets/vendor/lightweight-charts-5.2.1.js").is_file()
    assert (first.parent / "assets/chart-markers.js").is_file()
