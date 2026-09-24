"""What every market data source shares: errors, HTTP, the cache layout and bar sizes.

Files are written under .data/<connector>/, or under BACKTEST_DATA_DIR when it is set:
    <SYMBOL>_<interval>.csv        open_time_ms,open,high,low,close,volume
    <SYMBOL>.instrument.json       tick size, step size and limits for the symbol

Open times are stored as integer milliseconds so there is no doubt about which
end of the bar a timestamp refers to; the backtest stamps each bar at its close.
Volume is always in the base asset, whatever unit the exchange reports it in.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "custos-strategy-template"
# BACKTEST_DATA_DIR points at another cache, such as the fixture CI backtests with.
DATA_DIR = Path(os.environ.get("BACKTEST_DATA_DIR") or ROOT / ".data")

# Bar sizes as config.yaml writes them, and the neutral interval names the sources
# translate into their own spelling.
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

# One row per bar: open time in milliseconds, then open, high, low, close and
# base-asset volume as the exchange's decimal strings.
Kline = tuple[int, str, str, str, str, str]


class DataError(RuntimeError):
    pass


def interval_for(bar_type: str) -> str:
    try:
        return BAR_TYPE_TO_INTERVAL[bar_type.upper()]
    except KeyError:
        supported = ", ".join(BAR_TYPE_TO_INTERVAL)
        raise DataError(f"bar {bar_type!r} is not supported; use one of {supported}") from None


def interval_ms(interval: str) -> int:
    unit = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[interval[-1]]
    return int(interval[:-1]) * unit


def get_json(url: str, params: dict[str, object], exchange: str) -> object:
    query = urllib.parse.urlencode(params)
    # OKX answers 403 to urllib's default User-Agent.
    request = urllib.request.Request(
        f"{url}?{query}" if query else url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Binance, and others behind the same kind of geofence, answer 451 from
        # locations they do not serve.
        hint = f" ({exchange} does not serve this location)" if error.code == 451 else ""
        raise DataError(f"{exchange} refused {url} with HTTP {error.code}{hint}") from error
    except OSError as error:
        raise DataError(f"could not reach {url}: {error}") from error


def decimal_text(value: str | Decimal) -> str:
    """A decimal without trailing zeros or an exponent: '0.01000' -> '0.01'."""
    return format(Decimal(value).normalize(), "f")
