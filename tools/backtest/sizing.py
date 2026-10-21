"""Observe entry budgets and freeze order-construction inputs for audit views."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from tools.backtest.pnl import decimal, text


def value(raw: object) -> str | None:
    try:
        return text(decimal(raw, "sizing value"))
    except ValueError:
        return None


class SizingObserver:
    def __init__(self) -> None:
        self.hooks: list[tuple[object, str, object]] = []
        self.wrapped: set[tuple[int, str]] = set()
        self.active: list[dict] = []
        self.signals: dict[int, tuple[object, dict]] = {}
        self.orders: dict[tuple[str, str], dict] = {}

    def wrap(self, target, name, factory) -> None:
        key = (id(target), name)
        if key in self.wrapped:
            return
        original = getattr(target, name, None)
        if not callable(original):
            return
        previous = target.__dict__.get(name)
        setattr(target, name, factory(original))
        self.hooks.append((target, name, previous))
        self.wrapped.add(key)

    def attach(self, strategy) -> None:
        def calculate(original):
            def observed(ctx, signal):
                try:
                    snapshot = self.balances(strategy, ctx)
                except Exception:
                    snapshot = {"balance_status": "sizing snapshot unavailable"}
                self.signals[id(signal)] = (signal, snapshot)
                try:
                    self.wrap(ctx.execution_manager, "create_entry_order", create)
                    self.wrap(strategy._position_sizer, "calculate_base_size", base)
                except Exception:
                    # Custom strategies need not expose standard toolkit components.
                    snapshot["component_status"] = "standard sizing components unavailable"
                self.active.append(snapshot)
                try:
                    result = original(ctx, signal)
                    snapshot["target_notional"] = value(result)
                    return result
                finally:
                    self.active.pop()

            return observed

        def base(original):
            def observed(capital, *args, **kwargs):
                result = original(capital, *args, **kwargs)
                if self.active:
                    self.active[-1]["sizing_capital"] = value(capital)
                    self.active[-1]["base_target_notional"] = value(result)
                return result

            return observed

        def capital(original):
            def observed(*args, **kwargs):
                result = original(*args, **kwargs)
                if self.active:
                    self.active[-1]["sizing_capital"] = value(result)
                return result

            return observed

        def create(original):
            def observed(*args, **kwargs):
                # The installed toolkit invokes this interface with named inputs.
                # Unknown/custom calling conventions still execute unchanged.
                order = original(*args, **kwargs)
                if order is not None:
                    try:
                        signal = kwargs["signal"]
                        snapshot = self.for_signal(signal) or {}
                        bar = kwargs["bar"]
                        instrument = strategy.cache.instrument(kwargs["instrument_id"])
                        multiplier = decimal(str(instrument.multiplier), "contract multiplier")
                        snapshot.update(
                            order_notional=value(kwargs["size"]),
                            reference_price=value(bar.close.as_decimal()),
                            quantity_step=value(instrument.size_increment.as_decimal()),
                            rounded_quantity=value(order.quantity.as_decimal()),
                            maker_fee_rate=value(instrument.maker_fee),
                            taker_fee_rate=value(instrument.taker_fee),
                            maker_fee_pct=value(instrument.maker_fee * 100),
                            taker_fee_pct=value(instrument.taker_fee * 100),
                            multiplier=value(multiplier),
                            quantity_source="actual_created_order",
                        )
                        if not instrument.is_inverse:
                            notional = decimal(kwargs["size"], "order notional")
                            price = bar.close.as_decimal()
                            if price > 0 and multiplier > 0:
                                snapshot["raw_quantity"] = text(notional / (price * multiplier))
                                snapshot["raw_quantity_source"] = "linear_order_inputs"
                                snapshot["estimated_taker_fee"] = value(
                                    order.quantity.as_decimal()
                                    * price
                                    * multiplier
                                    * instrument.taker_fee
                                )
                        self.orders[(str(order.instrument_id), str(order.client_order_id))] = (
                            deepcopy(snapshot)
                        )
                    except Exception:
                        # An observation failure cannot prevent submission of a real order.
                        pass
                return order

            return observed

        self.wrap(strategy, "calculate_position_size", calculate)
        self.wrap(strategy, "_get_effective_capital", capital)

    def balances(self, strategy, ctx) -> dict:
        snapshot = {"observed_time_ns": str(strategy.clock.timestamp_ns())}
        try:
            instrument = strategy.cache.instrument(ctx.instrument_id)
            currency = instrument.quote_currency
            snapshot["currency"] = str(currency)
            snapshot["currency_precision"] = currency.precision
            snapshot["capital_mode"] = strategy.config.position.capital_mode
            snapshot["size_type"] = strategy.config.position.size_type
            snapshot["size_value"] = value(Decimal(str(strategy.config.position.size_value)))
            account = strategy.portfolio.account(ctx.instrument_id.venue)
            balance = account.balances()[currency]
            snapshot.update(
                available_balance=value(balance.free.as_decimal()),
                account_total=value(balance.total.as_decimal()),
                locked_balance=value(balance.locked.as_decimal()),
            )
        except Exception:
            snapshot["balance_status"] = "account balance not observed"
            return snapshot
        try:
            if strategy.portfolio.missing_price_instruments(ctx.instrument_id.venue):
                snapshot["equity_status"] = "unpriced positions; equity unavailable"
            else:
                equity = strategy.portfolio.equity(ctx.instrument_id.venue).get(currency)
                snapshot["account_equity"] = value(equity.as_decimal()) if equity else None
        except Exception:
            snapshot["equity_status"] = "account equity not observed"
        return snapshot

    def for_signal(self, signal) -> dict | None:
        entry = self.signals.get(id(signal))
        return deepcopy(entry[1]) if entry is not None and entry[0] is signal else None

    def for_order(self, instrument_id: str, order_id: str) -> dict | None:
        return deepcopy(self.orders.get((instrument_id, order_id)))

    def submitted(self, order) -> None:
        snapshot = self.orders.get((str(order.instrument_id), str(order.client_order_id)))
        if snapshot is not None and "submitted_quantity" not in snapshot:
            snapshot["submitted_quantity"] = value(order.quantity.as_decimal())

    def detach(self) -> None:
        for target, name, previous in reversed(self.hooks):
            if previous is None:
                delattr(target, name)
            else:
                setattr(target, name, previous)
        self.hooks.clear()
        self.wrapped.clear()
