import pytest

from tools.data import binance

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
    with pytest.raises(binance.DataError, match="PRICE_FILTER"):
        binance.instrument_rules(binance.PERPETUAL, "BTCUSDT", entry)


def test_connectors_map_to_their_own_market() -> None:
    assert binance.market_for("binance") is binance.SPOT
    assert binance.market_for("binance_perpetual") is binance.PERPETUAL
    with pytest.raises(binance.DataError, match="no public data source"):
        binance.market_for("elsewhere")


def test_bar_types_map_to_binance_intervals() -> None:
    assert binance.interval_for("1-HOUR") == "1h"
    assert binance.interval_for("15-minute") == "15m"
    with pytest.raises(binance.DataError, match="no Binance interval"):
        binance.interval_for("7-MINUTE")


def test_pairs_become_exchange_symbols() -> None:
    assert binance.symbol_for("btc-usdt") == "BTCUSDT"


def test_an_unreachable_api_is_reported_as_a_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(binance.urllib.request, "urlopen", refuse)
    with pytest.raises(binance.DataError, match="could not reach"):
        binance.fetch_instrument(binance.PERPETUAL, "BTCUSDT")


def test_a_restricted_location_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def restricted(url, *_args, **_kwargs):
        raise urllib.error.HTTPError(url, 451, "Unavailable", {}, None)

    monkeypatch.setattr(binance.urllib.request, "urlopen", restricted)
    with pytest.raises(binance.DataError, match="does not serve this location"):
        binance.fetch_instrument(binance.PERPETUAL, "BTCUSDT")
