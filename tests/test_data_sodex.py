"""SoDEX translations, checked against entries the public API returned on 2026-09-24."""

import pytest

from tools.data import sodex
from tools.data.common import DataError

PERPS_ENTRY = {
    "name": "BTC-USD",
    "baseCoin": "BTC",
    "quoteCoin": "vUSDC",
    "tickSize": "1",
    "minPrice": "1",
    "maxPrice": "0",
    "stepSize": "0.00001",
    "minQuantity": "0.00001",
    "maxQuantity": "1000",
    "minNotional": "10",
}
SPOT_ENTRY = {
    "name": "vBTC_vUSDC",
    "baseCoin": "vBTC",
    "quoteCoin": "vUSDC",
    "tickSize": "1",
    "minPrice": "1",
    "maxPrice": "0",
    "stepSize": "0.00001",
    "minQuantity": "0.00001",
    "maxQuantity": "0",
    "minNotional": "5",
}


def test_a_perpetual_is_priced_and_settled_in_its_quote_coin() -> None:
    rules = sodex.instrument_rules("sodex_perpetual", PERPS_ENTRY)
    assert (rules["symbol"], rules["base"], rules["quote"]) == ("BTC-USD", "BTC", "vUSDC")
    assert rules["settlement"] == "vUSDC"
    assert rules["market"] == "perpetual"


def test_limits_sodex_leaves_at_zero_are_absent() -> None:
    rules = sodex.instrument_rules("sodex", SPOT_ENTRY)
    assert rules["max_price"] is None
    assert rules["min_price"] == "1"
    assert rules["max_qty"] == "1000000000"
    assert rules["min_notional"] == "5"


def test_pairs_are_used_as_the_venue_lists_them() -> None:
    assert sodex.symbol_for("sodex", "vBTC_vUSDC") == "vBTC_vUSDC"


def test_klines_are_paged_backwards_until_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    hour = 3_600_000

    def row(t):
        return {"t": t, "o": "1", "h": "1", "l": "1", "c": "1", "v": "2", "q": "2", "n": 1}

    pages = [[row(4 * hour), row(3 * hour)], [row(2 * hour), row(1 * hour)]]
    ends = []

    def fake_get(url, params, exchange):
        ends.append(params["endTime"])
        return {"code": 0, "data": pages[len(ends) - 1] if len(ends) <= len(pages) else []}

    monkeypatch.setattr(sodex, "get_json", fake_get)
    monkeypatch.setattr(sodex.time, "sleep", lambda _s: None)
    rows = sodex.fetch_klines("sodex_perpetual", "BTC-USD", "1h", 2 * hour, 5 * hour)
    assert [r[0] for r in rows] == [2 * hour, 3 * hour, 4 * hour]
    assert rows[0][5] == "2"
    assert ends == [5 * hour, 3 * hour - 1]


def test_an_unlisted_pair_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sodex, "get_json", lambda *_a: {"code": 0, "data": [PERPS_ENTRY]})
    with pytest.raises(DataError, match="ETH-USDT is not listed on SoDEX perpetual"):
        sodex.fetch_rules("sodex_perpetual", "ETH-USDT")
