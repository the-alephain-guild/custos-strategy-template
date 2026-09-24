"""Public SoDEX klines and trading rules, for the sodex and sodex_perpetual connectors.

No account is needed. Spot and perpetuals are separate engines at SoDEX, each with
its own listing: spot pairs are the venue's v-prefixed tokens joined by an
underscore (vBTC_vUSDC) and perpetuals are quoted in USD (BTC-USD). Pairs are used
exactly as the venue lists them; there is no translation to get wrong.
"""

from __future__ import annotations

import time

from tools.data.common import DataError, Kline, decimal_text, get_json

NAME = "SoDEX"
BASE_URL = "https://mainnet-gw.sodex.dev/api/v1"

CONNECTORS = {"sodex": "spot", "sodex_perpetual": "perpetual"}
ENGINE = {"sodex": "spot", "sodex_perpetual": "perps"}
PAGE_LIMIT = {"sodex": 1500, "sodex_perpetual": 1000}
# The perpetuals engine offers fewer candle sizes than spot.
INTERVALS_BY_CONNECTOR = {
    "sodex": frozenset({"1m", "3m", "5m", "15m", "30m", "1h", "4h", "6h", "12h", "1d"}),
    "sodex_perpetual": frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"}),
}


def symbol_for(connector: str, pair: str) -> str:
    del connector
    return pair


def _data(payload: object, url: str) -> object:
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise DataError(f"SoDEX answered {url} with an error: {payload}")
    return payload["data"]


def _limit(value: str) -> str | None:
    """SoDEX writes an absent limit as 0."""
    return None if value in ("", "0") else decimal_text(value)


def instrument_rules(connector: str, raw: dict) -> dict[str, object]:
    """Reduce one SoDEX symbol entry to the fields a backtest needs."""
    return {
        "symbol": raw["name"],
        "market": CONNECTORS[connector],
        "base": raw["baseCoin"],
        # A perpetual is named in USD but margined and settled in the quote coin. The
        # backtest prices it in that coin too: a USD quote would need a USD/vUSDC rate
        # the simulated account has no source for.
        "quote": raw["quoteCoin"],
        "settlement": raw["quoteCoin"],
        "tick_size": decimal_text(raw["tickSize"]),
        "min_price": _limit(raw["minPrice"]),
        "max_price": _limit(raw["maxPrice"]),
        "step_size": decimal_text(raw["stepSize"]),
        "min_qty": decimal_text(raw["minQuantity"]),
        "max_qty": _limit(raw["maxQuantity"]) or "1000000000",
        "min_notional": decimal_text(raw["minNotional"]),
    }


def fetch_rules(connector: str, pair: str) -> dict[str, object]:
    url = f"{BASE_URL}/{ENGINE[connector]}/markets/symbols"
    for raw in _data(get_json(url, {}, NAME), url):
        if raw.get("name") == pair:
            return instrument_rules(connector, raw)
    raise DataError(f"{pair} is not listed on SoDEX {CONNECTORS[connector]}")


def fetch_klines(
    connector: str, pair: str, interval: str, start_ms: int, end_ms: int
) -> list[Kline]:
    """Pages backwards from end_ms: SoDEX returns klines newest first."""
    url = f"{BASE_URL}/{ENGINE[connector]}/markets/{pair}/klines"
    rows: dict[int, Kline] = {}
    cursor = end_ms
    while cursor > start_ms:
        batch = _data(
            get_json(
                url,
                {
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": cursor,
                    "limit": PAGE_LIMIT[connector],
                },
                NAME,
            ),
            url,
        )
        if not batch:
            break
        for row in batch:
            if start_ms <= row["t"] < end_ms:
                rows[row["t"]] = (row["t"], row["o"], row["h"], row["l"], row["c"], row["v"])
        oldest = min(row["t"] for row in batch)
        if oldest >= cursor:
            break
        cursor = oldest - 1
        time.sleep(0.1)
    return [rows[t] for t in sorted(rows)]
