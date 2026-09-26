import json

import pytest

from tools import ui
from tools.runner import report


def _summary(**overrides) -> dict:
    summary = {
        "runner_publishes": True,
        "snapshots": 57,
        "first_snapshot": {
            "occurred_at": "2026-09-26T10:32:10.123456789Z",
            "status": {"current_equity": "10000.00000000"},
        },
        "latest_snapshot": {
            "occurred_at": "2026-09-26T10:41:25.454236091Z",
            "status": {
                "phase": "running",
                "position_count": 1,
                "order_count": 1,
                "open_notional": "925.14939",
                "peak_equity": "10000.00000000",
                "current_equity": "9999.45947430",
                "drawdown_pct": "0.00540525700",
                "reliable": True,
                "unreliable_reason": None,
            },
            "positions": [
                {
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "quantity": "-0.011",
                    "avg_px": "84097.40",
                    "unrealized_pnl": "-0.07799000",
                    "notional": "925.14939",
                }
            ],
            "orders": [
                {
                    "client_order_id": "3925e1bc",
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "side": "BUY",
                    "quantity": "0.011",
                    "price": None,
                    "status": "ACCEPTED",
                }
            ],
            "dropped_events": 0,
        },
        "fills": [
            {
                "ts_event": "2026-09-26T10:33:00.441954844Z",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "order_side": "SELL",
                "last_qty": "0.011",
                "last_px": "84097.40",
                "commission": "0.46253570 USDT",
            }
        ],
        "fill_count": 1,
        "closed_positions": 0,
        "realized_pnl": {},
        "fees": {"USDT": "0.46253570"},
        "unreadable_amounts": 0,
    }
    summary.update(overrides)
    return summary


def _render(summary: dict, capsys, monkeypatch) -> str:
    monkeypatch.setattr(ui, "Console", None)  # plain text is what the assertions read
    report.render(summary, strategy="trend/supertrend", mode="sandbox")
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_shows_the_account_positions_orders_and_fills(capsys, monkeypatch) -> None:
    out = _render(_summary(), capsys, monkeypatch)

    assert "9999.45947430" in out
    assert "-0.54052570 (-0.01%)" in out  # change since the first snapshot
    assert "since 2026-09-26 10:32:10 UTC" in out
    assert "BTCUSDT-PERP.BINANCE" in out and "-0.011" in out and "-0.07799000" in out
    assert "3925e1bc" in out and "market" in out  # an order with no limit price
    assert "84097.40" in out and "0.46253570 USDT" in out
    assert "0.46253570 USDT" in out.split("Totals")[1]


def test_an_unreliable_snapshot_says_why_and_shows_no_positions(capsys, monkeypatch) -> None:
    summary = _summary()
    summary["latest_snapshot"]["status"].update(reliable=False, unreliable_reason="missing_price")
    summary["latest_snapshot"]["positions"] = []

    out = _render(summary, capsys, monkeypatch)

    assert "missing_price" in out
    assert "positions are left out" in out


def test_a_runner_that_does_not_publish_is_named_as_such(capsys, monkeypatch) -> None:
    summary = _summary(runner_publishes=False, snapshots=0, first_snapshot=None,
                       latest_snapshot=None, fills=[], fill_count=0, fees={})  # fmt: skip

    out = _render(summary, capsys, monkeypatch)

    assert "does not publish" in out
    assert "TOOLCHAIN=dev" in out


def test_no_snapshot_yet_says_when_the_first_one_comes(capsys, monkeypatch) -> None:
    summary = _summary(snapshots=0, first_snapshot=None, latest_snapshot=None,
                       fills=[], fill_count=0, fees={})  # fmt: skip

    out = _render(summary, capsys, monkeypatch)

    assert "10 seconds" in out


def test_dropped_events_are_warned_about(capsys, monkeypatch) -> None:
    summary = _summary()
    summary["latest_snapshot"]["dropped_events"] = 3

    out = _render(summary, capsys, monkeypatch)

    assert "3 fills or closed positions were not reported" in out


def test_main_prints_the_summary_unchanged_with_json(monkeypatch, capsys) -> None:
    document = _summary()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(document)))

    assert report.main(["report.py", "--strategy", "trend/x", "--mode", "sandbox", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == document


def test_main_refuses_output_that_is_not_a_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("Traceback: boom"))

    assert report.main(["report.py", "--strategy", "trend/x", "--mode", "sandbox"]) == 1


@pytest.mark.parametrize(
    ("first", "latest", "expected"),
    [
        ("10000", "10100", "+100 (+1.00%)"),
        ("10000", "10000", "0 (0.00%)"),
        ("0", "5", "+5"),
    ],
)
def test_change_is_signed_and_in_percent_where_it_can_be(first, latest, expected) -> None:
    assert report.change(first, latest) == expected
