"""The learning loop (#143): Settlement on fixture bars, the reflect Role's Lessons, the
lessons Source's point-in-time read (live and in replay) and the per-seat Scorecard
against a hand-worked example. Market data and models are stubbed; the store is real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_loop import StubLLM, StubMarketData
from tests.test_replay_eval import HOLD, NOW, seed_hourly_bars
from trader.core.symbols import default_registry
from trader.core.types import Bar
from trader.learning.reflect import REFLECT_SCHEMA, Reflector, reflect_prompt
from trader.learning.scorecard import scorecard, seat_views, spearman
from trader.learning.settlement import Settler, correct, direction, measure
from trader.loop.sources import LESSONS, LessonsSource
from trader.store.db import Database
from trader.store.repos import DecisionsRepo, LessonsRepo, SettlementsRepo

REG = default_registry()
GLD, SPY, ETH, BTC = (REG.get(t) for t in ("GLD", "SPY", "ETH/USD", "BTC/USD"))

# Daily bars stamp the session's open time; a weekday series from Thu 2026-10-01.
DECIDED = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # Monday, mid-session
GLD_CLOSES = [100, 101, 100, 102, 103, 104, 105, 110, 111]  # Oct 1, 2, 5, 6, 7, 8, 9, 12, 13
SPY_CLOSES = [495, 498, 500, 501, 503, 505, 506, 510, 512]


def sessions(start: datetime, count: int, *, weekdays_only: bool = True) -> list[datetime]:
    days: list[datetime] = []
    day = start
    while len(days) < count:
        if not weekdays_only or day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def daily(ticker: str, closes: list[float], *, weekdays_only: bool = True) -> list[Bar]:
    start = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    return [
        Bar(ticker, "1d", ts, c, c, c, c, 1_000.0)
        for ts, c in zip(
            sessions(start, len(closes), weekdays_only=weekdays_only), closes, strict=True
        )
    ]


class DailyData:
    """Daily bars by ticker; records every fetch so a test sees nothing is refetched."""

    name = "stub-daily"

    def __init__(self, series: dict[str, list[Bar]], *, down: set[str] = frozenset()) -> None:
        self.series = series
        self.down = down
        self.calls: list[tuple[str, str]] = []

    async def get_bars(self, symbol, interval, lookback):
        from trader.data.base import MarketDataError

        self.calls.append((symbol.ticker, interval))
        if symbol.ticker in self.down:
            raise MarketDataError("provider down")
        return self.series[symbol.ticker][-lookback:]


# --- Settlement on fixture bars ---------------------------------------------------


def test_a_decision_settles_five_sessions_after_the_session_it_was_made_in():
    outcome = measure(
        DECIDED,
        daily("GLD", GLD_CLOSES),
        daily("SPY", SPY_CLOSES),
        now=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert outcome is not None
    # Entry is Monday's close (the bar the decision was made in), exit five sessions on:
    # Tue, Wed, Thu, Fri, then the next Monday.
    assert outcome.entry_ts == datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
    assert outcome.exit_ts == datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
    assert (outcome.entry_price, outcome.exit_price) == (100, 110)
    assert outcome.forward_return == pytest.approx(0.10)
    assert outcome.benchmark_return == pytest.approx(0.02)  # SPY 500 -> 510
    assert outcome.excess_return == pytest.approx(0.08)
    # Known once the exit bar has ended; a Lesson is readable from here on.
    assert outcome.settled_at == datetime(2026, 10, 13, 4, 0, tzinfo=UTC)


def test_a_window_that_has_not_fully_traded_stays_pending():
    bars, bench = daily("GLD", GLD_CLOSES), daily("SPY", SPY_CLOSES)
    # The exit bar exists but has not ended yet.
    assert measure(DECIDED, bars, bench, now=datetime(2026, 10, 12, 20, tzinfo=UTC)) is None
    # The exit session has not happened at all.
    assert measure(DECIDED, bars[:7], bench, now=datetime(2026, 10, 20, tzinfo=UTC)) is None
    # The benchmark has not traded through the exit.
    assert measure(DECIDED, bars, bench[:7], now=datetime(2026, 10, 20, tzinfo=UTC)) is None


def test_crypto_counts_calendar_days_and_settles_against_btc():
    eth = daily("ETH/USD", [2000, 2010, 2020, 2030, 2040, 2050, 2060, 2100], weekdays_only=False)
    btc = daily("BTC/USD", [60000] * 3 + [61000] * 5, weekdays_only=False)
    decided = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)  # inside the Oct 2 bar
    outcome = measure(decided, eth, btc, now=datetime(2026, 10, 9, tzinfo=UTC))
    assert outcome is not None
    assert outcome.exit_ts == datetime(2026, 10, 7, 4, 0, tzinfo=UTC)  # five days, weekend in
    assert outcome.forward_return == pytest.approx(2060 / 2010 - 1)
    assert outcome.benchmark_return == pytest.approx(61000 / 60000 - 1)


def test_a_decisions_view_and_whether_it_was_right():
    assert direction({"action": "open", "intent": {"side": "buy"}}) == 1
    assert direction({"action": "open", "intent": {"side": "sell"}}) == -1
    assert direction({"action": "close", "intent": {"side": "sell"}}) == -1  # a long's close
    assert direction({"action": "close", "intent": {"side": "buy"}}) == 1  # a short's close
    assert direction({"action": "hold", "intent": None}) == 0
    assert correct(1, 0.02) is True and correct(-1, 0.02) is False
    assert correct(0, 0.05) is None  # a hold is not a directional call


# --- the settlement pass ----------------------------------------------------------


def decision_json(action: str, side: str | None = None, conviction: float = 0.7) -> dict:
    intent = None if side is None else {"symbol": "GLD", "side": side, "notional": 500.0}
    return {
        "symbol": "GLD",
        "action": action,
        "conviction": conviction,
        "rationale": f"{action} because",
        "intent": intent,
    }


async def journal(
    db: Database, ts: datetime, decision: dict | None, *, symbol: str = "GLD", **row
) -> int:
    decision_id = await DecisionsRepo(db).insert(
        trigger="heartbeat",
        status="shadow",
        symbol=symbol,
        asset_class="us_equity",
        prompt=row.pop("prompt", f"Symbol: {symbol}\nLast price: 100"),
        decision=decision,
        **row,
    )
    await db.execute("UPDATE decisions SET ts = %s WHERE id = %s", (ts, decision_id))
    return decision_id


class StubReflector:
    def __init__(self, fail_on: set[int] = frozenset()) -> None:
        self.seen: list[int] = []
        self.fail_on = fail_on

    async def lesson(self, row):
        self.seen.append(row["decision_id"])
        if row["decision_id"] in self.fail_on:
            from trader.llm.base import LLMError

            raise LLMError("capped")
        return f"lesson on #{row['decision_id']}", "xai:grok-test"


def fixture_data() -> DailyData:
    return DailyData({"GLD": daily("GLD", GLD_CLOSES), "SPY": daily("SPY", SPY_CLOSES)})


async def test_a_pass_settles_every_journaled_decision_and_writes_lessons(db: Database):
    opened = await journal(db, DECIDED, decision_json("open", "buy"))
    closed = await journal(db, DECIDED, decision_json("close", "sell"))
    held = await journal(db, DECIDED, decision_json("hold"))
    await journal(db, DECIDED, None)  # an error row: nothing to settle
    recent = await journal(db, datetime(2026, 10, 12, 15, tzinfo=UTC), decision_json("hold"))
    data, reflector = fixture_data(), StubReflector()

    out = await Settler(db, data, symbols=REG, reflector=reflector).settle(
        datetime(2026, 10, 14, tzinfo=UTC)
    )

    assert out == {"settled": 3, "pending": 0, "failed": [], "lessons": 3}
    # Bars once per symbol per pass, daily, nothing stored.
    assert sorted(data.calls) == [("GLD", "1d"), ("SPY", "1d")]
    assert await db.fetch_all("SELECT * FROM bars") == []
    rows = {
        r["decision_id"]: r
        for r in await db.fetch_all("SELECT * FROM decision_settlements ORDER BY decision_id")
    }
    assert set(rows) == {opened, closed, held}  # the 2-day-old hold is not due yet
    assert (rows[opened]["direction"], rows[opened]["correct"]) == (1, True)
    assert (rows[closed]["direction"], rows[closed]["correct"]) == (-1, False)
    assert (rows[held]["direction"], rows[held]["correct"]) == (0, None)
    assert rows[opened]["benchmark"] == "SPY" and rows[opened]["bot_id"] == 1
    assert rows[held]["excess_return"] == pytest.approx(0.08)

    lessons = await db.fetch_all("SELECT * FROM lessons ORDER BY decision_id")
    assert [lesson["decision_id"] for lesson in lessons] == [opened, closed, held]
    assert all(
        lesson["available_at"] == datetime(2026, 10, 13, 4, tzinfo=UTC) for lesson in lessons
    )
    assert lessons[0]["written_by"] == "xai:grok-test"

    # A second pass finds nothing new; the young decision is still not due.
    again = await Settler(db, data, symbols=REG, reflector=reflector).settle(
        datetime(2026, 10, 14, tzinfo=UTC)
    )
    assert (again["settled"], again["lessons"]) == (0, 0)
    assert recent not in {r["decision_id"] for r in await db.fetch_all("SELECT * FROM lessons")}


async def test_settlements_and_lessons_are_append_only(db: Database):
    decision_id = await journal(db, DECIDED, decision_json("open", "buy"))
    await Settler(db, fixture_data(), symbols=REG, reflector=StubReflector()).settle(
        datetime(2026, 10, 14, tzinfo=UTC)
    )
    for table in ("decision_settlements", "lessons"):
        with pytest.raises(Exception, match="append-only"):
            await db.execute(f"DELETE FROM {table} WHERE decision_id = %s", (decision_id,))


async def test_a_down_symbol_stays_pending_and_a_capped_role_stops_the_lessons(db: Database):
    first = await journal(db, DECIDED, decision_json("open", "buy"))
    second = await journal(db, DECIDED, decision_json("hold"))
    await journal(db, DECIDED, decision_json("hold"), symbol="SLV")
    data = fixture_data()
    data.down = {"SLV"}
    # Newest first: the second decision is reflected first and the Role is capped there.
    reflector = StubReflector(fail_on={second})

    out = await Settler(db, data, symbols=REG, reflector=reflector).settle(
        datetime(2026, 10, 14, tzinfo=UTC)
    )

    assert out["settled"] == 2 and out["pending"] == 1
    assert out["failed"][0]["symbol"] == "SLV"
    assert out["lessons"] == 0 and reflector.seen == [second]  # stopped, not skipped past
    assert first not in reflector.seen


async def test_the_reflect_role_writes_a_short_lesson_from_the_outcome(db: Database):
    decision_id = await journal(db, DECIDED, decision_json("open", "buy"), prompt="Symbol: GLD")
    await Settler(db, fixture_data(), symbols=REG).settle(datetime(2026, 10, 14, tzinfo=UTC))
    (row,) = await SettlementsRepo(db).unreflected(10)

    prompt = reflect_prompt(row)
    assert "Symbol: GLD" in prompt
    assert "returned +10.00%" in prompt and "SPY +2.00%" in prompt and "excess +8.00%" in prompt
    assert "it was right" in prompt

    llm = StubLLM([json.dumps({"lesson": "Momentum carried; trust the trend."})])
    assert await Reflector(llm).lesson(row) == (
        "Momentum carried; trust the trend.",
        "nemotron:nemotron-3-super",
    )
    assert REFLECT_SCHEMA["required"] == ["lesson"]
    assert await Reflector(StubLLM(["not json"])).lesson(row) is None  # off-contract: skip
    assert row["decision_id"] == decision_id


# --- the lessons Source -------------------------------------------------------------


async def seed_lesson(
    db: Database, text: str, available_at: datetime, *, bot_id: int = 1, ticker: str = "BTC/USD"
) -> int:
    decision_id = await journal(
        db, available_at - timedelta(days=7), decision_json("open", "buy"), symbol=ticker
    )
    await SettlementsRepo(db).insert(
        decision_id=decision_id,
        bot_id=bot_id,
        symbol=ticker,
        horizon_days=5,
        entry_ts=available_at - timedelta(days=7),
        entry_price=100.0,
        exit_ts=available_at - timedelta(days=1),
        exit_price=103.0,
        benchmark="BTC/USD",
        benchmark_return=0.01,
        forward_return=0.03,
        excess_return=0.02,
        direction=1,
        correct=True,
        settled_at=available_at,
    )
    return await LessonsRepo(db).insert(
        decision_id=decision_id,
        bot_id=bot_id,
        symbol=ticker,
        lesson=text,
        written_by="stub",
        available_at=available_at,
    )


async def test_the_lessons_source_reads_only_this_bots_settled_lessons(db: Database):
    await db.execute("INSERT INTO bots (id, name) VALUES (2, 'bot-2')")
    early = await seed_lesson(db, "Early lesson.", NOW - timedelta(days=2))
    later = await seed_lesson(db, "Later lesson.", NOW - timedelta(hours=1))
    await seed_lesson(db, "Future lesson.", NOW + timedelta(hours=1))
    await seed_lesson(db, "Bot two's lesson.", NOW - timedelta(hours=2), bot_id=2)
    await seed_lesson(db, "ETH lesson.", NOW - timedelta(hours=2), ticker="ETH/USD")
    source = LessonsSource(LessonsRepo(db))

    live = await source.read(BTC, as_of=None, params={"now": NOW, "bot_id": 1})
    assert live is not None and live.source == LESSONS
    assert live.version == f"{later},{early}"  # newest first
    assert live.text.splitlines()[0].endswith("Later lesson.")
    assert "+2.0% vs BTC/USD over 5 sessions, right" in live.text
    assert "Future lesson." not in live.text  # not settled yet
    assert "Bot two" not in live.text and "ETH" not in live.text

    pinned = await source.read(BTC, as_of=NOW - timedelta(days=1), params={})
    assert pinned is not None and pinned.version == str(early)  # no Bot = Bot #1

    other = await source.read(BTC, as_of=None, params={"now": NOW, "bot_id": 2})
    assert other is not None and other.text.endswith("Bot two's lesson.")
    assert await source.read(BTC, as_of=NOW - timedelta(days=3), params={}) is None


async def test_a_replay_never_reads_a_lesson_settled_after_the_bar(db: Database):
    """The acceptance test: each replayed step sees exactly the Lessons whose Settlement
    had settled by that step, never one settled later."""
    from trader.eval.replay import ReplayHarness

    await seed_hourly_bars(db)
    start = NOW - timedelta(hours=3)
    await seed_lesson(db, "Settled before the window.", start - timedelta(hours=1))
    await seed_lesson(db, "Settled mid-window.", start + timedelta(minutes=30))
    await seed_lesson(db, "Settled after the window.", NOW + timedelta(days=1))

    harness = ReplayHarness(db, StubLLM([HOLD, HOLD, HOLD]), StubMarketData())
    run = await harness.run(
        BTC, start, NOW, label="lessons-pit", config={"lessons": True, "macro_view": False}
    )
    assert run["status"] == "done"
    steps = await db.fetch_all("SELECT ts, prompt FROM eval_steps ORDER BY ts")
    assert len(steps) == 3
    for step in steps:
        assert "Settled before the window." in step["prompt"]
        assert ("Settled mid-window." in step["prompt"]) == (
            step["ts"] >= start + timedelta(minutes=30)
        )
        assert "Settled after the window." not in step["prompt"]
    assert "Lessons from this Bot's settled decisions" in steps[1]["prompt"]

    # Off by default: a replay that does not ask reads no Lessons.
    plain = ReplayHarness(db, StubLLM([HOLD]), StubMarketData())
    await plain.run(BTC, NOW - timedelta(hours=1), NOW, label="no-lessons")
    last = await db.fetch_one("SELECT prompt FROM eval_steps ORDER BY id DESC LIMIT 1")
    assert "Lessons from" not in last["prompt"]


async def test_a_replay_with_named_sources_still_reads_lessons_when_asked(db: Database):
    """A Bot version's Source list (``config.sources``) plus ``lessons`` reads the Lessons
    too: the flag is not dropped just because the run names its Sources."""
    from trader.eval.replay import ReplayHarness

    await seed_hourly_bars(db)
    start = NOW - timedelta(hours=1)
    await seed_lesson(db, "Settled before the window.", start - timedelta(hours=1))
    harness = ReplayHarness(db, StubLLM([HOLD]), StubMarketData())
    await harness.run(
        BTC,
        start,
        NOW,
        label="named-sources-lessons",
        config={"sources": [{"source": "held_position"}], "lessons": True},
    )
    step = await db.fetch_one("SELECT prompt FROM eval_steps ORDER BY id DESC LIMIT 1")
    assert "Settled before the window." in step["prompt"]


async def test_a_blind_replay_drops_the_lessons_source():
    from trader.eval.blind import split_sources

    kept, dropped = split_sources([{"source": LESSONS}, {"source": "fred"}])
    assert dropped == [LESSONS] and [s["source"] for s in kept] == ["fred"]


def test_lessons_are_opt_in_per_bot():
    from trader.loop.bots import bot_one_spec, bot_spec
    from trader.ops.config import Settings

    assert LESSONS not in {
        s["source"] for s in bot_one_spec(Settings(auth_disabled=True))["sources"]
    }
    on = bot_one_spec(Settings(auth_disabled=True, lessons_enabled=True))
    assert LESSONS in {s["source"] for s in on["sources"]}
    spec = bot_spec(symbols={"GLD": "shadow"}, capital_usd=1000, sources=[LESSONS])
    assert spec["sources"] == [{"source": LESSONS, "params": {}}]


# --- the Scorecard: a hand-worked example --------------------------------------------


def settled(
    forward: float,
    excess: float,
    raw: dict,
    analysts: list[dict] | None = None,
    critic: str | None = None,
) -> dict:
    desk = {"analysts": analysts or []}
    if critic is not None:
        desk["critic"] = {"verdict": critic}
    return {
        "forward_return": forward,
        "excess_return": excess,
        "raw_output": json.dumps(raw),
        "decision": None,
        "desk_verdicts": desk,
    }


def draft(action: str, conviction: float, side: str | None = None) -> dict:
    return {"action": action, "conviction": conviction, "rationale": "r", "side": side}


def lane(lane_name: str, stance: str, confidence: float, ok: bool = True) -> dict:
    return {"lane": lane_name, "ok": ok, "stance": stance, "confidence": confidence}


ROWS = [
    settled(
        0.05,
        0.03,
        draft("open", 0.8, "buy"),
        [lane("technical", "bullish", 0.7), lane("research", "bullish", 0.5)],
        "approve",
    ),
    settled(
        -0.02,
        -0.04,
        draft("open", 0.6, "sell"),
        [lane("technical", "bearish", 0.5), lane("research", "bullish", 0.9, ok=False)],
        "pass_through",
    ),
    settled(0.01, -0.02, draft("open", 0.5, "buy"), [lane("technical", "bullish", 0.6)], "veto"),
    settled(0.03, 0.01, draft("hold", 0.4), [lane("technical", "neutral", 0.5)], "skipped"),
    settled(0.08, 0.06, draft("open", 0.9, "buy"), [lane("technical", "bearish", 0.4)], "shrink"),
]


def test_the_scorecard_matches_a_hand_worked_example():
    """Worked by hand:

    decide views (+.8, -.6, +.5, 0, +.9) against excess (+.03, -.04, -.02, +.01, +.06):
    four directional, three hits → hit rate 3/4; slugging mean(.03, .04, .06) / .02 = 13/6.
    Ranks of the views (4, 1, 3, 2, 5) vs the forward returns (4, 1, 2, 3, 5): Σd² = 2,
    rank IC = 1 - 6·2 / (5·24) = 0.9.

    technical (+.7, -.5, +.6, 0, -.4): hits on rows 1 and 2, misses on 3 and 5 → 1/2;
    slugging .035 / .04 = 0.875; ranks (5, 1, 4, 3, 2) vs (4, 1, 2, 3, 5), Σd² = 14,
    rank IC = 1 - 84 / 120 = 0.3.

    critic, as a call on the Draft's direction: approve +1 (row 1), veto -1 (row 3),
    shrink +.5 (row 5); the pass-through and the skipped hold are not views. Three hits
    → 1.0, no misses → no slugging; ranks (3, 1, 2) vs (2, 1, 3), Σd² = 2, IC = 0.5.

    research: one usable view (row 1; the failed lane is not one) → 1/1, no IC.
    """
    card = scorecard(ROWS)
    assert card["horizon_days"] == 5 and card["settled"] == 5
    seats = card["seats"]
    assert set(seats) == {"decide", "analyst:technical", "analyst:research", "critic"}

    decide = seats["decide"]
    assert (decide["views"], decide["directional"], decide["hits"]) == (5, 4, 3)
    assert decide["hit_rate"] == pytest.approx(0.75)
    assert decide["slugging"] == pytest.approx(13 / 6)
    assert decide["rank_ic"] == pytest.approx(0.9)

    technical = seats["analyst:technical"]
    assert technical["hit_rate"] == pytest.approx(0.5)
    assert technical["slugging"] == pytest.approx(0.875)
    assert technical["rank_ic"] == pytest.approx(0.3)

    critic = seats["critic"]
    assert (critic["views"], critic["hit_rate"], critic["slugging"]) == (3, 1.0, None)
    assert critic["rank_ic"] == pytest.approx(0.5)

    research = seats["analyst:research"]
    assert (research["views"], research["hit_rate"], research["rank_ic"]) == (1, 1.0, None)


def test_the_decide_seat_is_the_draft_not_the_vetoed_decision():
    row = settled(0.01, -0.02, draft("open", 0.5, "buy"), critic="veto")
    row["decision"] = decision_json("hold")  # the critic turned the Draft into a hold
    assert seat_views(row) == {"decide": 0.5, "critic": -1.0}
    row["raw_output"] = "garbled"  # unreadable: fall back to the journaled decision
    assert seat_views(row)["decide"] == 0.0


def test_rank_ic_handles_ties_and_refuses_too_little():
    assert spearman([1, 1, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486832980505138)
    assert spearman([1, 2], [1, 2]) is None
    assert spearman([1, 1, 1], [1, 2, 3]) is None


# --- every surface ---------------------------------------------------------------------


async def test_the_registry_serves_the_scorecard_and_lessons(db: Database):
    from trader.loop.bots import BotNotFound
    from trader.surfaces.services import build_registry

    decision_id = await journal(
        db,
        DECIDED,
        decision_json("open", "buy"),
        raw_output=json.dumps(draft("open", 0.8, "buy")),
    )
    settler = Settler(db, fixture_data(), symbols=REG, reflector=StubReflector())
    reg = build_registry(db, settler=settler)

    # The command is the manual pass; it runs whether or not the background job is on.
    out = await reg.get("settle_decisions").handler()
    assert (out["status"], out["settled"], out["lessons"]) == ("ok", 1, 1)
    audit = await db.fetch_one("SELECT action FROM audit_events ORDER BY id DESC LIMIT 1")
    assert audit["action"] == "settle_decisions"

    card = await reg.get("scorecard").handler()
    assert card["seats"]["decide"]["hits"] == 1
    assert (await reg.get("scorecard").handler(bot="bot-1", symbol="SPY"))["settled"] == 0
    with pytest.raises(BotNotFound):
        await reg.get("scorecard").handler(bot="nobody")

    (lesson,) = await reg.get("lessons").handler(symbol="GLD")
    assert lesson["decision_id"] == decision_id and lesson["lesson"].startswith("lesson on")
    assert isinstance(lesson["available_at"], str)


def test_scorecard_lessons_and_settlement_on_api_mcp_and_cli(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp

    settings = Settings(auth_disabled=True)
    app = create_app(settings)
    with TestClient(app) as tc:
        card = tc.get("/scorecard").json()
        assert (card["settled"], card["seats"], card["horizon_days"]) == (0, {}, 5)
        assert tc.get("/lessons", params={"symbol": "GLD"}).json() == []
        assert tc.get("/scorecard", params={"bot": "nobody"}).status_code == 404
        # No market-data keys in the suite: the pass reports itself unavailable.
        assert tc.post("/settlement/run").json()["status"] == "unavailable"

    tools = {cap.tool_name for cap in app.state.registry.all()}
    assert {"get_scorecard", "get_lessons", "settle_decisions"} <= tools
    assert build_mcp(app.state.registry) is not None

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(settings)))
    result = CliRunner().invoke(cli.app, ["scorecard", "--symbol", "GLD"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["symbol"] == "GLD"
    result = CliRunner().invoke(cli.app, ["lessons"])
    assert result.exit_code == 0 and json.loads(result.output) == []
    assert json.loads(CliRunner().invoke(cli.app, ["settle"]).output)["status"] == "unavailable"


# --- the reflect Role ---------------------------------------------------------------


def test_reflect_is_a_profile_routable_role_on_a_non_reasoning_grok():
    from trader.llm.caps import ROLES as CAP_ROLES
    from trader.llm.profiles import CHAT, ROLE_KINDS
    from trader.llm.registry import ROLES, build_llm_registry
    from trader.ops.config import Settings

    assert ROLE_KINDS["reflect"] == CHAT and "reflect" in ROLES and "reflect" in CAP_ROLES
    with_grok = build_llm_registry(Settings(auth_disabled=True, xai_api_key="k"))
    chain = with_grok.chain_for("reflect")
    assert [(c.name, c.model) for c in chain][0] == ("xai", "grok-4-1-fast-non-reasoning")
    assert chain[-1].name == "nemotron"  # the decide client backstops, like research
    without = build_llm_registry(Settings(auth_disabled=True))
    assert [c.name for c in without.chain_for("reflect")] == ["nemotron"]
    research = build_llm_registry(
        Settings(
            auth_disabled=True,
            research_base_url="http://lan:8000/v1",
            research_model="m",
        )
    )
    assert [c.model for c in research.chain_for("reflect")] == ["m"]


async def test_the_reflect_probe_runs_its_real_contract():
    from trader.llm.probe import probe

    class Client:
        async def complete(self, messages, *, schema=None, max_tokens=512, temperature=0.2):
            assert schema == REFLECT_SCHEMA
            return await StubLLM([json.dumps({"lesson": "Fine."})]).complete("reflect", messages)

    assert (await probe("reflect", Client()))["ok"] is True
