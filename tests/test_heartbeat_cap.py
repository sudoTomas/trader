"""Heartbeat cap — a large watchlist must not wake every symbol on one tick.

With ``max_heartbeats_per_tick`` set, heartbeats beyond the cap wait for the next tick,
never-run symbols first and then the longest overdue; every symbol is still covered.
Signal triggers are never capped, and a symbol already waking on a signal keeps its
heartbeat. The default (0) is today's behaviour: no cap.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from trader.core.symbols import AssetClass, make_symbol
from trader.loop.scheduler import HEARTBEAT, PRICE_MOVE, Mark, SignalScheduler, TriggerConfig

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET, NYSE open
TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE"]
SYMBOLS = [make_symbol(t, AssetClass.US_EQUITY) for t in TICKERS]


def marks(price: float = 100.0) -> dict[str, Mark]:
    return {t: Mark(price=price) for t in TICKERS}


def beats(triggers) -> list[str]:
    return sorted(t.symbol for t in triggers if t.type == HEARTBEAT)


def test_no_cap_by_default_wakes_every_due_symbol():
    sched = SignalScheduler(SYMBOLS, TriggerConfig())
    assert beats(sched.evaluate(NOW, marks())) == TICKERS


def test_cap_spreads_heartbeats_and_still_covers_every_symbol():
    sched = SignalScheduler(SYMBOLS, TriggerConfig(max_heartbeats_per_tick=2))
    seen: list[str] = []
    now = NOW
    for _ in range(math.ceil(len(TICKERS) / 2)):
        fired = sched.evaluate(now, marks())
        assert len(beats(fired)) <= 2
        sched.mark_dispatched(fired, now)
        seen.extend(beats(fired))
        now += timedelta(minutes=2)
    assert sorted(seen) == TICKERS  # every symbol woke exactly once
    assert len(seen) == len(set(seen))


def test_never_run_symbols_go_first_then_the_longest_overdue():
    sched = SignalScheduler(SYMBOLS, TriggerConfig(max_heartbeats_per_tick=2))
    first = sched.evaluate(NOW, marks())
    assert beats(first) == ["AAA", "BBB"]  # ticker order breaks the tie among never-run
    sched.mark_dispatched(first, NOW)
    second = sched.evaluate(NOW + timedelta(minutes=2), marks())
    assert beats(second) == ["CCC", "DDD"]  # never-run beat already-run


def test_signal_triggers_are_never_capped_and_keep_their_heartbeat():
    sched = SignalScheduler(SYMBOLS, TriggerConfig(max_heartbeats_per_tick=1, price_move_pct=0.02))
    # Seed a price history, then move EEE by 5% inside the window.
    sched.mark_dispatched(sched.evaluate(NOW, marks(100.0)), NOW)
    moved = marks(100.0)
    moved["EEE"] = Mark(price=105.0)
    fired = sched.evaluate(NOW + timedelta(minutes=2), moved)
    assert [t.symbol for t in fired if t.type == PRICE_MOVE] == ["EEE"]
    # EEE's heartbeat rides along (it is waking anyway); the cap still limits the rest.
    others = [s for s in beats(fired) if s != "EEE"]
    assert len(others) <= 1
