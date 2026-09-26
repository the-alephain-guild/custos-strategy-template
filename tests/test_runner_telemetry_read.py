from decimal import Decimal

from tools.runner import telemetry_read


def _envelope(kind: str, occurred_at: str, **payload) -> dict:
    return {
        "envelope_version": 1,
        "occurred_at": occurred_at,
        "payload_schema_version": 1,
        "payload": {"kind": kind, **payload},
    }


def _snapshot(occurred_at: str, equity: str) -> dict:
    return _envelope(
        "snapshot",
        occurred_at,
        status={"current_equity": equity, "reliable": True, "unreliable_reason": None},
        positions=[],
        orders=[],
        dropped_events=0,
    )


def _fill(occurred_at: str, trade_id: str, commission: str) -> dict:
    return _envelope(
        "fill",
        occurred_at,
        trade_id=trade_id,
        instrument_id="BTCUSDT-PERP.BINANCE",
        order_side="SELL",
        last_qty="0.011",
        last_px="84097.40",
        commission=commission,
        ts_event=occurred_at,
    )


def test_keeps_the_first_and_latest_snapshot_and_counts_them() -> None:
    summary = telemetry_read.summarize(
        [
            _snapshot("2026-09-26T10:32:10Z", "10000"),
            _snapshot("2026-09-26T10:32:20Z", "10001.5"),
            _snapshot("2026-09-26T10:32:30Z", "9999.45"),
        ]
    )

    assert summary["snapshots"] == 3
    assert summary["first_snapshot"]["occurred_at"] == "2026-09-26T10:32:10Z"
    assert summary["first_snapshot"]["status"]["current_equity"] == "10000"
    assert summary["latest_snapshot"]["occurred_at"] == "2026-09-26T10:32:30Z"
    assert summary["latest_snapshot"]["status"]["current_equity"] == "9999.45"


def test_keeps_only_the_most_recent_fills_but_totals_all_of_them() -> None:
    fills = [_fill(f"2026-09-26T10:3{i}:00Z", f"T-{i}", "0.10 USDT") for i in range(5)]

    summary = telemetry_read.summarize(fills, recent=2)

    assert summary["fill_count"] == 5
    assert [fill["trade_id"] for fill in summary["fills"]] == ["T-3", "T-4"]
    assert summary["fees"] == {"USDT": "0.50"}


def test_totals_realised_results_and_fees_per_currency_without_floats() -> None:
    summary = telemetry_read.summarize(
        [
            _fill("2026-09-26T10:33:00Z", "T-1", "0.1 USDT"),
            _fill("2026-09-26T10:34:00Z", "T-2", "0.2 USDT"),
            _fill("2026-09-26T10:35:00Z", "T-3", "0.00001 BNB"),
            _envelope("position_closed", "2026-09-26T10:36:00Z", realized_pnl="3.10 USDT"),
            _envelope("position_closed", "2026-09-26T10:37:00Z", realized_pnl="-1.05 USDT"),
        ]
    )

    assert summary["fees"] == {"USDT": "0.3", "BNB": "0.00001"}
    assert summary["realized_pnl"] == {"USDT": "2.05"}
    assert summary["closed_positions"] == 2
    assert Decimal(summary["fees"]["USDT"]) == Decimal("0.3")


def test_an_amount_without_a_currency_is_counted_apart() -> None:
    summary = telemetry_read.summarize([_fill("2026-09-26T10:33:00Z", "T-1", "0.1")])

    assert summary["fees"] == {"": "0.1"}


def test_an_unreadable_amount_is_skipped_and_counted() -> None:
    summary = telemetry_read.summarize([_fill("2026-09-26T10:33:00Z", "T-1", "n/a USDT")])

    assert summary["fees"] == {}
    assert summary["unreadable_amounts"] == 1


def test_nothing_published_is_an_empty_summary() -> None:
    summary = telemetry_read.summarize([])

    assert summary["snapshots"] == 0
    assert summary["first_snapshot"] is None
    assert summary["latest_snapshot"] is None
    assert summary["fills"] == []


def test_the_subject_covers_every_kind_for_one_run() -> None:
    assert (
        telemetry_read.subject("local", "local-supertrend", "supertrend-sandbox")
        == "arx.local.telemetry.local-supertrend.supertrend-sandbox.>"
    )


def test_the_baseline_is_the_first_snapshot_the_runner_could_value() -> None:
    starting = _snapshot("2026-09-26T10:53:12Z", "0")
    starting["payload"]["status"].update(reliable=False, unreliable_reason="venue_unavailable")

    summary = telemetry_read.summarize(
        [
            starting,
            _snapshot("2026-09-26T10:53:22Z", "10000"),
            _snapshot("2026-09-26T10:53:32Z", "10005"),
        ]
    )

    assert summary["first_snapshot"]["occurred_at"] == "2026-09-26T10:53:22Z"
    assert summary["latest_snapshot"]["status"]["current_equity"] == "10005"


def test_no_valued_snapshot_means_no_baseline() -> None:
    starting = _snapshot("2026-09-26T10:53:12Z", "0")
    starting["payload"]["status"].update(reliable=False, unreliable_reason="venue_unavailable")

    summary = telemetry_read.summarize([starting])

    assert summary["first_snapshot"] is None
    assert summary["latest_snapshot"]["occurred_at"] == "2026-09-26T10:53:12Z"
