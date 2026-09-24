"""Which data source serves which connector, and the cache in front of them.

Every connector a strategy can be created for (copier.yml) must appear here, and
tests/test_data_sources.py checks that it does. Adding an exchange means adding
a module beside binance.py with the same five names -- NAME, CONNECTORS,
symbol_for, fetch_rules and fetch_klines -- and listing it in SOURCES.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType

from tools.data import binance, common, okx, sodex
from tools.data.common import DataError, interval_for, interval_ms

SOURCES: dict[str, ModuleType] = {
    connector: module for module in (binance, okx, sodex) for connector in module.CONNECTORS
}


def source_for(connector: str) -> ModuleType:
    try:
        return SOURCES[connector]
    except KeyError:
        supported = ", ".join(sorted(SOURCES))
        raise DataError(
            f"no public data source for connector {connector!r}; use {supported}"
        ) from None


def intervals_for(connector: str) -> frozenset[str]:
    source = source_for(connector)
    by_connector = getattr(source, "INTERVALS_BY_CONNECTOR", None)
    return by_connector[connector] if by_connector else source.INTERVALS


def paths_for(connector: str, pair: str, interval: str) -> tuple[Path, Path]:
    symbol = source_for(connector).symbol_for(connector, pair)
    folder = common.DATA_DIR / connector
    return folder / f"{symbol}_{interval}.csv", folder / f"{symbol}.instrument.json"


def _covers(csv_path: Path, start_ms: int, end_ms: int, step_ms: int) -> bool:
    if not csv_path.is_file():
        return False
    with csv_path.open(newline="") as handle:
        times = [int(row["open_time_ms"]) for row in csv.DictReader(handle)]
    return bool(times) and times[0] <= start_ms and times[-1] >= end_ms - step_ms


def ensure_data(
    connector: str, pair: str, bar_type: str, start: datetime, end: datetime
) -> tuple[Path, Path]:
    """Download closed bars covering [start, end) and the symbol's rules, unless present."""
    source = source_for(connector)
    interval = interval_for(bar_type)
    if interval not in intervals_for(connector):
        supported = ", ".join(sorted(intervals_for(connector), key=interval_ms))
        raise DataError(f"{source.NAME} has no {interval} bars for {connector}; use {supported}")
    csv_path, rules_path = paths_for(connector, pair, interval)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not rules_path.is_file():
        rules = source.fetch_rules(connector, pair)
        rules_path.write_text(json.dumps(rules, indent=2) + "\n", encoding="utf-8")

    step_ms = interval_ms(interval)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if _covers(csv_path, start_ms, end_ms, step_ms):
        return csv_path, rules_path

    # A bar that has not closed yet would change after it was cached.
    closed_by = int(time.time() * 1000)
    rows = [
        row
        for row in source.fetch_klines(connector, pair, interval, start_ms, end_ms)
        if row[0] + step_ms <= closed_by
    ]
    if not rows:
        raise DataError(f"{source.NAME} returned no {interval} bars for {pair} in that range")
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["open_time_ms", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)
    return csv_path, rules_path
