"""Exit-ladder scorer: hand-built 15-minute bars, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trader.calls.parse import CallLevels, CallUpdate
from trader.calls.score import CallScore, Fill, simulate, size_usd
from trader.core.symbols import AssetClass
from trader.core.types import Bar
from trader.eval.costs import ReplayCosts

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
FREE = ReplayCosts()


def bars(*ohlc: tuple[float, float, float, float], start: datetime = T0) -> list[Bar]:
    return [
        Bar("DOGE/USD", "15m", start + timedelta(minutes=15 * i), o, h, lo, c, 1.0)
        for i, (o, h, lo, c) in enumerate(ohlc)
    ]


def call(
    *,
    side: str = "long",
    entry: float | None = 100.0,
    tps: list[float] | None = None,
    stop: float | None = None,
    allocation_pct: float | None = None,
    leverage: float | None = None,
) -> CallLevels:
    return CallLevels(
        coin="DOGE",
        side=side,
        entry=entry,
        tps=[110.0, 120.0, 130.0] if tps is None else tps,
        stop=stop,
        no_stop=False,
        allocation_pct=allocation_pct,
        leverage=leverage,
    )


def update(event: str, *, new_stop: float | None = None) -> CallUpdate:
    return CallUpdate(coin="DOGE", event=event, tp_level=None, new_stop=new_stop)


def run(levels, posted_at, series, *, updates=(), stop_frac=0.08, costs=FREE) -> CallScore:
    return simulate(
        levels,
        posted_at,
        list(updates),
        series,
        stop_frac=stop_frac,
        costs=costs,
        asset_class=AssetClass.CRYPTO,
    )


def test_entry_is_next_bar_open_after_post():
    series = bars((99, 99, 99, 99), (101, 102, 100, 101), (101, 102, 100, 101))
    score = run(call(entry=100.0), T0 + timedelta(minutes=7), series)
    assert score.entry == 101.0  # the 10:15 bar's open, not the 10:00 bar's
    assert score.fills[0] == Fill(T0 + timedelta(minutes=15), 101.0, 1.0, "entry")
    assert score.stated_entry == 100.0
    assert score.slippage_pct == pytest.approx(1.0)


def test_post_on_a_boundary_uses_that_bar():
    series = bars((99, 99, 99, 99), (101, 102, 100, 101))
    score = run(call(), T0 + timedelta(minutes=15), series)
    assert score.entry == 101.0


def test_thirds_at_each_tp_and_breakeven_after_tp1():
    series = bars((100, 111, 99, 110), (110, 110, 100, 100))
    score = run(call(stop=None), T0, series)
    assert score.status == "closed"
    assert score.stop_used == pytest.approx(92.0)
    exits = [(f.reason, f.price, f.fraction) for f in score.fills[1:]]
    assert exits == [
        ("tp1", 110.0, pytest.approx(1 / 3)),
        ("breakeven", 100.0, pytest.approx(2 / 3)),
    ]
    assert score.net_return == pytest.approx(0.10 / 3, abs=1e-9)
    assert score.first_touch == "tp1"
    assert score.hours_held == pytest.approx(0.25)


def test_all_three_tps_fill_in_order():
    series = bars((100, 125, 99, 120), (120, 131, 119, 130))
    score = run(call(), T0, series)
    assert [f.reason for f in score.fills[1:]] == ["tp1", "tp2", "tp3"]
    assert sum(f.fraction for f in score.fills[1:]) == pytest.approx(1.0)
    assert score.net_return == pytest.approx((0.10 + 0.20 + 0.30) / 3)
    assert score.status == "closed"


def test_stop_counts_first_when_one_bar_touches_both():
    series = bars((100, 112, 91, 100))
    score = run(call(), T0, series)
    assert [(f.reason, f.price, f.fraction) for f in score.fills[1:]] == [("stop", 92.0, 1.0)]
    assert score.net_return == pytest.approx(-0.08)
    assert score.first_touch == "stop"


def test_tighter_of_call_stop_and_frac():
    series = bars((100, 101, 99, 100))
    assert run(call(stop=90.0), T0, series).stop_used == pytest.approx(92.0)
    assert run(call(stop=95.0), T0, series).stop_used == pytest.approx(95.0)


def test_closed_update_exits_the_rest_at_next_bar_open():
    series = bars((100, 105, 99, 104), (104, 106, 103, 105), (106, 107, 105, 106))
    posted = T0 + timedelta(minutes=20)  # takes effect at the 10:30 bar
    score = run(call(), T0, series, updates=[(posted, update("closed"))])
    assert score.fills[-1] == Fill(T0 + timedelta(minutes=30), 106.0, 1.0, "closed")
    assert score.status == "closed"
    assert score.net_return == pytest.approx(0.06)
    assert score.first_touch == "neither"


def test_stop_moved_update_only_tightens():
    # A looser move (85) is ignored; a tighter one (97) then stops the trade out.
    series = bars((100, 101, 99, 100), (100, 101, 98, 99), (99, 100, 96, 97))
    score = run(
        call(),
        T0,
        series,
        updates=[
            (T0 + timedelta(minutes=5), update("stop_moved", new_stop=85.0)),
            (T0 + timedelta(minutes=20), update("stop_moved", new_stop=97.0)),
        ],
    )
    assert [(f.reason, f.price, f.ts) for f in score.fills[1:]] == [
        ("stop", 97.0, T0 + timedelta(minutes=30))
    ]
    assert score.stop_used == pytest.approx(92.0)  # the starting stop, not the moved one


def test_stop_to_breakeven_update_and_tp_hit_changes_nothing():
    series = bars((100, 105, 99, 104), (104, 105, 99.5, 100))
    score = run(
        call(),
        T0,
        series,
        updates=[
            (T0 + timedelta(minutes=1), update("tp_hit")),
            (T0 + timedelta(minutes=10), update("stop_to_breakeven")),
        ],
    )
    assert [(f.reason, f.price, f.fraction) for f in score.fills[1:]] == [("breakeven", 100.0, 1.0)]


def test_stop_gapped_through_fills_at_the_open():
    series = bars((100, 111, 99, 110), (95, 96, 94, 95))
    score = run(call(), T0, series)
    assert score.fills[-1] == Fill(
        T0 + timedelta(minutes=15), 95.0, pytest.approx(2 / 3), "breakeven"
    )


def test_short_is_mirrored():
    series = bars((100, 101, 89, 90), (90, 100.5, 89, 100))
    score = run(call(side="short", tps=[90.0, 80.0], stop=105.0), T0, series)
    assert score.stop_used == pytest.approx(105.0)  # tighter than 108
    assert [(f.reason, f.price, f.fraction) for f in score.fills[1:]] == [
        ("tp1", 90.0, 0.5),
        ("breakeven", 100.0, 0.5),
    ]
    assert score.net_return == pytest.approx(0.05)
    assert score.first_touch == "tp1"


def test_short_stop_from_frac_and_loss():
    series = bars((100, 109, 99, 108))
    score = run(call(side="short", tps=[90.0], stop=None), T0, series)
    assert score.stop_used == pytest.approx(108.0)
    assert score.net_return == pytest.approx(-0.08)


def test_tps_on_the_wrong_side_of_the_actual_entry_are_dropped():
    # Entry slipped to 115: TP1 (110) is gone, TP2 and TP3 take halves.
    series = bars((115, 121, 114, 120), (120, 131, 119, 130))
    score = run(call(), T0, series)
    assert [(f.reason, f.price, f.fraction) for f in score.fills[1:]] == [
        ("tp1", 120.0, 0.5),
        ("tp2", 130.0, 0.5),
    ]


def test_no_tps_left_holds_to_the_end_of_bars():
    series = bars((140, 145, 139, 141), (141, 150, 140, 148))
    score = run(call(), T0, series)
    assert score.status == "open"
    assert score.fills[-1] == Fill(T0 + timedelta(minutes=15), 148.0, 1.0, "mark")
    assert score.first_touch == "neither"


def test_open_when_bars_run_out():
    series = bars((100, 111, 99, 110), (110, 115, 105, 112))
    score = run(call(), T0, series)
    assert score.status == "open"
    assert score.fills[-1] == Fill(T0 + timedelta(minutes=15), 112.0, pytest.approx(2 / 3), "mark")
    assert score.net_return == pytest.approx(0.10 / 3 + 0.12 * 2 / 3)


def test_unscorable_without_bars_after_post():
    score = run(call(), T0 + timedelta(hours=1), bars((100, 101, 99, 100)))
    assert score.status == "unscorable"
    assert score.reason == "no bars after post"
    assert score.entry is None and score.fills == () and score.net_return == 0.0
    assert score.as_dict()["status"] == "unscorable"


def test_costs_reduce_net():
    series = bars((100, 101, 91, 95))
    gross_only = run(call(), T0, series)
    costed = run(call(), T0, series, costs=ReplayCosts(crypto_taker_fee_bps=25.0))
    assert costed.gross_return == pytest.approx(gross_only.gross_return)
    assert costed.cost_return == pytest.approx(2 * 0.0025)
    assert costed.net_return == pytest.approx(costed.gross_return - 2 * 0.0025)


def test_as_dict_is_json_ready():
    d = run(call(), T0, bars((100, 112, 91, 100))).as_dict()
    assert d["fills"][0] == {
        "ts": T0.isoformat(),
        "price": 100.0,
        "fraction": 1.0,
        "reason": "entry",
    }
    assert d["first_touch"] == "stop"


def test_size_usd():
    assert size_usd(call(), 10_000) == pytest.approx(100.0)
    assert size_usd(call(allocation_pct=0.5, leverage=10), 10_000) == pytest.approx(500.0)
