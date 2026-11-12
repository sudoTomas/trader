"""Blinded replay (#140): named vs blind, the gap is how much of replay P&L is memory.

A blind run renders every prompt (decide, desk analysts, critic, the Confirm, the
exit-review block) without the ticker, the company name, calendar dates or dollar values,
and maps the model's %-of-Sleeve sizes back before the crude book (RiskGuard's seat)
sees the intent. The LLM is stubbed; the store is real.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

import pytest

from tests.source_fixtures import StubFred
from tests.test_loop import NOW, StubMarketData
from tests.test_replay_eval import RecordingEvaluator
from trader.core.symbols import AssetClass, Symbol, default_registry, make_symbol
from trader.desk.desk import Desk
from trader.eval.blind import Blinder
from trader.eval.replay import ReplayHarness
from trader.llm.base import Completion, Usage
from trader.loop.confirm import Confirmer
from trader.loop.decider import PROMPT_VARIANTS
from trader.store.db import Database
from trader.store.repos import BarsRepo, BriefsRepo, MacroViewsRepo, SentimentRepo

BTC = default_registry().get("BTC/USD")
AAPL = make_symbol("AAPL", AssetClass.US_EQUITY)
# Wednesday 11:00 ET: three regular-session hours for the equity, three for crypto.
START, END = NOW, NOW + timedelta(hours=3)

HOLD = (
    '{"action":"hold","conviction":0.3,"rationale":"nothing doing","side":null,"notional_usd":null}'
)


def _open(notional: float) -> str:
    return json.dumps(
        {
            "action": "open",
            "conviction": 0.8,
            "rationale": "trend",
            "side": "buy",
            "notional_usd": notional,
        }
    )


CLOSE = (
    '{"action":"close","conviction":0.7,"rationale":"thesis gone","side":"sell","notional_usd":100}'
)
ANALYST = '{"stance":"bullish","confidence":0.6,"note":"rsi rising"}'
CRITIC = '{"verdict":"approve","notional_usd":null,"reason":"supported"}'
CONFIRM = json.dumps(
    {
        "action": {"probabilities": {"hold": 0.2, "open": 0.5, "close": 0.3}},
        "open_side": {"probabilities": {"buy": 0.7, "sell": 0.3}},
        "close_side": {"probabilities": {"buy": 0.3, "sell": 0.7}},
    }
)

# Per symbol: what the fixture names it by, and a base price far from the ~100 index.
FIXTURES: dict[str, dict[str, Any]] = {
    "BTC/USD": {"symbol": BTC, "name": "Bitcoin", "terms": ["BTC", "BTCUSD"], "base": 60_000.0},
    "AAPL": {"symbol": AAPL, "name": "Apple", "terms": ["AAPL"], "base": 230.17},
}


class RoleLLM:
    """LLMRegistry-shaped: scripted replies per Role, every call's messages recorded."""

    def __init__(self, decide: list[str]) -> None:
        self._decide = list(decide)
        self.calls: list[tuple[str, list[dict]]] = []

    async def complete(
        self, role, messages, *, schema=None, max_tokens=1024, temperature=0.2, state=None
    ):
        if state is not None:  # Jev's structured state, recorded as the text it sends
            messages = [{"role": "user", "content": json.dumps(state)}]
        self.calls.append((role, messages))
        text = {
            "decide": lambda: self._decide.pop(0),
            "analyst": lambda: ANALYST,
            "critic": lambda: CRITIC,
            "confirm": lambda: CONFIRM,
        }[role]()
        return Completion(
            text=text,
            usage=Usage(prompt_tokens=100, completion_tokens=20),
            cost_usd=0.0,
            provider="stub",
            model="stub-model",
        )


class CapturingDesk(Desk):
    """The real desk, keeping each reviewed (context, Draft) so the test can render the
    Confirm on exactly what the critic saw (replay itself runs no Confirm)."""

    def __init__(self, llm: Any) -> None:
        super().__init__(llm)
        self.reviewed: list[tuple[Any, Any]] = []

    async def review(self, ctx, decision, desk_info):
        self.reviewed.append((ctx, decision))
        return await super().review(ctx, decision, desk_info)


async def _seed(db: Database, symbol: Symbol, base: float, name: str) -> None:
    """Hourly bars around the window, and a leaky row in every free-text Source."""
    await BarsRepo(db).upsert_many(
        [
            {
                "symbol": symbol.ticker,
                "interval": "1h",
                "ts": END - timedelta(hours=150 - i),
                "open": base * (1 + i / 1000),
                "high": base * (1 + i / 1000) * 1.001,
                "low": base * (1 + i / 1000) * 0.999,
                "close": base * (1 + i / 1000),
                "volume": 1_000.0,
            }
            for i in range(150)
        ]
    )
    before = START - timedelta(hours=2)
    view = await MacroViewsRepo(db).add(
        compiled_by="stub:stub-model",
        global_thesis=f"{name} ({symbol.ticker}) leads; Fed cut on 2026-07-30 at 14:00.",
        sections={symbol.asset_class: f"{name} trades near $1,234 after earnings."},
        note_ids=[1],
    )
    brief = await BriefsRepo(db).add(
        symbol=symbol.ticker,
        compiled_by="stub:stub-model",
        thesis=f"{name} breakout above $250 since 2026-08-01.",
        points=[f"{symbol.ticker} volume rising"],
        note_ids=[1],
    )
    score = await SentimentRepo(db).insert(
        symbol=symbol.ticker,
        status="ok",
        score=0.4,
        volume=f"{name} chatter",
        drivers=[f"{name} launch"],
    )
    await db.execute("UPDATE macro_views SET created_at = %s WHERE version = %s", (before, view))
    await db.execute("UPDATE briefs SET ts = %s WHERE version = %s", (before, brief))
    await db.execute("UPDATE sentiment_scores SET ts = %s WHERE id = %s", (before, score))


ALL_SOURCES = [
    {"source": "fred"},
    {"source": "sentiment"},
    {"source": "brief"},
    {"source": "macro_view"},
    {"source": "held_position"},
]


async def _replay(db: Database, ticker: str, *, blind: bool) -> tuple[dict, RoleLLM, list[str]]:
    """One desk-on replay of the fixture with every Source on and a seeded held position;
    returns the run, the LLM and every prompt text rendered (the Confirm's included)."""
    fx = FIXTURES[ticker]
    symbol = fx["symbol"]
    provider = StubMarketData()
    provider.fred = StubFred()  # type: ignore[attr-defined]
    # Step 1 holds, step 2 closes the seed (critic + Confirm), step 3 opens (likewise).
    llm = RoleLLM([HOLD, CLOSE, _open(10)])
    desk = CapturingDesk(llm)
    harness = ReplayHarness(db, llm, provider, desk=desk)
    run = await harness.run(
        symbol,
        START,
        END,
        label=f"{'blind' if blind else 'named'}-{ticker}",
        config={
            "sources": ALL_SOURCES,
            "desk": True,
            "blind": blind,
            "sleeve_usd": 5000,
            "prompt_variant": "equity-largecap" if symbol.asset_class != "crypto" else "default",
            "exit_review_seed": {
                "qty": 1000 / fx["base"],
                "avg_entry_price": fx["base"] * 0.98,
                "opened_at": (START - timedelta(hours=5)).isoformat(),
            },
        },
    )
    assert run["status"] == "done", run["summary"]
    for ctx, decision in desk.reviewed:
        if decision.intent is not None:  # as live: a hold Draft is never confirmed
            await Confirmer(llm).confirm(ctx, decision)
    texts = [m["content"] for _role, messages in llm.calls for m in messages]
    return run, llm, texts


ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b")
DOLLARS = re.compile(r"\$|\d[\d,.]*\s*USD\b")


def _word(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])")


def _leaks(text: str, ticker: str) -> list[str]:
    fx = FIXTURES[ticker]
    found = [t for t in [ticker, *fx["terms"]] if _word(t).search(text)]
    if fx["name"].lower() in text.lower():
        found.append(fx["name"])
    for pattern in (ISO_DATE, CLOCK, DOLLARS):
        found += pattern.findall(text)
    return found


@pytest.mark.parametrize("ticker", ["BTC/USD", "AAPL"])
async def test_every_blind_prompt_withholds_identity_dates_and_dollars(db: Database, ticker):
    fx = FIXTURES[ticker]
    await _seed(db, fx["symbol"], fx["base"], fx["name"])
    named_run, named_llm, named = await _replay(db, ticker, blind=False)
    # The fixture is leaky on purpose: named prompts carry every marker the scan looks for.
    named_leaks = {leak for text in named for leak in _leaks(text, ticker)}
    assert ticker in named_leaks and FIXTURES[ticker]["name"] in named_leaks
    assert any(ISO_DATE.fullmatch(x) for x in named_leaks)
    assert any(DOLLARS.fullmatch(x) or x == "$" for x in named_leaks)

    run, llm, blind = await _replay(db, ticker, blind=True)
    roles = {role for role, _ in llm.calls}
    # Every prompt was rendered; the Confirm never sees a blind step (its state is unblinded).
    assert roles == {"decide", "analyst", "critic"}
    assert "confirm" in {role for role, _ in named_llm.calls}
    user_prompts = [m[1]["content"] for _role, m in llm.calls]
    assert any("Held position review" in p for p in user_prompts)  # the exit-review block
    assert any("X sentiment" in p for p in user_prompts)
    assert any("FRED macro series" in p for p in user_prompts)
    assert any("Recent decisions" in p for p in user_prompts)
    for text in blind:
        assert _leaks(text, ticker) == [], text
    # No raw price either: the last close, as the named prompt prints it.
    last_close = FIXTURES[ticker]["base"] * (1 + 146 / 1000)
    assert not any(f"{last_close:,.6g}" in text for text in blind)
    assert any("ASSET_A" in p for p in user_prompts)

    assert run["summary"]["blind"] == {
        "alias": "ASSET_A",
        "sources_dropped": ["brief", "macro_view"],
        "sources_kept": ["fred", "held_position", "sentiment"],
    }
    assert "blind" not in named_run["summary"]

    def count(calls, role):
        return sum(1 for r, _ in calls if r == role)

    for role in ("decide", "critic"):  # same steps, same reviews
        assert count(named_llm.calls, role) == count(llm.calls, role)
    # The research analyst reads only the research lanes, which a blind run drops.
    assert count(llm.calls, "analyst") == count(llm.calls, "decide")
    assert count(named_llm.calls, "analyst") == 2 * count(named_llm.calls, "decide")


async def test_blind_sizes_map_back_to_the_same_intent_as_a_named_decision(db: Database):
    """10% of a 5,000 Sleeve, blind, is the same order intent RiskGuard would see from a
    named 500 USD open: same symbol, side and notional — and the same crude book."""
    fx = FIXTURES["BTC/USD"]
    await _seed(db, BTC, fx["base"], fx["name"])
    runs = {}
    for blind, reply in ((False, _open(500)), (True, _open(10))):
        harness = ReplayHarness(db, RoleLLM([reply]), StubMarketData())
        runs[blind] = await harness.run(
            BTC,
            START,
            START + timedelta(hours=1),
            label=f"map-{blind}",
            config={"blind": blind, "sleeve_usd": 5000, "sources": ALL_SOURCES},
        )
    steps = {
        blind: await db.fetch_one("SELECT decision FROM eval_steps WHERE run_id = %s", (r["id"],))
        for blind, r in runs.items()
    }
    assert steps[True]["decision"]["intent"] == steps[False]["decision"]["intent"]
    assert steps[False]["decision"]["intent"] == {
        "symbol": "BTC/USD",
        "side": "buy",
        "notional": 500.0,
    }
    for key in ("unrealized_pnl", "gross_pnl", "net_pnl", "trading_costs"):
        assert runs[True]["summary"][key] == runs[False]["summary"][key]


async def test_unblind_turns_a_percent_of_the_sleeve_into_dollars(db: Database):
    from trader.core.types import Action, Decision, OrderIntent, Side

    pct = Decision(
        symbol="AAPL",
        action=Action.OPEN,
        conviction=0.7,
        rationale="r",
        intent=OrderIntent(symbol="AAPL", side=Side.BUY, notional=12.5),
    )
    named = Decision(
        symbol="AAPL",
        action=Action.OPEN,
        conviction=0.7,
        rationale="r",
        intent=OrderIntent(symbol="AAPL", side=Side.BUY, notional=625.0),
    )
    blinder = Blinder(AAPL, step=timedelta(hours=1))
    assert blinder.unblind(pct, equity=5000.0) == named
    hold = Decision(symbol="AAPL", action=Action.HOLD, conviction=0.2, rationale="r")
    assert blinder.unblind(hold, equity=5000.0) == hold


async def test_blind_refuses_a_prompt_variant_that_names_the_symbol(db: Database, monkeypatch):
    monkeypatch.setitem(PROMPT_VARIANTS, "aapl-only", "Trade AAPL only. Reply with JSON.")
    harness = ReplayHarness(db, RoleLLM([]), StubMarketData())
    with pytest.raises(ValueError, match="names the symbol"):
        await harness.create_run(
            AAPL,
            START,
            END,
            label="leaky",
            config={"blind": True, "prompt_variant": "aapl-only"},
        )
    # Named, the same variant is fine: only blind runs withhold identity.
    await harness.create_run(
        AAPL, START, END, label="named", config={"prompt_variant": "aapl-only"}
    )


async def test_the_live_prompt_is_unchanged_without_blind(db: Database):
    """No blind renderer outside a blind replay: the named prompt keeps its header."""
    await _seed(db, BTC, 60_000.0, "Bitcoin")
    llm = RoleLLM([HOLD])
    await ReplayHarness(db, llm, StubMarketData()).run(
        BTC, START, START + timedelta(hours=1), label="named"
    )
    prompt = llm.calls[0][1][1]["content"]
    assert prompt.startswith("Symbol: BTC/USD (crypto)\nTime: 2026-08-12T15:00:00+00:00")


# --- the named-vs-blind script -------------------------------------------------------


def _script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "replay_blind_compare.py"
    spec = importlib.util.spec_from_file_location("replay_blind_compare", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_compare_script_rebuilds_the_books_net_pnl(db: Database):
    """The script's hit rate counts the crude book's round trips; rebuilt from the steps
    they must add up to the run's own net P&L (fills paid, the last one marked)."""
    from trader.eval.costs import ReplayCosts

    script = _script()
    await _seed(db, BTC, 60_000.0, "Bitcoin")
    llm = RoleLLM([_open(10), HOLD, CLOSE.replace("100", "50"), _open(20)])
    costs = ReplayCosts(half_spread_bps_crypto=10.0, fee_bps=2.0, crypto_taker_fee_bps=25.0)
    run = await ReplayHarness(db, llm, StubMarketData(), costs=costs).run(
        BTC,
        START,
        START + timedelta(hours=4),
        label="script",
        config={"blind": True, "sleeve_usd": 5000},
    )
    steps = await db.fetch_all(
        "SELECT ts, status, price, decision FROM eval_steps WHERE run_id = %s ORDER BY ts",
        (run["id"],),
    )
    trades = script._trades(steps)
    assert len(trades) == 2  # one closed, one still open at the end
    assert sum(trades) == pytest.approx(run["summary"]["net_pnl"], abs=1e-6)


def test_the_compare_script_configures_each_arm_from_the_bot_spec():
    script = _script()
    spec = {
        "sources": [{"source": "sentiment"}, {"source": "brief"}, {"source": "macro_view"}],
        "prompt": {"variant": "equity-largecap"},
        "lanes": {"desk": False},
        "sleeve": {"capital_usd": 5000},
    }
    named = script._config(spec, "named")
    assert named == {
        "sources": spec["sources"],
        "prompt_variant": "equity-largecap",
        "desk": False,
        "sleeve_usd": 5000.0,
    }
    assert script._config(spec, "blind") == {**named, "blind": True}
    assert script._config(spec, script.CONTROL)["sources"] == [
        {"source": "sentiment", "params": {}}
    ]


# --- surfaces ------------------------------------------------------------------------


async def test_registry_forwards_blind_into_the_run_config(db: Database):
    from trader.surfaces.services import build_registry

    evaluator = RecordingEvaluator()
    reg = build_registry(db, evaluator=evaluator)
    window = {"start": START.isoformat(), "end": END.isoformat()}
    await reg.get("run_replay_eval").handler(symbol="BTC/USD", blind=True, **window)
    assert evaluator.configs == [{"desk": False, "briefs": True, "macro_view": True, "blind": True}]
    audit = await db.fetch_all("SELECT payload FROM audit_events ORDER BY id")
    assert audit[-1]["payload"]["blind"] is True


def test_blind_on_api_and_cli(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    import trader.surfaces.api.app as app_mod
    import trader.surfaces.cli.main as cli
    from trader.ops.config import Settings

    evaluator = RecordingEvaluator()
    monkeypatch.setattr(app_mod, "build_evaluator", lambda db, settings, desk=None, **kw: evaluator)
    settings = Settings(auth_disabled=True)
    window = {"start": START.isoformat(), "end": END.isoformat()}
    with TestClient(app_mod.create_app(settings)) as tc:
        resp = tc.post("/eval/replay", params={"symbol": "BTC/USD", "blind": True, **window})
        assert resp.status_code == 200
    monkeypatch.setattr(cli, "_client", lambda: TestClient(app_mod.create_app(settings)))
    args = ["eval", "run", "--symbol", "BTC/USD", "--start", window["start"]]
    result = CliRunner().invoke(cli.app, [*args, "--end", window["end"], "--blind"])
    assert result.exit_code == 0, result.output
    blind = {"desk": False, "briefs": True, "macro_view": True, "blind": True}
    assert evaluator.configs == [blind, blind]


async def test_the_mcp_tool_takes_blind(db: Database):
    from mcp.client import Client

    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    evaluator = RecordingEvaluator()
    async with Client(build_mcp(build_registry(db, evaluator=evaluator))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "blind" in tools["run_replay_eval"].input_schema["properties"]
        window = {"start": START.isoformat(), "end": END.isoformat()}
        await client.call_tool("run_replay_eval", {"symbol": "BTC/USD", "blind": True, **window})
    assert evaluator.configs[-1]["blind"] is True
