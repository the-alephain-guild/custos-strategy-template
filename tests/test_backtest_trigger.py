"""Trigger observations are optional, immutable and available no earlier than observed."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from custos_toolkit.signals import Signal

from tests.test_backtest_collector import Probe, run_probe
from tests.test_backtest_report import T, data
from tools.backtest.collector import ChartCollector
from tools.backtest.report import ChartError, validate_report, validate_trigger_details


def trigger():
    return {
        "version": 1,
        "observed_time_ns": str(T),
        "reason_code": "custom_cross",
        "reason_i18n": {"zh": "自定义阈值确认", "en": "Custom threshold confirmation"},
        "fields": [
            {
                "key": "level",
                "label_i18n": {"zh": "阈值", "en": "Threshold"},
                "format": "number",
                "value": "95.00",
            },
            {
                "key": "at",
                "label_i18n": {"zh": "确认时间", "en": "Confirmation time"},
                "format": "time",
                "value": str(T),
            },
        ],
    }


def test_trigger_details_are_copied_when_the_signal_is_observed():
    snapshot = trigger()
    signal = Signal.enter_long(price=100, pair="BTC-USDT")
    signal.metadata["trigger_details"] = snapshot
    collector = ChartCollector()
    collector.record_signal(
        SimpleNamespace(instrument_id="BTC.TEST"), signal, SimpleNamespace(bar_type="daily"), T
    )
    frozen = deepcopy(collector.signals[0]["metadata"]["trigger_details"])
    signal.metadata["trigger_details"]["fields"][0]["value"] = "1000"
    assert collector.signals[0]["metadata"]["trigger_details"] == frozen


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(version=True),
        lambda s: s.update(observed_time_ns=str(T + 1)),
        lambda s: s["reason_i18n"].pop("en"),
        lambda s: s["fields"].append(deepcopy(s["fields"][0])),
        lambda s: s["fields"][0].update(value=True),
        lambda s: s["fields"][0].update(value=float("nan")),
        lambda s: s["fields"][0].update(value="1_000"),
        lambda s: s["fields"][0].update(format="script"),
        lambda s: s["fields"][1].update(value=str(T + 1)),
    ],
)
def test_malformed_and_future_trigger_observations_are_rejected(mutation):
    snapshot = trigger()
    mutation(snapshot)
    with pytest.raises(ChartError):
        validate_trigger_details(snapshot, T)


def test_old_reports_stay_valid_and_optional_trigger_data_cannot_backdate_a_signal():
    report = data()
    validate_report(report)
    report["signals"][0]["metadata"]["trigger_details"] = trigger()
    validate_report(report)
    report["signals"][0]["metadata"]["trigger_details"]["observed_time_ns"] = str(T + 1)
    with pytest.raises(ChartError, match="after its signal"):
        validate_report(report)


def test_bad_trigger_observation_does_not_change_native_trades(tmp_path, monkeypatch):
    from nautilus_trader.model import Venue

    plain = run_probe(tmp_path, False)
    calculate = Probe.calculate_signal
    finish = ChartCollector.finish
    observed = []

    def bad(self, ctx, bar):
        signal = calculate(self, ctx, bar)
        signal.metadata["trigger_details"] = {"version": 999}
        return signal

    monkeypatch.setattr(Probe, "calculate_signal", bad)

    def finish_after_execution(self, strategy, cache):
        orders = sorted(
            (str(o.side), str(o.quantity), str(o.status), o.ts_init, o.ts_last)
            for o in cache.orders()
        )
        account = cache.account_for_venue(Venue("BINANCE"))
        instrument = cache.instruments()[0]
        observed.append(
            (
                orders,
                str(account.balance_total(instrument.quote_currency)),
                (strategy.calls, strategy.posts, strategy.fills),
            )
        )
        return finish(self, strategy, cache)

    monkeypatch.setattr(ChartCollector, "finish", finish_after_execution)
    with pytest.raises(ChartError, match="trigger"):
        run_probe(tmp_path)
    assert observed == [plain[:3]]
