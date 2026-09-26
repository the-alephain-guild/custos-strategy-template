import io
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tools import ui
from tools.runner import report

# A reader eight hours east of UTC, so local times differ from the stamps.
LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 26, 10, 54, 26, tzinfo=UTC).astimezone(LOCAL)
RUNNER = report.RunnerInfo(
    started_at="2026-09-26T10:42:11Z",
    image="custos-runner:dev",
    revision="27dfb0ade82a03627f65e081880eb88d0e1ed0d2",
)


def _summary(**overrides) -> dict:
    summary = {
        "runner_publishes": True,
        "snapshots": 57,
        "first_snapshot": {
            "occurred_at": "2026-09-26T10:42:22.123456789Z",
            "status": {"current_equity": "10000.00000000", "reliable": True},
        },
        "latest_snapshot": {
            "occurred_at": "2026-09-26T10:54:22.454236091Z",
            "status": {
                "phase": "running",
                "position_count": 1,
                "order_count": 1,
                "open_notional": "925.14939",
                "peak_equity": "10000.00000000",
                "current_equity": "9999.64827335",
                "drawdown_pct": "0.003517266500",
                "reliable": True,
                "unreliable_reason": None,
            },
            "positions": [
                {
                    "instrument_id": "BTCUSDT-PERP.BINANCE",
                    "quantity": "-0.011",
                    "avg_px": "84097.40000000001",
                    "unrealized_pnl": "0.11088000",
                    "notional": "925.10242",
                }
            ],
            "orders": [
                {
                    "client_order_id": "5f2240206f86474ab3e2b30ccd6b2aa7",
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
                "ts_event": "2026-09-25T09:10:00.000000000Z",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "order_side": "BUY",
                "last_qty": "0.011",
                "last_px": "84000",
                "commission": "0.462 USDT",
            },
            {
                "ts_event": "2026-09-26T10:54:00.441954844Z",
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "order_side": "SELL",
                "last_qty": "0.011",
                "last_px": "84110.30",
                "commission": "0.46260665 USDT",
            },
        ],
        "fill_count": 2,
        "closed_positions": 1,
        "realized_pnl": {"USDT": "-1.2"},
        "fees": {"USDT": "0.92460665"},
        "unreadable_amounts": 0,
    }
    summary.update(overrides)
    return summary


def _plain(capsys, monkeypatch, summary: dict | None = None, runner=RUNNER) -> str:
    monkeypatch.setattr(ui, "Console", None)
    report.render(
        _summary() if summary is None else summary,
        strategy="trend/supertrend",
        mode="sandbox",
        runner=runner,
        now=NOW,
    )
    captured = capsys.readouterr()
    return captured.out + captured.err


# Formatting rules


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10000.00000000", "10,000.00"),
        ("9999.64827335", "9,999.65"),
        ("-0.35172665", "-0.35"),
        ("0", "0.00"),
        ("1234567.891", "1,234,567.89"),
    ],
)
def test_amounts_have_thousands_separators_and_two_places(text, expected) -> None:
    assert report.money(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("84097.40000000001", "84,097.4"),
        ("84110.30", "84,110.3"),
        ("0.011", "0.011"),
        ("-0.011", "-0.011"),
        ("84000", "84,000"),
        ("0.00000001", "0.00000001"),
    ],
)
def test_prices_and_sizes_drop_float_tails_and_trailing_zeros(text, expected) -> None:
    assert report.number(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0.003517266500", "<0.01%"), ("0", "0.00%"), ("12.345", "12.35%"), ("-0.001", "<0.01%")],
)
def test_percentages_have_two_places_and_say_when_they_are_below_that(text, expected) -> None:
    assert report.percent(text) == expected


def test_a_fee_keeps_its_currency() -> None:
    assert report.fee("0.46260665 USDT") == "0.46260665 USDT"
    assert report.fee("0.10000000 BNB") == "0.1 BNB"
    assert report.fee("n/a") == "n/a"


@pytest.mark.parametrize(
    ("first", "latest", "expected", "tone"),
    [
        ("10000", "10100", "▲ +100.00 (+1.00%)", "up"),
        ("10000.00000000", "9999.64827335", "▼ -0.35 (<0.01%)", "down"),
        ("10000.00000000", "10000.00000000", "0.00 (0.00%)", ""),
        ("0", "5", "▲ +5.00", "up"),
    ],
)
def test_change_is_signed_toned_and_in_percent(first, latest, expected, tone) -> None:
    assert report.change(first, latest) == ui.Cell(expected, tone)


def test_times_are_local_and_carry_the_date_only_when_it_is_not_today() -> None:
    assert report.when("2026-09-26T10:54:00.441954844Z", NOW) == "18:54:00"
    assert report.when("2026-09-25T09:10:00Z", NOW) == "09-25 17:10:00"
    assert report.when(None, NOW) == "unknown"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(4, "4s"), (65, "1m 05s"), (3 * 3600 + 7 * 60, "3h 07m"), (2 * 86400 + 3600, "2d 1h")],
)
def test_durations_read_as_a_person_would_say_them(seconds, expected) -> None:
    assert report.duration(timedelta(seconds=seconds)) == expected


def test_long_order_ids_are_shortened() -> None:
    assert report.short_id("5f2240206f86474ab3e2b30ccd6b2aa7") == "5f224020…"
    assert report.short_id("O-1") == "O-1"


def test_the_runner_is_named_by_image_and_revision() -> None:
    assert report.runner_label(RUNNER) == "custos-runner:dev @ 27dfb0a"
    pinned = report.RunnerInfo(
        started_at="", image="ghcr.io/org/custos:v0.3.0@sha256:5383136e", revision="415abf1c0ffee"
    )
    assert report.runner_label(pinned) == "custos:v0.3.0 @ 415abf1"


# The report as a whole


def test_the_report_carries_every_figure(capsys, monkeypatch) -> None:
    out = _plain(capsys, monkeypatch)

    assert "supertrend · sandbox" in out
    assert "running for 12m 15s" in out and "updated 4s ago" in out
    assert "custos-runner:dev @ 27dfb0a" in out
    assert "· BINANCE ·" in out and "BTCUSDT-PERP " in out and "PERP.BINANCE" not in out
    assert "9,999.65" in out and "▼ -0.35 (<0.01%)" in out and "<0.01%" in out
    assert "SHORT" in out and "84,097.4" in out and "+0.11" in out
    assert "5f224020…" in out and "—" in out  # an order with no limit price
    assert "18:54:00" in out and "09-25 17:10:00" in out
    assert "0.46260665 USDT" in out
    assert "-1.20 USDT" in out and "0.92460665 USDT" in out


def test_rich_output_colours_gains_and_losses(monkeypatch) -> None:
    from rich.console import Console

    console = Console(record=True, width=140, force_terminal=True, color_system="standard")
    monkeypatch.setattr(ui, "_console", lambda stream_is_err: console)
    report.render(_summary(), strategy="trend/supertrend", mode="sandbox", runner=RUNNER, now=NOW)

    styled = console.export_text(styles=True, clear=False)
    assert "31m▼ -0.35" in styled  # loss in red, bold as every headline figure
    assert "\x1b[31mSHORT" in styled
    assert "\x1b[32m+0.11" in styled  # unrealised gain in green
    assert "84,097.4" in console.export_text()


def test_an_unreliable_snapshot_says_why_and_shows_no_positions(capsys, monkeypatch) -> None:
    summary = _summary(first_snapshot=None)
    summary["latest_snapshot"]["status"].update(reliable=False, unreliable_reason="missing_price")
    summary["latest_snapshot"]["positions"] = []

    out = _plain(capsys, monkeypatch, summary)

    assert "missing_price" in out and "positions are left out" in out
    assert "not known yet" in out


def test_a_runner_that_does_not_publish_is_named_as_such(capsys, monkeypatch) -> None:
    summary = _summary(runner_publishes=False, snapshots=0, first_snapshot=None,
                       latest_snapshot=None, fills=[], fill_count=0, fees={})  # fmt: skip

    out = _plain(capsys, monkeypatch, summary)

    assert "does not publish" in out and "TOOLCHAIN=dev" in out


def test_no_snapshot_yet_says_when_the_first_one_comes(capsys, monkeypatch) -> None:
    summary = _summary(snapshots=0, first_snapshot=None, latest_snapshot=None,
                       fills=[], fill_count=0, fees={})  # fmt: skip

    assert "10 seconds" in _plain(capsys, monkeypatch, summary)


def test_dropped_events_are_warned_about(capsys, monkeypatch) -> None:
    summary = _summary()
    summary["latest_snapshot"]["dropped_events"] = 3

    assert "3 fills or closed positions were not reported" in _plain(capsys, monkeypatch, summary)


def test_a_run_that_is_not_up_says_so_and_how_to_start_it(capsys, monkeypatch) -> None:
    monkeypatch.setattr(ui, "Console", None)
    report.render_not_running(strategy="trend/supertrend", mode="testnet", toolchain="dev")

    out = capsys.readouterr().out
    assert "not running" in out
    assert "make start STRATEGY=trend/supertrend MODE=testnet TOOLCHAIN=dev" in out


# The command line


def test_json_prints_the_summary_with_the_runner(monkeypatch, capsys) -> None:
    document = _summary()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(document)))

    args = ["report.py", "--strategy", "trend/x", "--mode", "sandbox", "--json",
            "--started-at", RUNNER.started_at, "--image", RUNNER.image,
            "--revision", RUNNER.revision]  # fmt: skip
    assert report.main(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["snapshots"] == 57
    assert printed["runner"] == {
        "started_at": RUNNER.started_at,
        "image": RUNNER.image,
        "revision": RUNNER.revision,
    }


def test_json_for_a_run_that_is_not_up(monkeypatch, capsys) -> None:
    assert report.main(["report.py", "--strategy", "trend/x", "--mode", "sandbox",
                        "--not-running", "--json"]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out) == {"running": False}


def test_output_that_is_not_a_summary_is_refused(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("Traceback: boom"))

    assert report.main(["report.py", "--strategy", "trend/x", "--mode", "sandbox"]) == 1
