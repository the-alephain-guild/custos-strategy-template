"""Backtest-only observation of strategy outputs and actual fill callbacks."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from tools.backtest.report import ChartError, indicator_snapshot, legacy_indicators, split_fill


class ChartCollector:
    def __init__(self) -> None:
        self.candles: list[dict] = []
        self.signals: list[dict] = []
        self.fills: list[dict] = []
        self.indicators: dict[str, dict] = {}
        self.errors: list[str] = []
        self._strategy = None
        self._originals: dict = {}
        self._pending: list[tuple[object, dict]] = []
        self._order_lineage: dict[str, str] = {}

    def add_bars(self, bars: list, step_ns: int) -> None:
        for bar in bars:
            self.candles.append(
                {
                    "instrument_id": str(bar.bar_type.instrument_id),
                    "bar_type": str(bar.bar_type),
                    "open_time_ns": str(bar.ts_event - step_ns),
                    "close_time_ns": str(bar.ts_event),
                    **{
                        name: float(str(getattr(bar, name)))
                        for name in ("open", "high", "low", "close", "volume")
                    },
                }
            )

    def attach(self, strategy: object) -> None:
        if self._strategy is not None:
            raise ValueError("collector already attached")
        self._strategy = strategy
        names = ("calculate_signal", "on_post_bar", "on_order_filled", "submit_order")
        self._originals = {name: strategy.__dict__.get(name) for name in names}
        calculate, post, filled, submit = (getattr(strategy, name) for name in names)

        def submit_order(order, *args, **kwargs):
            # Snapshot tags before native execution: market fills can remove
            # them, and position cleanup can clear the toolkit's lineage map.
            try:
                for tag in order.tags or []:
                    if str(tag).startswith("signal_id:"):
                        self._order_lineage[str(order.client_order_id)] = str(tag)[10:]
            except Exception as error:
                self.errors.append(f"order observation: {error}")
            return submit(order, *args, **kwargs)

        def calculate_signal(ctx, bar):
            signal = calculate(ctx, bar)
            # Only observation failures are deferred. A strategy failure keeps its
            # original behavior; an observer failure must not change its orders.
            try:
                if signal.is_actionable():
                    record = {
                        "id": f"signal-{len(self.signals) + 1}",
                        "instrument_id": str(ctx.instrument_id),
                        "bar_type": str(bar.bar_type),
                        "event_time_ns": str(bar.ts_event),
                        "declared_time_ns": str(signal.timestamp)
                        if signal.timestamp is not None
                        else None,
                        "action": signal.direction.value,
                        "price": str(signal.price),
                        "metadata": deepcopy(signal.metadata),
                    }
                    self.signals.append(record)
                    self._pending.append((signal, record))
                snapshot = getattr(strategy, "get_chart_snapshot", None)
                if snapshot is not None:
                    indicator_snapshot(self.indicators, snapshot(ctx, bar), ctx, bar)
            except Exception as error:
                self.errors.append(f"signal/indicator observation: {error}")
            return signal

        def on_post_bar(ctx, bar):
            result = post(ctx, bar)
            try:
                self._flush_signals()
                self._order_lineage.update(getattr(strategy, "_order_signal_map", {}))
            except Exception as error:
                self.errors.append(f"lineage observation: {error}")
            return result

        def on_order_filled(event):
            try:
                self.fills.append(
                    {
                        "id": str(event.event_id),
                        "trade_id": str(event.trade_id),
                        "order_id": str(event.client_order_id),
                        "position_id": str(event.position_id)
                        if event.position_id is not None
                        else None,
                        "instrument_id": str(event.instrument_id),
                        "event_time_ns": str(event.ts_event),
                        "side": event.order_side.name,
                        "order_type": event.order_type.name,
                        "quantity": str(event.last_qty),
                        "price": str(event.last_px),
                        "commission": str(event.commission),
                    }
                )
            except Exception as error:
                self.errors.append(f"fill observation: {error}")
            return filled(event)

        strategy.calculate_signal = calculate_signal
        strategy.on_post_bar = on_post_bar
        strategy.on_order_filled = on_order_filled
        strategy.submit_order = submit_order

    def _flush_signals(self) -> None:
        for signal, record in self._pending:
            record["native_signal_id"] = signal.metadata.get("_signal_id")
        self._pending.clear()

    def detach(self) -> None:
        if self._strategy is None:
            return
        for name, original in self._originals.items():
            if original is None:
                delattr(self._strategy, name)
            else:
                setattr(self._strategy, name, original)
        self._strategy = None

    def finish(self, strategy: object, cache: object) -> dict:
        if self.errors:
            raise ChartError("; ".join(self.errors))
        self._flush_signals()
        indicators = list(self.indicators.values())
        if not hasattr(strategy, "get_chart_snapshot"):
            try:
                history = getattr(strategy, "get_indicator_history", lambda: {})()
            except Exception as error:
                raise ChartError(f"historical indicator export: {error}") from error
            indicators = legacy_indicators(history)
        signal_ids = {s["native_signal_id"]: s["id"] for s in self.signals if s["native_signal_id"]}
        # The toolkit retains this map because filled market orders may lose
        # their tags. Missing lineage stays missing, never guessed by time.
        mapping = {**self._order_lineage, **getattr(strategy, "_order_signal_map", {})}
        inventory: dict[str, Decimal] = {}
        filled_quantities: dict[str, Decimal] = {}
        seen: set[str] = set()
        fills = []
        for event in self.fills:
            if event["id"] in seen:
                raise ChartError("duplicate fill event id")
            seen.add(event["id"])
            row = dict(event)
            instrument = row["instrument_id"]
            before = inventory.get(instrument, Decimal(0))
            row["legs"], after = split_fill(before, row["side"], Decimal(row["quantity"]))
            row["position_before"], row["position_after"] = str(before), str(after)
            row["signal_id"] = signal_ids.get(mapping.get(row["order_id"]))
            inventory[instrument] = after
            filled_quantities[row["order_id"]] = filled_quantities.get(
                row["order_id"], Decimal(0)
            ) + Decimal(row["quantity"])
            fills.append(row)
        orders = [
            {
                "id": str(o.client_order_id),
                "instrument_id": str(o.instrument_id),
                "side": o.side.name,
                "order_type": o.order_type.name,
                "quantity": str(o.quantity),
                "status": o.status.name,
                "filled_quantity": str(o.filled_qty),
            }
            for o in cache.orders()
        ]
        for order in orders:
            observed = filled_quantities.get(order["id"], Decimal(0))
            if observed != Decimal(order["filled_quantity"]):
                raise ChartError(f"fill callbacks disagree with cached order {order['id']}")
        return {
            "schema_version": 1,
            "candles": self.candles,
            "indicators": indicators,
            "signals": self.signals,
            "fills": fills,
            "orders": orders,
        }
