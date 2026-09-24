"""OKX translations, checked against entries the public API returned on 2026-09-24."""

from decimal import Decimal

import pytest

from tools.data import okx
from tools.data.common import DataError

SWAP_ENTRY = {
    "instId": "BTC-USDT-SWAP",
    "instType": "SWAP",
    "ctType": "linear",
    "ctVal": "0.01",
    "settleCcy": "USDT",
    "tickSz": "0.1",
    "lotSz": "0.01",
    "minSz": "0.01",
    "maxLmtSz": "100000000",
}
SPOT_ENTRY = {
    "instId": "ETH-USDT",
    "instType": "SPOT",
    "ctType": "",
    "ctVal": "",
    "settleCcy": "",
    "tickSz": "0.01",
    "lotSz": "0.000001",
    "minSz": "0.0001",
    "maxLmtSz": "999999999999",
}
# [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
SWAP_CANDLE = ["1790229600000", "83932.6", "84187.6", "83872.1", "84068.5",
               "120528.84", "1205.2884", "101335643.16414", "1"]  # fmt: skip
SPOT_CANDLE = ["1790233200000", "2686.12", "2691.07", "2679.64", "2691.07",
               "5203.57571", "13974143.90443629", "13974143.90443629", "1"]  # fmt: skip


def test_swap_sizes_are_converted_from_contracts_to_the_base_asset() -> None:
    rules = okx.instrument_rules("okx_perpetual", SWAP_ENTRY)
    assert (rules["base"], rules["quote"], rules["settlement"]) == ("BTC", "USDT", "USDT")
    # One contract is 0.01 BTC, so a lot of 0.01 contracts is 0.0001 BTC.
    assert rules["step_size"] == "0.0001"
    assert rules["min_qty"] == "0.0001"
    assert rules["market"] == "perpetual"


def test_spot_sizes_are_already_in_the_base_asset() -> None:
    rules = okx.instrument_rules("okx", SPOT_ENTRY)
    assert (rules["base"], rules["quote"], rules["settlement"]) == ("ETH", "USDT", "USDT")
    assert rules["step_size"] == "0.000001"
    assert rules["min_price"] is None


def test_an_inverse_swap_is_refused() -> None:
    with pytest.raises(DataError, match="inverse"):
        okx.instrument_rules("okx_perpetual", dict(SWAP_ENTRY, ctType="inverse"))


def test_candle_volume_is_taken_in_the_base_asset() -> None:
    # A swap reports contracts in vol and BTC in volCcy; spot reports ETH in vol.
    assert okx.kline("okx_perpetual", SWAP_CANDLE)[5] == "1205.2884"
    assert okx.kline("okx", SPOT_CANDLE)[5] == "5203.57571"
    assert Decimal(SWAP_CANDLE[5]) * Decimal("0.01") == Decimal("1205.2884")


def test_pairs_become_okx_instrument_ids() -> None:
    assert okx.symbol_for("okx_perpetual", "btc-usdt") == "BTC-USDT-SWAP"
    assert okx.symbol_for("okx", "ETH-USDT") == "ETH-USDT"


def test_long_candles_are_requested_aligned_to_utc() -> None:
    assert okx.BARS["1d"] == "1Dutc"
    assert okx.BARS["1h"] == "1H"


def test_candles_are_paged_backwards_and_unconfirmed_ones_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hour = 3_600_000
    pages = [
        [[str(5 * hour), "1", "1", "1", "1", "1", "1", "1", "0"],
         [str(4 * hour), "1", "1", "1", "1", "1", "1", "1", "1"],
         [str(3 * hour), "1", "1", "1", "1", "1", "1", "1", "1"]],
        [[str(2 * hour), "1", "1", "1", "1", "1", "1", "1", "1"],
         [str(1 * hour), "1", "1", "1", "1", "1", "1", "1", "1"]],
        [],
    ]  # fmt: skip
    cursors = []

    def fake_get(url, params, exchange):
        cursors.append(params["after"])
        return {"code": "0", "data": pages[len(cursors) - 1]}

    monkeypatch.setattr(okx, "get_json", fake_get)
    monkeypatch.setattr(okx.time, "sleep", lambda _s: None)
    rows = okx.fetch_klines("okx_perpetual", "BTC-USDT", "1h", 2 * hour, 6 * hour)
    assert [row[0] for row in rows] == [2 * hour, 3 * hour, 4 * hour]
    assert cursors == [6 * hour, 3 * hour]


def test_an_okx_error_code_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        okx, "get_json", lambda *_a: {"code": "51001", "msg": "Instrument ID does not exist"}
    )
    with pytest.raises(DataError, match="Instrument ID does not exist"):
        okx.fetch_rules("okx_perpetual", "NOPE-USDT")
