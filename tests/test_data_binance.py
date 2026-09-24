import pytest

from tools.data import binance
from tools.data.common import DataError

FUTURES_ENTRY = {
    "symbol": "BTCUSDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "marginAsset": "USDT",
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "tickSize": "0.10",
            "minPrice": "556.80",
            "maxPrice": "4529764",
        },
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MIN_NOTIONAL", "notional": "50"},
    ],
}
SPOT_ENTRY = {
    "symbol": "ETHUSDT",
    "baseAsset": "ETH",
    "quoteAsset": "USDT",
    "filters": [
        {
            "filterType": "PRICE_FILTER",
            "tickSize": "0.01000000",
            "minPrice": "0.01000000",
            "maxPrice": "1000000.00000000",
        },
        {
            "filterType": "LOT_SIZE",
            "stepSize": "0.00010000",
            "minQty": "0.00010000",
            "maxQty": "9000.00000000",
        },
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


def test_futures_rules_use_the_margin_asset_and_notional_field() -> None:
    rules = binance.instrument_rules(binance.PERPETUAL, "BTCUSDT", FUTURES_ENTRY)
    assert rules["settlement"] == "USDT"
    assert rules["tick_size"] == "0.1"
    assert rules["min_notional"] == "50"
    assert rules["market"] == "perpetual"


def test_spot_rules_strip_trailing_zeros_and_read_min_notional() -> None:
    rules = binance.instrument_rules(binance.SPOT, "ETHUSDT", SPOT_ENTRY)
    assert rules["tick_size"] == "0.01"
    assert rules["step_size"] == "0.0001"
    assert rules["min_notional"] == "5"
    assert rules["settlement"] == "USDT"


def test_a_symbol_without_price_rules_is_refused() -> None:
    entry = dict(FUTURES_ENTRY, filters=[])
    with pytest.raises(DataError, match="PRICE_FILTER"):
        binance.instrument_rules(binance.PERPETUAL, "BTCUSDT", entry)


def test_pairs_become_exchange_symbols() -> None:
    assert binance.symbol_for("binance_perpetual", "btc-usdt") == "BTCUSDT"


def test_an_unreachable_api_is_reported_as_a_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(DataError, match="could not reach"):
        binance.fetch_rules("binance_perpetual", "BTC-USDT")


def test_a_restricted_location_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    def restricted(request, *_args, **_kwargs):
        raise urllib.error.HTTPError(request.full_url, 451, "Unavailable", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", restricted)
    with pytest.raises(DataError, match="Binance does not serve this location"):
        binance.fetch_rules("binance_perpetual", "BTC-USDT")
