"""Frozen sizing inputs, actual order quantities and observation-only behavior."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from nautilus_trader.model import InstrumentId, Money, Price, Quantity

from tests.test_backtest_collector import run_probe
from tests.test_backtest_run import RULES
from tools.backtest.run import build_instrument
from tools.backtest.sizing import SizingObserver


def subject(*, missing_equity=False, custom=False):
    instrument = build_instrument(
        "BTCUSDT-PERP.BINANCE",
        dict(RULES, market="perpetual"),
        Decimal("0.0002"),
        Decimal("0.001"),
    )
    instrument_id = InstrumentId.from_str("BTCUSDT-PERP.BINANCE")
    unit = instrument.quote_currency
    balances = SimpleNamespace(
        free=Money(Decimal("9950.86719370"), unit),
        total=Money(Decimal("10000"), unit),
        locked=Money(Decimal("49.13280630"), unit),
    )
    portfolio = SimpleNamespace(
        account=lambda _: SimpleNamespace(balances=lambda: {unit: balances}),
        equity=lambda _: {unit: Money(Decimal("10020"), unit)},
        missing_price_instruments=lambda _: {instrument_id} if missing_equity else set(),
    )
    sizer = SimpleNamespace(calculate_base_size=lambda capital: capital * Decimal("0.1"))
    orders = []

    def create_entry_order(**kwargs):
        order = SimpleNamespace(
            instrument_id=instrument_id,
            client_order_id=f"order-{len(orders)}",
            quantity=Quantity.from_str("0.010"),
        )
        orders.append(order)
        return order

    manager = SimpleNamespace(create_entry_order=create_entry_order)
    strategy = SimpleNamespace(
        clock=SimpleNamespace(timestamp_ns=lambda: 1736294400000000000),
        cache=SimpleNamespace(instrument=lambda _: instrument),
        portfolio=portfolio,
        config=SimpleNamespace(
            position=SimpleNamespace(
                capital_mode="compound", size_type="percentage", size_value=0.1
            )
        ),
        _position_sizer=sizer,
    )
    strategy.calculate_position_size = (
        (lambda ctx, signal: Decimal("123"))
        if custom
        else lambda ctx, signal: sizer.calculate_base_size(balances.free.as_decimal()).quantize(
            Decimal("0.001"), rounding="ROUND_DOWN"
        )
    )
    ctx = SimpleNamespace(instrument_id=instrument_id, execution_manager=manager)
    return strategy, ctx, balances, manager


def test_entry_snapshot_distinguishes_free_equity_base_target_and_real_order_rounding():
    strategy, ctx, balances, manager = subject()
    original = strategy.calculate_position_size
    original_base = strategy._position_sizer.calculate_base_size
    original_create = manager.create_entry_order
    observer = SizingObserver()
    observer.attach(strategy)
    signal = SimpleNamespace()
    size = strategy.calculate_position_size(ctx, signal)
    order = manager.create_entry_order(
        signal=signal,
        instrument_id=ctx.instrument_id,
        size=size,
        bar=SimpleNamespace(close=Price.from_str("96920.30")),
    )
    observer.submitted(order)
    snapshot = observer.for_order(str(ctx.instrument_id), order.client_order_id)
    assert Decimal(snapshot["available_balance"]) == Decimal("9950.8671937")
    assert Decimal(snapshot["account_equity"]) == Decimal("10020")
    assert Decimal(snapshot["sizing_capital"]) == Decimal("9950.8671937")
    assert Decimal(snapshot["base_target_notional"]) == Decimal("995.08671937")
    assert snapshot["target_notional"] == snapshot["order_notional"] == "995.086"
    assert Decimal(snapshot["raw_quantity"]) == Decimal("995.086") / Decimal("96920.3")
    assert (
        Decimal(snapshot["rounded_quantity"])
        == Decimal(snapshot["submitted_quantity"])
        == Decimal("0.01")
    )
    assert Decimal(snapshot["estimated_taker_fee"]) == Decimal("0.969203")
    assert Decimal(snapshot["multiplier"]) == 1
    balances.free = Money(Decimal("1"), strategy.cache.instrument(ctx.instrument_id).quote_currency)
    assert observer.for_order(str(ctx.instrument_id), order.client_order_id) == snapshot
    snapshot["available_balance"] = "changed copy"
    assert (
        observer.for_order(str(ctx.instrument_id), order.client_order_id)["available_balance"]
        != "changed copy"
    )
    observer.detach()
    assert strategy.calculate_position_size is original
    assert strategy._position_sizer.calculate_base_size is original_base
    assert manager.create_entry_order is original_create


def test_override_notional_is_separate_and_unreliable_equity_stays_unknown():
    strategy, ctx, _, manager = subject(missing_equity=True, custom=True)
    observer = SizingObserver()
    observer.attach(strategy)
    signal = SimpleNamespace()
    assert strategy.calculate_position_size(ctx, signal) == Decimal("123")
    order = manager.create_entry_order(
        signal=signal,
        instrument_id=ctx.instrument_id,
        size=Decimal("500"),
        bar=SimpleNamespace(close=Price.from_str("96920.30")),
    )
    result = observer.for_order(str(ctx.instrument_id), order.client_order_id)
    assert result["target_notional"] == "123" and result["order_notional"] == "500"
    assert "sizing_capital" not in result and "base_target_notional" not in result
    assert "account_equity" not in result and "unpriced" in result["equity_status"]
    observer.detach()


def test_observation_failure_does_not_suppress_sizing_and_strategy_exceptions_propagate():
    strategy, ctx, _, _ = subject(custom=True)
    del strategy.clock
    observer = SizingObserver()
    observer.attach(strategy)
    assert strategy.calculate_position_size(ctx, SimpleNamespace()) == Decimal("123")
    observer.detach()

    def fail(*_):
        raise RuntimeError("real strategy failure")

    strategy.calculate_position_size = fail
    observer.attach(strategy)
    with pytest.raises(RuntimeError, match="real strategy failure"):
        strategy.calculate_position_size(ctx, SimpleNamespace())
    observer.detach()


def test_native_engine_snapshots_are_bound_to_entry_orders_and_exits_have_no_budget(tmp_path):
    plain = run_probe(tmp_path, False)
    observed = run_probe(tmp_path)
    assert plain[:3] == observed[:3]
    fills = observed[3]["fills"]
    assert Decimal(fills[0]["sizing"]["available_balance"]) == Decimal("10000")
    assert Decimal(fills[0]["sizing"]["base_target_notional"]) == Decimal("1000")
    assert Decimal(fills[0]["sizing"]["raw_quantity"]) == Decimal("0.1")
    assert Decimal(fills[0]["sizing"]["rounded_quantity"]) == Decimal("0.1")
    assert Decimal(fills[0]["sizing"]["quantity_step"]) == Decimal("0.001")
    assert fills[1]["sizing"] is None and fills[3]["sizing"] is None
    assert Decimal(fills[2]["sizing"]["available_balance"]) != Decimal("10000")


@pytest.mark.parametrize("capital_mode", ["fixed_capital", "compound"])
def test_native_capital_basis_is_observed_rather_than_inferred_from_the_mode(
    tmp_path, capital_mode
):
    plain = run_probe(tmp_path, False, capital_mode=capital_mode)
    observed = run_probe(tmp_path, capital_mode=capital_mode)
    assert plain[:3] == observed[:3]
    snapshot = observed[3]["fills"][2]["sizing"]
    assert snapshot["capital_mode"] == capital_mode
    if capital_mode == "compound":
        assert Decimal(snapshot["sizing_capital"]) == Decimal(snapshot["available_balance"])
    else:
        assert Decimal(snapshot["sizing_capital"]) != Decimal(snapshot["available_balance"])


def test_partial_fills_share_the_frozen_order_plan_but_keep_individual_fees():
    from custos_toolkit.signals import Signal
    from nautilus_trader.model import OrderSide, OrderType

    from tools.backtest.collector import ChartCollector

    strategy, ctx, _, manager = subject()
    strategy.calculate_signal = lambda ctx, bar: Signal.enter_long(price=96920.3, pair="BTC-USDT")
    strategy.on_post_bar = lambda *_: None
    strategy.on_order_filled = lambda *_: None
    strategy.submit_order = lambda *_: None
    strategy.get_indicator_history = lambda: {}
    observer = ChartCollector()
    observer.attach(strategy)
    bar = SimpleNamespace(
        close=Price.from_str("96920.30"), ts_event=1736294400000000000, bar_type="daily"
    )
    signal = strategy.calculate_signal(ctx, bar)
    signal.metadata["_signal_id"] = "native-entry"
    size = strategy.calculate_position_size(ctx, signal)
    orders = []
    for _ in range(2):
        order = manager.create_entry_order(
            signal=signal, instrument_id=ctx.instrument_id, size=size, bar=bar
        )
        order.tags = ["signal_id:native-entry"]
        order.side = OrderSide.BUY
        order.order_type = OrderType.MARKET
        order.status = SimpleNamespace(name="FILLED")
        order.filled_qty = order.quantity
        orders.append(order)
        strategy.submit_order(order)
    for index, (order, qty, fee) in enumerate(
        [(orders[0], "0.003", "0.01"), (orders[0], "0.007", "0.02"), (orders[1], "0.010", "0.03")]
    ):
        strategy.on_order_filled(
            SimpleNamespace(
                event_id=f"fill-{index}",
                trade_id=f"trade-{index}",
                client_order_id=order.client_order_id,
                position_id="position",
                instrument_id=ctx.instrument_id,
                ts_event=bar.ts_event + index,
                order_side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                last_qty=Quantity.from_str(qty),
                last_px=bar.close,
                commission=Money(
                    Decimal(fee), strategy.cache.instrument(ctx.instrument_id).quote_currency
                ),
            )
        )
    strategy.on_post_bar(ctx, bar)
    result = observer.finish(
        strategy,
        SimpleNamespace(
            orders=lambda: orders,
            instruments=lambda: [strategy.cache.instrument(ctx.instrument_id)],
        ),
    )
    assert result["fills"][0]["sizing"] == result["fills"][1]["sizing"]
    assert [f["commission_amount"] for f in result["fills"]] == [
        "0.01000000",
        "0.02000000",
        "0.03000000",
    ]
    assert all(f["signal_id"] == result["signals"][0]["id"] for f in result["fills"])
    observer.detach()
