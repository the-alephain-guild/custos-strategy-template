"""Public OKX candles and trading rules, for the okx and okx_perpetual connectors.

No account is needed. Two OKX conventions are translated here so the backtest
never sees them:

- A perpetual swap is sized in contracts, each worth ctVal of the base asset. The
  rules and the volume are converted to base-asset units, the unit a strategy
  sizes in.
- OKX aligns 6-hour, 12-hour and daily candles to Hong Kong time unless the
  interval carries a "utc" suffix. The UTC variants are requested, so a daily bar
  opens at midnight UTC as it does on the other exchanges.
"""

from __future__ import annotations

import time
from decimal import Decimal

from tools.data.common import DataError, Kline, decimal_text, get_json

NAME = "OKX"
CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
PAGE_LIMIT = 100

CONNECTORS = {"okx": "spot", "okx_perpetual": "perpetual"}
BARS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6Hutc",
    "12h": "12Hutc",
    "1d": "1Dutc",
}
INTERVALS = frozenset(BARS)


def symbol_for(connector: str, pair: str) -> str:
    symbol = pair.upper().replace("/", "-").removesuffix("-SWAP")
    return f"{symbol}-SWAP" if CONNECTORS[connector] == "perpetual" else symbol


def _data(payload: object, url: str) -> list:
    if not isinstance(payload, dict) or payload.get("code") != "0":
        message = payload.get("msg") if isinstance(payload, dict) else payload
        raise DataError(f"OKX answered {url} with an error: {message}")
    return payload["data"]


def instrument_rules(connector: str, raw: dict) -> dict[str, object]:
    """Reduce one OKX instrument entry to the fields a backtest needs, in base units."""
    perpetual = CONNECTORS[connector] == "perpetual"
    contract = Decimal(raw["ctVal"]) if perpetual else Decimal(1)
    if perpetual and raw.get("ctType") != "linear":
        raise DataError(f"{raw['instId']} is an inverse swap; only linear swaps are supported")
    base, quote = raw["instId"].removesuffix("-SWAP").split("-")
    return {
        "symbol": raw["instId"],
        "market": CONNECTORS[connector],
        "base": base,
        "quote": quote,
        "settlement": raw["settleCcy"] if perpetual else quote,
        "tick_size": decimal_text(raw["tickSz"]),
        "min_price": None,
        "max_price": None,
        "step_size": decimal_text(Decimal(raw["lotSz"]) * contract),
        "min_qty": decimal_text(Decimal(raw["minSz"]) * contract),
        "max_qty": decimal_text(Decimal(raw["maxLmtSz"]) * contract),
        "min_notional": "0",
    }


def fetch_rules(connector: str, pair: str) -> dict[str, object]:
    symbol = symbol_for(connector, pair)
    kind = "SWAP" if CONNECTORS[connector] == "perpetual" else "SPOT"
    entries = _data(
        get_json(INSTRUMENTS_URL, {"instType": kind, "instId": symbol}, NAME), INSTRUMENTS_URL
    )
    if not entries:
        raise DataError(f"{symbol} is not listed on OKX {CONNECTORS[connector]}")
    return instrument_rules(connector, entries[0])


def kline(connector: str, row: list) -> Kline:
    """One candle as [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm].

    vol counts contracts on a swap and base units on spot; volCcy is the base-asset
    amount on a swap.
    """
    volume = row[6] if CONNECTORS[connector] == "perpetual" else row[5]
    return (int(row[0]), row[1], row[2], row[3], row[4], volume)


def fetch_klines(
    connector: str, pair: str, interval: str, start_ms: int, end_ms: int
) -> list[Kline]:
    """Pages backwards from end_ms: OKX returns candles newest first, older than `after`."""
    symbol = symbol_for(connector, pair)
    rows: dict[int, Kline] = {}
    cursor = end_ms
    while cursor > start_ms:
        batch = _data(
            get_json(
                CANDLES_URL,
                {"instId": symbol, "bar": BARS[interval], "after": cursor, "limit": PAGE_LIMIT},
                NAME,
            ),
            CANDLES_URL,
        )
        if not batch:
            break
        for row in batch:
            # A candle still forming says so; it is left for a later run.
            if row[8] == "1" and int(row[0]) >= start_ms:
                rows[int(row[0])] = kline(connector, row)
        cursor = int(batch[-1][0])
        time.sleep(0.12)
    return [rows[t] for t in sorted(rows)]
