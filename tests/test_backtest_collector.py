"""Observe a real engine without replacing its trading decisions or execution."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from custos_toolkit.config import load_config
from custos_toolkit.signals import Signal
from custos_toolkit_nautilus.adapter import NautilusTradingStrategy, NautilusTradingStrategyConfig
from custos_toolkit_nautilus.adapter.trading_config import build_nautilus_base_config
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import LoggerConfig
from nautilus_trader.model import AccountType, BarType, Money, OmsType, OrderType, Venue

from tools.backtest.collector import ChartCollector
from tools.backtest.run import build_instrument, currency, load_market_data

START = 1_735_689_600_000_000_000
DAY = 86_400_000_000_000


class Probe(NautilusTradingStrategy):
    def on_strategy_start(self):
        self.calls = 0
        self.posts = 0
        self.fills = 0

    def on_strategy_stop(self):
        pass

    def calculate_signal(self, ctx, bar):
        self.calls += 1
        method = [Signal.enter_long, Signal.exit_long, Signal.enter_short, Signal.exit_short]
        return method[(self.calls - 1) % 4](price=bar.close, pair=ctx.pair)

    def on_post_bar(self, ctx, bar):
        self.posts += 1

    def on_order_filled(self, event):
        self.fills += 1
        super().on_order_filled(event)

    def get_chart_snapshot(self, ctx, bar):
        return {"close": {"value": float(bar.close), "label": "Observed close"}}

    def get_indicator_history(self):
        return {}


def run_probe(tmp_path, observed=True, prices=(10000, 10100, 10200, 10100), direction="both"):
    raw = {
        "strategy": {"name": "probe", "version": "0.1.0"},
        "trading": {
            "connector": {"value": "binance_perpetual"},
            "pairs": {"value": ["BTC-USDT"]},
            "direction": {"value": direction},
            "order_type": {"value": "market"},
            "leverage": {"value": 1},
        },
        "position": {"size_type": {"value": "percentage"}, "size_value": {"value": 0.1}},
        "warmup": {"mode": {"value": "none"}},
        "platforms": {"nautilus": {"bar_type": {"value": "1-DAY"}}},
        "risk": {
            "trade": {
                "sl_tp_mode": {"value": "tick"},
                "stop_loss": {"method": {"value": "none"}},
                "take_profit": {"method": {"value": "none"}},
            }
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(raw))
    config = NautilusTradingStrategyConfig(**build_nautilus_base_config(load_config(config_path)))
    strategy = Probe(config)
    rules = dict(
        symbol="BTCUSDT",
        market="perpetual",
        base="BTC",
        quote="USDT",
        settlement="USDT",
        tick_size="0.1",
        step_size="0.001",
        min_qty="0.001",
        max_qty="100",
        min_notional="5",
        min_price=None,
        max_price=None,
    )
    instrument = build_instrument("BTCUSDT-PERP.BINANCE", rules, Decimal("0.001"), Decimal("0.001"))
    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggerConfig.from_spec("stdout=Error"))
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(10000, instrument.quote_currency)],
    )
    engine.add_instrument(instrument)
    path = tmp_path / "probe.csv"
    path.write_text(
        "open_time_ms,open,high,low,close,volume\n"
        + "\n".join(
            f"{(START + i * DAY) // 1000000},{p},{p},{p},{p},10" for i, p in enumerate(prices)
        )
        + "\n"
    )
    quotes, bars = load_market_data(
        path,
        instrument,
        BarType.from_str(f"{instrument.id}-1-DAY-LAST-EXTERNAL"),
        DAY,
        START,
        START + len(prices) * DAY,
    )
    engine.add_data(quotes)
    engine.add_data(bars)
    collector = ChartCollector()
    collector.add_bars(bars, DAY)
    if observed:
        collector.attach(strategy)
    engine.add_strategy(strategy)
    engine.run(start=START, end=START + len(prices) * DAY)
    orders = sorted(
        (str(o.side), str(o.quantity), str(o.status), o.ts_init, o.ts_last)
        for o in engine.cache.orders()
    )
    cash = str(
        engine.cache.account_for_venue(Venue("BINANCE")).balance_total(instrument.quote_currency)
    )
    report = collector.finish(strategy, engine.cache) if observed else None
    calls = strategy.calls, strategy.posts, strategy.fills
    if observed:
        collector.detach()
        assert "calculate_signal" not in strategy.__dict__
    engine.dispose()
    return orders, cash, calls, report


def test_observation_preserves_real_engine_orders_balance_and_callback_counts(tmp_path):
    plain = run_probe(tmp_path, False)
    seen = run_probe(tmp_path)
    assert plain[:3] == seen[:3]
    assert seen[2] == (4, 4, 4)
    report = seen[3]
    assert [s["action"] for s in report["signals"]] == [
        "enter_long",
        "exit_long",
        "enter_short",
        "exit_short",
    ]
    assert [f["legs"][0]["action"] for f in report["fills"]] == [
        "enter_long",
        "exit_long",
        "enter_short",
        "exit_short",
    ]
    assert all(f["signal_id"] for f in report["fills"])
    assert {f["order_type"] for f in report["fills"]} == {"MARKET"}
    assert {order["order_type"] for order in report["orders"]} == {"MARKET"}
    assert len(report["indicators"][0]["points"]) == 4
    net = sum(Decimal(fill["pnl"]["realized_net"]) for fill in report["fills"])
    cash_amount, cash_currency = seen[1].split()
    assert cash_currency == "USDT"
    assert abs(net - (Decimal(cash_amount) - Decimal(10000))) <= Decimal("0.00000001")
    assert report["instruments"][0]["pnl_currency"] == "USDT"
    assert report["fills"][-1]["pnl"]["cumulative_net"] == str(net)


def test_spot_instrument_exports_explicit_linear_economics():
    from tests.test_backtest_run import RULES

    instrument = build_instrument(
        "BTCUSDT.BINANCE", dict(RULES, market="spot"), Decimal(0), Decimal(0)
    )
    collector = ChartCollector()
    report = collector.finish(
        SimpleNamespace(), SimpleNamespace(orders=lambda: [], instruments=lambda: [instrument])
    )
    assert report["instruments"] == [
        {
            "id": "BTCUSDT.BINANCE",
            "price_model": "linear",
            "pnl_currency": "USDT",
            "pnl_precision": 8,
            "multiplier": "1",
        }
    ]


def test_repeated_use_is_refused_and_detach_restores_instance_hooks():
    strategy = SimpleNamespace(
        calculate_signal=lambda ctx, bar: Signal.neutral(),
        on_post_bar=lambda ctx, bar: None,
        on_order_filled=lambda event: None,
        submit_order=lambda order: None,
    )
    original = strategy.calculate_signal
    collector = ChartCollector()
    collector.attach(strategy)
    with pytest.raises(ValueError, match="attached"):
        collector.attach(strategy)
    collector.detach()
    assert strategy.calculate_signal is original


def canonical(report):
    signals = [{k: v for k, v in s.items() if k != "native_signal_id"} for s in report["signals"]]
    fills = [
        {k: v for k, v in f.items() if k not in {"id", "order_id", "trade_id", "position_id"}}
        for f in report["fills"]
    ]
    return signals, fills


def test_future_prices_and_longer_runs_do_not_change_recorded_event_prefixes(tmp_path):
    prices = (10000, 10100, 10200, 10100, 9900, 10300, 10500, 10000)
    full = run_probe(tmp_path, prices=prices)[3]
    for count in range(1, len(prices)):
        earlier = run_probe(tmp_path, prices=prices[:count])[3]
        cutoff = START + count * DAY
        prefix = {
            **full,
            **{
                kind: [e for e in full[kind] if int(e["event_time_ns"]) <= cutoff]
                for kind in ("signals", "fills")
            },
        }
        assert canonical(earlier) == canonical(prefix)
    changed = run_probe(tmp_path, prices=prices[:4] + (19000, 5000, 17000, 4000))[3]
    for kind in ("signals", "fills"):
        full[kind] = [e for e in full[kind] if int(e["event_time_ns"]) <= START + 4 * DAY]
        changed[kind] = [e for e in changed[kind] if int(e["event_time_ns"]) <= START + 4 * DAY]
    assert canonical(full) == canonical(changed)


def test_observer_failure_does_not_suppress_real_trading(tmp_path, monkeypatch):
    plain = run_probe(tmp_path, False)

    def fail(self, ctx, bar):
        raise RuntimeError("broken chart hook")

    monkeypatch.setattr(Probe, "get_chart_snapshot", fail)
    original = ChartCollector.finish

    def finish(self, strategy, cache):
        with pytest.raises(ValueError, match="broken chart hook"):
            original(self, strategy, cache)
        return {"failed": True}

    monkeypatch.setattr(ChartCollector, "finish", finish)
    observed = run_probe(tmp_path)
    assert plain[:3] == observed[:3]
    assert observed[3] == {"failed": True}


def test_direction_gates_leave_signals_visible_without_fabricating_fills(tmp_path):
    plain = run_probe(tmp_path, False, direction="long")
    observed = run_probe(tmp_path, direction="long")
    assert plain[:3] == observed[:3]
    assert len(observed[3]["signals"]) == 4
    assert len(observed[3]["fills"]) == 2
    assert [f["legs"][0]["action"] for f in observed[3]["fills"]] == ["enter_long", "exit_long"]


def test_one_signal_links_multiple_orders_and_partial_fill_callbacks():
    strategy = SimpleNamespace(
        calculate_signal=lambda ctx, bar: Signal.enter_long(price=100, pair=ctx.pair),
        on_post_bar=lambda ctx, bar: None,
        on_order_filled=lambda event: None,
        submit_order=lambda order: None,
        get_indicator_history=lambda: {},
    )
    ctx = SimpleNamespace(pair="BTC-TEST", instrument_id="BTC.TEST")
    bar = SimpleNamespace(ts_event=START, bar_type="daily")
    collector = ChartCollector()
    collector.attach(strategy)
    signal = strategy.calculate_signal(ctx, bar)
    signal.metadata["_signal_id"] = "native-parent"
    orders = []
    for identity, quantity in (("o1", "0.7"), ("o2", "0.3")):
        order = SimpleNamespace(
            client_order_id=identity,
            instrument_id="BTC.TEST",
            tags=["signal_id:native-parent"],
            side=SimpleNamespace(name="BUY"),
            quantity=quantity,
            filled_qty=quantity,
            status=SimpleNamespace(name="FILLED"),
            order_type=OrderType.MARKET if identity == "o1" else OrderType.LIMIT,
        )
        orders.append(order)
        strategy.submit_order(order)
        order.tags = None
    for index, (order_id, quantity) in enumerate((("o1", "0.3"), ("o1", "0.4"), ("o2", "0.3")), 1):
        strategy.on_order_filled(
            SimpleNamespace(
                event_id=f"f{index}",
                trade_id=f"t{index}",
                client_order_id=order_id,
                position_id="p",
                instrument_id="BTC.TEST",
                ts_event=START + index,
                order_side=SimpleNamespace(name="BUY"),
                order_type=OrderType.MARKET if order_id == "o1" else OrderType.LIMIT,
                last_qty=quantity,
                last_px="100.1",
                commission=Money(Decimal("0.01"), currency("TEST")),
            )
        )
    strategy.on_post_bar(ctx, bar)
    result = collector.finish(
        strategy, SimpleNamespace(orders=lambda: orders, instruments=lambda: [])
    )
    collector.detach()
    assert len(result["signals"]) == 1 and len(result["fills"]) == 3
    assert {fill["signal_id"] for fill in result["fills"]} == {result["signals"][0]["id"]}
    assert [fill["quantity"] for fill in result["fills"]] == ["0.3", "0.4", "0.3"]
    assert [fill["order_type"] for fill in result["fills"]] == ["MARKET", "MARKET", "LIMIT"]
    assert [order["order_type"] for order in result["orders"]] == ["MARKET", "LIMIT"]
    assert all(fill["commission_amount"] == "0.01000000" for fill in result["fills"])
    assert all(fill["commission_currency"] == "TEST" for fill in result["fills"])
    assert [fill["event_time_ns"] for fill in result["fills"]] == [
        str(START + i) for i in (1, 2, 3)
    ]
