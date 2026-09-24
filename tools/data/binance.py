"""Download public Binance klines and instrument rules for backtesting.

No account is needed: both klines and exchangeInfo are public endpoints. Spot
data comes from the spot API and perpetual data from the USD-M futures API,
because the two markets have different prices, fees and trading rules.

Files are written under .data/<market>/:
    <SYMBOL>_<interval>.csv        open_time_ms,open,high,low,close,volume
    <SYMBOL>.instrument.json       tick size, step size and limits for the symbol

Open times are stored as integer milliseconds so there is no doubt about which
end of the bar a timestamp refers to; the backtest stamps each bar at its close.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / ".data"

BAR_TYPE_TO_INTERVAL = {
    "1-MINUTE": "1m",
    "3-MINUTE": "3m",
    "5-MINUTE": "5m",
    "15-MINUTE": "15m",
    "30-MINUTE": "30m",
    "1-HOUR": "1h",
    "2-HOUR": "2h",
    "4-HOUR": "4h",
    "6-HOUR": "6h",
    "12-HOUR": "12h",
    "1-DAY": "1d",
}


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
CONNECTOR_TO_MARKET = {"binance": SPOT, "binance_perpetual": PERPETUAL}


class DataError(RuntimeError):
    pass


def market_for(connector: str) -> Market:
    try:
        return CONNECTOR_TO_MARKET[connector]
    except KeyError:
        supported = ", ".join(sorted(CONNECTOR_TO_MARKET))
        raise DataError(
            f"no public data source for connector {connector!r}; use {supported}"
        ) from None


def interval_for(bar_type: str) -> str:
    try:
        return BAR_TYPE_TO_INTERVAL[bar_type.upper()]
    except KeyError:
        supported = ", ".join(BAR_TYPE_TO_INTERVAL)
        raise DataError(
            f"bar {bar_type!r} has no Binance interval; use one of {supported}"
        ) from None


def symbol_for(pair: str) -> str:
    return pair.replace("-", "").upper()


def _get_json(url: str, params: dict[str, object]) -> object:
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}" if query else url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalized(value: str) -> str:
    text = format(Decimal(value).normalize(), "f")
    return text


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
        "tick_size": _normalized(price["tickSize"]),
        "min_price": _normalized(price["minPrice"]),
        "max_price": _normalized(price["maxPrice"]),
        "step_size": _normalized(lot["stepSize"]),
        "min_qty": _normalized(lot["minQty"]),
        "max_qty": _normalized(lot["maxQty"]),
        "min_notional": _normalized(min_notional),
    }


def fetch_instrument(market: Market, symbol: str) -> dict[str, object]:
    params = {"symbol": symbol} if market is SPOT else {}
    info = _get_json(market.exchange_info_url, params)
    for raw in info.get("symbols", []):
        if raw.get("symbol") == symbol:
            return instrument_rules(market, symbol, raw)
    raise DataError(f"{symbol} is not listed on Binance {market.name}")


def _fetch_klines(
    market: Market, symbol: str, interval: str, start_ms: int, end_ms: int
) -> list[list]:
    rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get_json(
            market.klines_url,
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": market.page_limit,
            },
        )
        if not batch:
            break
        rows.extend(batch)
        cursor = int(batch[-1][0]) + 1
        time.sleep(0.1)
    return rows


def paths_for(market: Market, symbol: str, interval: str) -> tuple[Path, Path]:
    folder = DATA_DIR / market.name
    return folder / f"{symbol}_{interval}.csv", folder / f"{symbol}.instrument.json"


def ensure_data(
    connector: str, pair: str, bar_type: str, start: datetime, end: datetime
) -> tuple[Path, Path]:
    """Download klines covering [start, end) and the symbol's rules, unless present."""
    market = market_for(connector)
    symbol = symbol_for(pair)
    interval = interval_for(bar_type)
    csv_path, rules_path = paths_for(market, symbol, interval)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not rules_path.is_file():
        rules_path.write_text(
            json.dumps(fetch_instrument(market, symbol), indent=2) + "\n", encoding="utf-8"
        )

    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if csv_path.is_file():
        with csv_path.open(newline="") as handle:
            times = [int(row["open_time_ms"]) for row in csv.DictReader(handle)]
        if times and times[0] <= start_ms and times[-1] >= end_ms - _interval_ms(interval):
            return csv_path, rules_path

    rows = _fetch_klines(market, symbol, interval, start_ms, end_ms)
    if not rows:
        raise DataError(f"Binance returned no {interval} klines for {symbol} in that range")
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["open_time_ms", "open", "high", "low", "close", "volume"])
        for row in rows:
            writer.writerow([int(row[0]), row[1], row[2], row[3], row[4], row[5]])
    return csv_path, rules_path


def _interval_ms(interval: str) -> int:
    unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[interval[-1]]
    return int(interval[:-1]) * unit
