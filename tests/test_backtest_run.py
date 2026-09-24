from pathlib import Path

import pytest

from tools.backtest import run

HOUR_NS = 3_600_000_000_000
RULES = {
    "symbol": "BTCUSDT",
    "market": "perpetual",
    "base": "BTC",
    "quote": "USDT",
    "settlement": "USDT",
    "tick_size": "0.1",
    "min_price": "556.8",
    "max_price": "4529764",
    "step_size": "0.001",
    "min_qty": "0.001",
    "max_qty": "1000",
    "min_notional": "50",
}


def _instrument():
    from decimal import Decimal

    return run.build_instrument("BTCUSDT-PERP.BINANCE", RULES, Decimal("0.0002"), Decimal("0.0004"))


def _csv(tmp_path: Path, *rows: tuple[int, str, str, str, str]) -> Path:
    path = tmp_path / "BTCUSDT_1h.csv"
    lines = ["open_time_ms,open,high,low,close,volume"]
    lines += [f"{t},{o},{h},{lo},{c},1" for t, o, h, lo, c in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_bar_is_stamped_at_its_close_and_its_open_quote_at_its_open(tmp_path: Path) -> None:
    # A bar stamped at its open would show its close price an hour early.
    from nautilus_trader.model import BarType

    open_ms = 1_735_689_600_000
    path = _csv(tmp_path, (open_ms, "100", "110", "90", "105"))
    instrument = _instrument()
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    open_ns = open_ms * 1_000_000

    quotes, bars = run.load_market_data(
        path, instrument, bar_type, HOUR_NS, open_ns, open_ns + HOUR_NS
    )

    assert [b.ts_event for b in bars] == [open_ns + HOUR_NS]
    assert [q.ts_event for q in quotes] == [open_ns]
    assert float(quotes[0].bid_price) == 100.0
    assert float(bars[0].close) == 105.0


def test_bars_that_end_after_the_window_are_left_out(tmp_path: Path) -> None:
    from nautilus_trader.model import BarType

    first_ms = 1_735_689_600_000
    path = _csv(
        tmp_path,
        (first_ms, "100", "110", "90", "105"),
        (first_ms + 3_600_000, "105", "115", "95", "110"),
    )
    instrument = _instrument()
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    start_ns = first_ms * 1_000_000

    _, bars = run.load_market_data(
        path, instrument, bar_type, HOUR_NS, start_ns, start_ns + HOUR_NS + HOUR_NS // 2
    )

    assert len(bars) == 1


def test_the_perpetual_instrument_follows_the_exchange_rules() -> None:
    instrument = _instrument()
    assert str(instrument.id) == "BTCUSDT-PERP.BINANCE"
    assert instrument.price_precision == 1
    assert instrument.size_precision == 3
    assert str(instrument.settlement_currency) == "USDT"


def test_an_instrument_without_price_bounds_or_a_known_currency_builds() -> None:
    # OKX publishes no price bounds; SoDEX settles in its own vUSDC token.
    from decimal import Decimal

    rules = dict(
        RULES, symbol="BTC-USD", quote="vUSDC", settlement="vUSDC", min_price=None, max_price=None
    )
    instrument = run.build_instrument("BTC-USD.SODEX_PERPS", rules, Decimal(0), Decimal(0))
    assert instrument.max_price is None
    assert str(instrument.settlement_currency) == "vUSDC"


@pytest.mark.parametrize(("increment", "digits"), [("0.1", 1), ("0.001", 3), ("1", 0), ("10", 0)])
def test_precision_is_read_from_the_increment(increment: str, digits: int) -> None:
    assert run.decimals(increment) == digits


def test_an_unknown_strategy_path_is_refused() -> None:
    with pytest.raises(SystemExit, match="no strategy"):
        run.resolve_strategy_dir("nowhere/at_all")
