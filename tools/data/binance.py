"""Public Binance klines and trading rules, for the binance and binance_perpetual connectors.

No account is needed: klines and exchangeInfo are public. Spot data comes from the
spot API and perpetual data from the USDⓈ-M futures API, because the two markets
have different prices, fees and trading rules.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from tools.data.common import DataError, Kline, decimal_text, get_json

NAME = "Binance"
INTERVALS = frozenset({"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"})


@dataclass(frozen=True)
class Market:
    name: str
    klines_url: str
    exchange_info_url: str
    page_limit: int


SPOT = Market(
    "spot",
    "https://api.binance.com/api/v3/klines",
    "https://api.binance.com/api/v3/exchangeInfo",
    1000,
)
PERPETUAL = Market(
    "perpetual",
    "https://fapi.binance.com/fapi/v1/klines",
    "https://fapi.binance.com/fapi/v1/exchangeInfo",
    1000,
)
CONNECTORS = {"binance": SPOT, "binance_perpetual": PERPETUAL}


def symbol_for(connector: str, pair: str) -> str:
    del connector
    return pair.replace("-", "").upper()


def instrument_rules(market: Market, symbol: str, raw: dict) -> dict[str, object]:
    """Reduce one exchangeInfo symbol entry to the fields a backtest needs."""
    filters = {f["filterType"]: f for f in raw.get("filters", [])}
    try:
        price = filters["PRICE_FILTER"]
        lot = filters["LOT_SIZE"]
    except KeyError as missing:
        raise DataError(f"{symbol} has no {missing.args[0]} filter in exchangeInfo") from None
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    min_notional = notional.get("notional") or notional.get("minNotional") or "0"
    return {
        "symbol": symbol,
        "market": market.name,
        "base": raw["baseAsset"],
        "quote": raw["quoteAsset"],
        "settlement": raw.get("marginAsset", raw["quoteAsset"]),
        "tick_size": decimal_text(price["tickSize"]),
        "min_price": decimal_text(price["minPrice"]),
        "max_price": decimal_text(price["maxPrice"]),
        "step_size": decimal_text(lot["stepSize"]),
        "min_qty": decimal_text(lot["minQty"]),
        "max_qty": decimal_text(lot["maxQty"]),
        "min_notional": decimal_text(min_notional),
    }


def fetch_rules(connector: str, pair: str) -> dict[str, object]:
    market, symbol = CONNECTORS[connector], symbol_for(connector, pair)
    params = {"symbol": symbol} if market is SPOT else {}
    info = get_json(market.exchange_info_url, params, NAME)
    for raw in info.get("symbols", []):
        if raw.get("symbol") == symbol:
            return instrument_rules(market, symbol, raw)
    raise DataError(f"{symbol} is not listed on Binance {market.name}")


def fetch_klines(
    connector: str, pair: str, interval: str, start_ms: int, end_ms: int
) -> list[Kline]:
    market, symbol = CONNECTORS[connector], symbol_for(connector, pair)
    rows: list[Kline] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = get_json(
            market.klines_url,
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": market.page_limit,
            },
            NAME,
        )
        if not batch:
            break
        rows.extend((int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in batch)
        cursor = int(batch[-1][0]) + 1
        time.sleep(0.1)
    return rows
