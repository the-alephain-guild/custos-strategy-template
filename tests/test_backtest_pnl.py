"""Independent fill accounting vectors, including fees, partial exits and flips."""

from copy import deepcopy
from decimal import Decimal
from fractions import Fraction

import pytest

from tools.backtest.pnl import annotate_fills


def spec(identity="BTC.TEST", **changes):
    return {
        **dict(
            id=identity,
            price_model="linear",
            pnl_currency="USDT",
            pnl_precision=8,
            multiplier="1",
        ),
        **changes,
    }


def fills(*values, instrument="BTC.TEST"):
    return [
        dict(
            id=f"f{i}",
            instrument_id=instrument,
            event_time_ns=str(i),
            side=side,
            price=str(price),
            quantity=str(quantity),
            commission=fee,
        )
        for i, (side, quantity, price, fee) in enumerate(values, 1)
    ]


def numbers(rows, key):
    return [None if row["pnl"][key] is None else Decimal(row["pnl"][key]) for row in rows]


@pytest.mark.parametrize(
    "values,notional",
    [
        (
            (
                ("BUY", 1, 100, "1 USDT"),
                ("BUY", 1, 120, "1 USDT"),
                ("SELL", ".5", 130, ".5 USDT"),
                ("SELL", "1.5", 90, "1.5 USDT"),
            ),
            "55",
        ),
        (
            (
                ("SELL", 1, 100, "1 USDT"),
                ("SELL", 1, 80, "1 USDT"),
                ("BUY", ".5", 70, ".5 USDT"),
                ("BUY", "1.5", 110, "1.5 USDT"),
            ),
            "45",
        ),
    ],
)
def test_scaled_entries_and_partial_exits_include_all_fees_once(values, notional):
    source = fills(*values)
    before = deepcopy(source)
    rows = annotate_fills(source, [spec()])
    assert source == before
    assert numbers(rows, "realized_net") == [
        Decimal("-1"),
        Decimal("-1"),
        Decimal("9.5"),
        Decimal("-31.5"),
    ]
    assert numbers(rows, "cumulative_net") == [
        Decimal("-1"),
        Decimal("-2"),
        Decimal("7.5"),
        Decimal("-24"),
    ]
    assert rows[0]["pnl"]["return_pct"] is None
    assert Decimal(rows[2]["pnl"]["entry_notional"]) == Decimal(notional)
    assert Decimal(rows[2]["pnl"]["closed_net"]) == Decimal("9")
    assert Decimal(rows[2]["pnl"]["entry_fee_allocated"]) == Decimal(".5")
    assert Decimal(rows[2]["pnl"]["return_pct"]) == pytest.approx(
        Decimal("9") / Decimal(notional) * 100
    )


@pytest.mark.parametrize(
    "values,total",
    [
        ((("BUY", 1, 100, "1 USDT"), ("SELL", 2, 110, "2 USDT"), ("BUY", 1, 100, "1 USDT")), "16"),
        ((("SELL", 1, 100, "1 USDT"), ("BUY", 3, 90, "3 USDT"), ("SELL", 2, 95, "2 USDT")), "14"),
    ],
)
def test_both_reversal_directions_split_fee_and_cost_without_double_counting(values, total):
    rows = annotate_fills(fills(*values), [spec()])
    flip = rows[1]["pnl"]
    assert Decimal(flip["realized_gross"]) == 10
    assert Decimal(flip["closed_quantity"]) == 1
    assert Decimal(flip["closed_net"]) == 8
    assert Decimal(flip["exit_fee_allocated"]) == 1
    assert Decimal(rows[-1]["pnl"]["cumulative_net"]) == Decimal(total)


def test_future_entries_cannot_change_an_earlier_partial_close_or_row_identity():
    source = fills(
        ("BUY", 1, 100, "1 USDT"),
        ("SELL", ".5", 120, ".5 USDT"),
        ("BUY", ".5", 200, ".5 USDT"),
        ("SELL", 1, 180, "1 USDT"),
    )
    full = annotate_fills(source, [spec()])
    prefix = annotate_fills(source[:2], [spec()])
    changed = deepcopy(source)
    changed[2]["price"] = "2"
    assert full[:2] == prefix == annotate_fills(changed, [spec()])[:2]
    assert Decimal(full[1]["pnl"]["return_pct"]) == 18
    assert [row["id"] for row in full] == [row["id"] for row in source]
    assert Decimal(full[-1]["pnl"]["cumulative_net"]) == 37


def test_same_timestamp_keeps_callback_order_instead_of_sorting_random_ids():
    source = fills(("BUY", 1, 100, "1 USDT"), ("SELL", 1, 110, "1 USDT"))
    source[0]["id"] = "z"
    source[1]["id"] = "a"
    source[1]["event_time_ns"] = "1"
    rows = annotate_fills(source, [spec()])
    assert rows[0]["pnl"]["return_pct"] is None
    assert Decimal(rows[1]["pnl"]["return_pct"]) == 8
    assert Decimal(rows[1]["pnl"]["cumulative_net"]) == 8


def test_instrument_ledgers_and_closed_cycles_remain_independent():
    a = fills(("BUY", 1, 100, "1 USDT"), ("SELL", 1, 110, "1 USDT"))
    b = fills(("BUY", 2, 10, "1 USDT"), ("SELL", 2, 12, "1 USDT"), instrument="ETH.TEST")
    for row in b:
        row["id"] = "b" + row["id"]
    rows = annotate_fills([a[0], b[0], a[1], b[1]], [spec(), spec("ETH.TEST")])
    assert numbers(rows, "cumulative_net") == [
        Decimal("-1"),
        Decimal("-1"),
        Decimal("8"),
        Decimal("2"),
    ]
    cycles = fills(
        ("BUY", 1, 100, "1 USDT"),
        ("SELL", 1, 110, "1 USDT"),
        ("SELL", 1, 110, "1 USDT"),
        ("BUY", 1, 100, "1 USDT"),
    )
    assert numbers(annotate_fills(cycles, [spec()]), "cumulative_net") == [
        Decimal("-1"),
        Decimal("8"),
        Decimal("7"),
        Decimal("16"),
    ]


def test_multiplier_and_maker_rebates_are_used_instead_of_assuming_unit_contracts():
    source = fills(
        ("BUY", 2, 100, "-2 USDT"), ("SELL", 1, 110, "1 USDT"), ("SELL", 1, 110, "1 USDT")
    )
    instrument = spec()
    instrument["multiplier"] = "10"
    rows = annotate_fills(source, [instrument])
    assert numbers(rows, "cumulative_net") == [Decimal("2"), Decimal("101"), Decimal("200")]
    assert Decimal(rows[1]["pnl"]["entry_notional"]) == 1000
    assert Decimal(rows[1]["pnl"]["return_pct"]) == 10


@pytest.mark.parametrize(
    "problem",
    [
        "foreign_fee",
        "missing_fee",
        "nonfinite",
        "history_gap",
        "missing_instrument",
        "inverse",
        "multiplier",
    ],
)
def test_incomplete_or_unsupported_economics_are_not_disguised_as_zero_profit(problem):
    source = fills(("BUY", 1, 100, "1 USDT"), ("SELL", 1, 110, "1 USDT"))
    instruments = [spec()]
    if problem == "foreign_fee":
        source[0]["commission"] = ".1 BNB"
    if problem == "missing_fee":
        source[0].pop("commission")
    if problem == "nonfinite":
        source[0]["price"] = "NaN"
    if problem == "history_gap":
        source[0]["position_before"] = "2"
    if problem == "missing_instrument":
        instruments = []
    if problem == "inverse":
        instruments[0]["price_model"] = "inverse"
    if problem == "multiplier":
        instruments[0]["multiplier"] = "0"
    rows = annotate_fills(source, instruments)
    for row in rows:
        assert row["pnl"]["status"] == "unavailable" and row["pnl"]["reason"]
        assert row["pnl"]["realized_net"] is None
        assert row["pnl"]["cumulative_net"] is None


def test_structured_fee_values_are_authoritative_and_zero_foreign_fee_needs_no_fx():
    source = fills(("BUY", 1, 100, "display-only text"), ("SELL", 1, 110, "0 BNB"))
    source[0].update(commission_amount="1", commission_currency="USDT")
    rows = annotate_fills(source, [spec()])
    assert numbers(rows, "cumulative_net") == [Decimal("-1"), Decimal("9")]


def test_out_of_order_list_is_valued_chronologically_and_duplicate_fills_are_refused():
    source = fills(("BUY", 1, 100, "1 USDT"), ("SELL", 1, 110, "1 USDT"))
    rows = annotate_fills(list(reversed(source)), [spec()])
    assert [row["id"] for row in rows] == ["f2", "f1"]
    assert numbers(rows, "cumulative_net") == [Decimal("8"), Decimal("-1")]
    with pytest.raises(ValueError, match="duplicate"):
        annotate_fills([source[0], source[0]], [spec()])


def test_partial_fee_pool_residue_is_consumed_on_final_close():
    source = fills(
        ("BUY", 3, 100, "1 USDT"),
        ("SELL", 1, 110, ".1 USDT"),
        ("SELL", 1, 110, ".1 USDT"),
        ("SELL", 1, 110, ".1 USDT"),
    )
    rows = annotate_fills(source, [spec()])
    assert sum(Fraction(row["pnl"]["entry_fee_allocated"]) for row in rows) == 1
    assert Decimal(rows[-1]["pnl"]["cumulative_net"]) == Decimal("28.7")


def test_unknown_fee_taints_only_this_instrument_from_that_fill_forward():
    a = fills(("BUY", 1, 100, "1 USDT"), ("SELL", 1, 110, "1 BNB"), ("BUY", 1, 100, "1 USDT"))
    b = fills(("BUY", 1, 10, "1 EUR"), ("SELL", 1, 15, "1 EUR"), instrument="ETH.TEST")
    for row in b:
        row["id"] = "b" + row["id"]
    result = annotate_fills(a + b, [spec(), spec("ETH.TEST", pnl_currency="EUR")])
    assert result[0]["pnl"]["realized_net"] == "-1"
    assert result[1]["pnl"]["status"] == result[2]["pnl"]["status"] == "unavailable"
    assert result[-1]["pnl"]["cumulative_net"] == "3"
