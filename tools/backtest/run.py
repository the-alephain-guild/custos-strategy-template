#!/usr/bin/env python3
"""Backtest one strategy on public market data from its exchange.

    uv run python tools/backtest/run.py trend/my_idea --start 2025-01-01 --end 2025-04-01

The strategy is found the way Custos finds it: its directory is handed to the
strategy toolkit's registry, which imports refinement/nautilus/strategy.py and
builds the strategy from the directory's config.yaml. What runs here is the
same code and configuration that runs live.

Missing data is downloaded first, from the exchange the strategy's connector
names (see tools/data/sources.py). With --venue the strategy runs on one of its
venue profiles (tools/venues.py): the same code, config.yaml with the profile
laid over it, and that exchange's data. Each bar is
stamped at its close, so the strategy never sees a bar before it has finished.
A summary table is printed, or the summary as JSON with --json, and the JSON
is written to <strategy>/backtests/output/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui, venues  # noqa: E402
from tools.data.common import BAR_TYPE_TO_INTERVAL, DataError, interval_for  # noqa: E402
from tools.data.sources import ensure_data  # noqa: E402
from tools.toolchain.dev import current_toolchain  # noqa: E402


def resolve_strategy_dir(argument: str) -> Path:
    candidates = [Path(argument), ROOT / argument, ROOT / "strategies" / argument]
    for candidate in candidates:
        if (candidate / "config.yaml").is_file():
            return candidate.resolve()
    raise SystemExit(
        f"no strategy with a config.yaml at {argument!r} (tried {len(candidates)} places)"
    )


def parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def interval_ns(bar_type: str) -> int:
    interval = interval_for(bar_type)
    unit = {"m": 60, "h": 3_600, "d": 86_400}[interval[-1]]
    return int(interval[:-1]) * unit * 1_000_000_000


def decimals(increment: str) -> int:
    exponent = Decimal(increment).normalize().as_tuple().exponent
    return max(0, -exponent)


def currency(code: str):
    """A currency by code, registering venue tokens such as SoDEX's vUSDC on first use.

    NautilusTrader knows the common crypto codes. Anything else is registered as a
    crypto currency at eight decimals, so balances and fees in it can be expressed.
    """
    from nautilus_trader.model import Currency, CurrencyType

    try:
        return Currency.from_str(code, strict=True)
    except ValueError:
        Currency.register(Currency(code, 8, 0, code, CurrencyType.CRYPTO), False)
        return Currency.from_str(code, strict=True)


def build_instrument(instrument_id: str, rules: dict, maker: Decimal, taker: Decimal):
    from nautilus_trader.model import (
        CryptoPerpetual,
        CurrencyPair,
        InstrumentId,
        Money,
        Price,
        Quantity,
        Symbol,
    )

    quote = currency(rules["quote"])
    common = dict(
        instrument_id=InstrumentId.from_str(instrument_id),
        raw_symbol=Symbol(rules["symbol"]),
        base_currency=currency(rules["base"]),
        quote_currency=quote,
        price_precision=decimals(rules["tick_size"]),
        size_precision=decimals(rules["step_size"]),
        price_increment=Price.from_str(rules["tick_size"]),
        size_increment=Quantity.from_str(rules["step_size"]),
        lot_size=Quantity.from_str(rules["step_size"]),
        max_quantity=Quantity.from_str(rules["max_qty"]),
        min_quantity=Quantity.from_str(rules["min_qty"]),
        max_notional=None,
        min_notional=Money(Decimal(rules["min_notional"]), quote),
        # OKX publishes no price bounds and SoDEX leaves some unset.
        max_price=Price.from_str(rules["max_price"]) if rules.get("max_price") else None,
        min_price=Price.from_str(rules["min_price"]) if rules.get("min_price") else None,
        maker_fee=maker,
        taker_fee=taker,
        ts_event=0,
        ts_init=0,
    )
    if rules["market"] == "perpetual":
        return CryptoPerpetual(
            **common,
            settlement_currency=currency(rules["settlement"]),
            is_inverse=False,
            margin_init=Decimal("1"),
            margin_maint=Decimal("0"),
        )
    return CurrencyPair(**common, margin_init=Decimal("0"), margin_maint=Decimal("0"))


def load_market_data(
    csv_path: Path, instrument, bar_type, step_ns: int, start_ns: int, end_ns: int
):
    """Bars stamped at their close, plus a quote at each open for order fills."""
    from nautilus_trader.model import Bar, QuoteTick

    bars, quotes = [], []
    with csv_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            open_ns = int(row["open_time_ms"]) * 1_000_000
            close_ns = open_ns + step_ns
            if open_ns < start_ns or close_ns > end_ns:
                continue
            # make_price / make_qty take numbers and round them to the instrument's precision.
            volume = float(row["volume"])
            open_price = instrument.make_price(float(row["open"]))
            quotes.append(
                QuoteTick(
                    instrument_id=instrument.id,
                    bid_price=open_price,
                    ask_price=open_price,
                    bid_size=instrument.make_qty(1),
                    ask_size=instrument.make_qty(1),
                    ts_event=open_ns,
                    ts_init=open_ns,
                )
            )
            bars.append(
                Bar(
                    bar_type=bar_type,
                    open=open_price,
                    high=instrument.make_price(float(row["high"])),
                    low=instrument.make_price(float(row["low"])),
                    close=instrument.make_price(float(row["close"])),
                    volume=instrument.make_qty(volume if volume > 0 else 1),
                    ts_event=close_ns,
                    ts_init=close_ns,
                )
            )
    return quotes, bars


@dataclass(frozen=True)
class BarFeed:
    """One bar series the backtest downloads and feeds to the strategy."""

    interval: str
    bar_spec: str
    # Fills need prices between bars; they come from the strategy's own bar only,
    # so a second series never adds a second quote at the same instant.
    quotes: bool


def bar_feeds(bar_spec: str, additional_timeframes: list[str] | None) -> list[BarFeed]:
    """The strategy's bar, then every additional timeframe it reads, each fed once.

    A strategy that reads a slower timeframe as well (a daily regime over hourly
    signals) lists it under backtesting.additional_timeframes and subscribes to it
    through the toolkit, which spells it with TIMEFRAME_MAP. The same map spells
    it here, so the bars fed are the bars subscribed to.
    """
    from custos_toolkit_nautilus.adapter.utils import TIMEFRAME_MAP

    downloadable = set(BAR_TYPE_TO_INTERVAL.values())
    feeds = [BarFeed(interval_for(bar_spec), bar_spec.upper(), quotes=True)]
    for raw in additional_timeframes or []:
        interval = str(raw).lower()
        if interval not in TIMEFRAME_MAP or interval not in downloadable:
            raise SystemExit(
                f"additional timeframe {raw!r} cannot be backtested; "
                f"use one of {', '.join(sorted(downloadable & set(TIMEFRAME_MAP)))}"
            )
        if interval in (feed.interval for feed in feeds):
            continue
        feeds.append(BarFeed(interval, TIMEFRAME_MAP[interval], quotes=False))
    return feeds


def config_for(root: Path, strategy_dir: Path, venue_id: str | None):
    """The strategy's configuration as the run reads it: its own, or a profile over it.

    A profile is rendered into .runner/strategies/ first, as `make start` renders
    it, and loaded from there: the toolkit's loader takes a file, and this way a
    backtest and a run read the very same bytes.
    """
    from custos_toolkit.config import load_config

    if venue_id is None:
        return load_config(strategy_dir / "config.yaml")
    try:
        rendered = venues.render_run_dir(root, strategy_dir, venue_id)
    except venues.VenueError as failure:
        raise SystemExit(str(failure)) from failure
    return load_config(rendered / "config.yaml")


def run(
    strategy_dir: Path,
    start: datetime,
    end: datetime,
    balance: Decimal,
    verbose: bool,
    venue_id: str | None = None,
) -> dict:
    # The registry reads this once, when it is first imported.
    os.environ["STRATEGY_INJECT_PATH"] = str(strategy_dir)

    from custos_toolkit_nautilus.adapter import create_strategy, get_venue_from_connector
    from custos_toolkit_nautilus.adapter.utils import instrument_id_str
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.common import LoggerConfig
    from nautilus_trader.model import (
        AccountType,
        BarType,
        Money,
        OmsType,
        Venue,
    )

    config = config_for(ROOT, strategy_dir, venue_id)
    connector = config.trading.get("connector")
    pairs = list(config.trading.get("pairs"))
    fees = config.trading.get("fees") or {}
    maker = Decimal(str(fees.get("maker", 0)))
    taker = Decimal(str(fees.get("taker", 0)))
    leverage = Decimal(str(config.trading.get("leverage") or 1))
    bar_spec = config.platforms.get("nautilus", {}).get("bar_type")
    backtesting = config.get("backtesting") or {}
    feeds = bar_feeds(bar_spec, backtesting.get("additional_timeframes"))
    start_ns, end_ns = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)

    logging = LoggerConfig.from_spec("stdout=Info" if verbose else "stdout=Error")
    engine = BacktestEngine(config=BacktestEngineConfig(logging=logging))

    instruments = []
    for pair in pairs:
        csv_paths = [ensure_data(connector, pair, feed.bar_spec, start, end) for feed in feeds]
        rules = json.loads(csv_paths[0][1].read_text(encoding="utf-8"))
        instrument = build_instrument(instrument_id_str(pair, connector), rules, maker, taker)
        instruments.append((instrument, rules, [csv for csv, _ in csv_paths]))

    settlement = currency(instruments[0][1]["settlement"])
    is_perpetual = instruments[0][1]["market"] == "perpetual"
    engine.add_venue(
        venue=Venue(get_venue_from_connector(connector)),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN if is_perpetual else AccountType.CASH,
        starting_balances=[Money(balance, settlement)],
        default_leverage=leverage if is_perpetual else None,
    )

    bar_count = 0
    for instrument, _rules, csv_paths in instruments:
        engine.add_instrument(instrument)
        for feed, csv_path in zip(feeds, csv_paths, strict=True):
            bar_type = BarType.from_str(f"{instrument.id}-{feed.bar_spec}-LAST-EXTERNAL")
            step_ns = interval_ns(feed.bar_spec)
            quotes, bars = load_market_data(
                csv_path, instrument, bar_type, step_ns, start_ns, end_ns
            )
            if not bars:
                raise SystemExit(
                    f"no {feed.interval} bars for {instrument.id} between {start} and {end}"
                )
            if feed.quotes:
                engine.add_data(quotes)
                bar_count += len(bars)
            engine.add_data(bars)

    strategy = create_strategy(strategy_dir.name, config_wrapper=config)
    engine.add_strategy(strategy)
    engine.run(start=start_ns, end=end_ns)

    result = engine.get_result()
    account = engine.cache.account_for_venue(Venue(get_venue_from_connector(connector)))
    final_balance = account.balance_total(settlement).as_decimal() if account else None
    summary = {
        "strategy": strategy_dir.name,
        "connector": connector,
        "venue": venue_id,
        "pairs": pairs,
        "bar": bar_spec,
        "additional_bars": [feed.bar_spec for feed in feeds[1:]],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": bar_count,
        "starting_balance": str(balance),
        "final_balance": str(final_balance) if final_balance is not None else None,
        "orders": result.total_orders,
        "positions": result.total_positions,
        "pnl": result.stats_pnls.get(str(settlement), {}),
        "returns": result.stats_returns,
        "toolchain": current_toolchain(),
    }
    engine.dispose()
    return summary


def write_summary(strategy_dir: Path, summary: dict) -> Path:
    folder = strategy_dir / "backtests" / "output"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}.json"
    path.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _number(value: object, digits: int = 2, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    number = float(value) * (100 if percent else 1)
    return f"{number:,.{digits}f}{'%' if percent else ''}"


def summary_rows(summary: dict) -> list[tuple[str, str]]:
    """The figures a person reads first, in the order they read them."""
    pnl, returns = summary["pnl"], summary["returns"]
    toolchain = summary.get("toolchain") or {}
    profile = [("Profile", summary["venue"])] if summary.get("venue") else []
    return [
        ("Market", f"{summary['connector']} {', '.join(summary['pairs'])}, {summary['bar']} bars"),
        *profile,
        ("Period", f"{summary['start'][:10]} to {summary['end'][:10]} ({summary['bars']} bars)"),
        ("Orders / positions", f"{summary['orders']} / {summary['positions']}"),
        ("Balance", f"{summary['starting_balance']} -> {_number(summary['final_balance'])}"),
        ("PnL", f"{_number(pnl.get('PnL (total)'))} ({_number(pnl.get('PnL% (total)'))}%)"),
        ("Win rate", _number(pnl.get("Win Rate"), 1, percent=True)),
        ("Profit factor", _number(returns.get("Profit Factor"))),
        ("Sharpe (252 days)", _number(returns.get("Sharpe Ratio (252 days)"))),
        ("Toolchain", f"{toolchain.get('mode', 'pinned')}: NautilusTrader "
                      f"{toolchain.get('nautilus_trader')}, toolkit "
                      f"{toolchain.get('custos_strategy_toolkit')}"),
    ]  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", help="strategy directory, e.g. trend/my_idea")
    parser.add_argument("--start", required=True, help="ISO date or time, UTC if no offset")
    parser.add_argument("--end", required=True, help="ISO date or time, UTC if no offset")
    parser.add_argument("--balance", default="10000", help="starting balance in the quote currency")
    parser.add_argument("--verbose", action="store_true", help="show the strategy's log output")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--venue", help="a venue profile of the strategy, venues/<id>.yaml")
    args = parser.parse_args()

    strategy_dir = resolve_strategy_dir(args.strategy)
    start, end = parse_time(args.start), parse_time(args.end)
    if end <= start:
        raise SystemExit("--end must be after --start")
    profile = f" on its {args.venue} profile" if args.venue else ""
    ui.info(
        f"backtesting {strategy_dir.name}{profile} from {start:%Y-%m-%d} to {end:%Y-%m-%d}",
        "backtest",
    )
    try:
        summary = run(strategy_dir, start, end, Decimal(args.balance), args.verbose, args.venue)
    except DataError as error:
        ui.error(str(error), "backtest")
        return 1
    path = write_summary(strategy_dir, summary)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        ui.table(f"Backtest of {summary['strategy']}", summary_rows(summary))
    shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    ui.ok(f"summary written to {shown}", "backtest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
