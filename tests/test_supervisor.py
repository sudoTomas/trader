"""The Supervisor and a second Bot in shadow (#126).

Two Bots run concurrently under one Supervisor with isolated cooldowns, journal history,
pauses and Sleeves; the Supervisor fetches each symbol's market data once per tick and
lets at most N Bots call the decide model at once; Bot #1 decides exactly as it did
alone; store-defined Bots are created, re-versioned and moved between Symbol modes on
every door, under the Owner rule; a Bot's prompt variant selects its decide rules.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest

from tests.conftest import ALL_TABLES, RESET_RISK_STATE
from tests.test_loop import BTC, HOLD, LIMITS, NOW, OPEN, StubLLM, StubMarketData
from trader.core.symbols import default_registry
from trader.loop.bots import BOT_ONE, BotDesk, BotNotFound, BotSpecError, bot_one_spec, bot_spec
from trader.loop.decider import (
    EQUITY_LARGECAP_PROMPT,
    SYSTEM_PROMPT,
    Decider,
    prompt_for,
    prompt_spec,
)
from trader.loop.journal import DENIED, SHADOW, Journal
from trader.loop.runner import BotRunner
from trader.loop.supervisor import SharedMarketData, Supervisor, runner_for
from trader.ops.config import Settings
from trader.risk.breakers import BreakerStore
from trader.risk.guard import RiskGuard
from trader.store.db import Database

ETH = default_registry().get("ETH/USD")


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, auth_disabled=True, **kwargs)


class CountingData(StubMarketData):
    """Counts every fetch that reaches the provider, per symbol."""

    def __init__(self) -> None:
        super().__init__()
        self.quotes: dict[str, int] = {}
        self.bars_by: dict[str, int] = {}

    async def get_bars(self, symbol, interval, lookback):
        self.bars_by[symbol.ticker] = self.bars_by.get(symbol.ticker, 0) + 1
        await asyncio.sleep(0)  # let a concurrent reader find the fetch in flight
        return await super().get_bars(symbol, interval, lookback)

    async def get_latest_quote(self, symbol):
        self.quotes[symbol.ticker] = self.quotes.get(symbol.ticker, 0) + 1
        await asyncio.sleep(0)
        return await super().get_latest_quote(symbol)


async def bot_one(db: Database, **kw) -> dict:
    return await BotDesk(db).seed(BOT_ONE, bot_one_spec(settings(loop_symbols="BTC/USD", **kw)))


async def bot_two(db: Database, name: str = "bot-2", **kw) -> dict:
    spec = bot_spec(
        symbols=kw.pop("symbols", {"BTC/USD": "shadow"}),
        capital_usd=kw.pop("capital_usd", 5_000.0),
        prompt_variant=kw.pop("prompt_variant", "equity-largecap"),
        **kw,
    )
    return await BotDesk(db).create(name, spec)


def runner(db: Database, shared, bot: dict, replies: list, **kw) -> BotRunner:
    return BotRunner(
        db=db,
        market_data=shared,
        llm=StubLLM(replies),
        guard=RiskGuard(db, LIMITS),
        symbols=kw.pop("symbols", [BTC]),
        shadow=True,
        equity=100_000.0,
        bot_id=bot["id"],
        bot_version=bot["version"],
        sources=bot["spec"]["sources"],
        prompt_variant=bot["spec"]["prompt"]["variant"],
        name=bot["name"],
        **kw,
    )


# --- two Bots, one Supervisor ---------------------------------------------------------


async def test_two_bots_tick_concurrently_with_isolated_cooldowns_and_history(db: Database):
    one, two = await bot_one(db), await bot_two(db)
    shared = SharedMarketData(CountingData())
    first = runner(db, shared, one, [HOLD, HOLD])
    second = runner(db, shared, two, [HOLD, HOLD], observes_account=False)
    supervisor = Supervisor([first, second], shared)

    # Bot #1 wakes alone first: its heartbeat cooldown is its own.
    await first.tick(NOW)
    results = await supervisor.tick(NOW + timedelta(seconds=5))
    assert [(r.symbol, r.status) for r in results] == [("BTC/USD", SHADOW)]
    row = await Journal(db).get(results[0].decision_id)
    assert row["bot_id"] == two["id"]
    # Bot #1's call on BTC is not Bot #2's history, though it is the same symbol.
    assert "Recent decisions" not in row["prompt"]

    later = await supervisor.tick(NOW + timedelta(hours=1))
    assert [r.symbol for r in later] == ["BTC/USD", "BTC/USD"]
    for result, bot in zip(later, (one, two), strict=True):
        row = await Journal(db).get(result.decision_id)
        assert row["bot_id"] == bot["id"]
        history = row["prompt"].split("Recent decisions (newest first):")[1]
        assert history.count("\n  - ") == 1  # its own earlier hold only


async def test_a_bot_pause_and_sleeve_are_its_own(db: Database):
    one, two = await bot_one(db), await bot_two(db)
    await BreakerStore(db, bot_id=one["id"]).pause("operator")
    shared = SharedMarketData(StubMarketData())
    first = runner(db, shared, one, [OPEN])
    second = runner(db, shared, two, [OPEN], observes_account=False)

    results = await Supervisor([first, second], shared).tick(NOW)

    by_bot = {(await Journal(db).get(r.decision_id))["bot_id"]: r for r in results}
    assert by_bot[one["id"]].status == DENIED and "paused" in by_bot[one["id"]].reason
    assert by_bot[two["id"]].status == SHADOW  # Bot #2's own row is not paused
    # Each prompt shows what that Bot sizes against: the account, or its Sleeve.
    one_row = await Journal(db).get(by_bot[one["id"]].decision_id)
    two_row = await Journal(db).get(by_bot[two["id"]].decision_id)
    assert "Account equity: 100,000.00 USD" in one_row["prompt"]
    assert "Sleeve equity: 5,000.00 USD" in two_row["prompt"]


async def test_a_second_bot_never_sees_another_bot_s_holding(db: Database):
    """A fixed Sleeve's book is its own ledger: Bot #1's position is not Bot #2's."""
    from trader.store.repos import BotPositionsRepo, PositionsRepo

    one, two = await bot_one(db), await bot_two(db)
    await PositionsRepo(db).upsert(symbol="BTC/USD", asset_class="crypto", qty=0.5)
    await BotPositionsRepo(db).upsert(bot_id=one["id"], symbol="BTC/USD", qty=0.5)
    shared = SharedMarketData(StubMarketData())
    first = runner(db, shared, one, [HOLD])
    second = runner(db, shared, two, [HOLD], observes_account=False)

    results = await Supervisor([first, second], shared).tick(NOW)

    one_prompt, two_prompt = [(await Journal(db).get(r.decision_id))["prompt"] for r in results]
    assert "Open position: 0.5 units" in one_prompt
    assert "Open position: 0 units" in two_prompt


async def test_market_data_is_fetched_once_per_symbol_per_tick(db: Database):
    one, two = await bot_one(db), await bot_two(db, symbols={"BTC/USD": "shadow"})
    data = CountingData()
    shared = SharedMarketData(data)
    first = runner(db, shared, one, [HOLD, HOLD], symbols=[BTC, ETH])
    second = runner(db, shared, two, [HOLD], observes_account=False)
    supervisor = Supervisor([first, second], shared)

    await supervisor.tick(NOW)
    # Two Bots, BTC read for marks and for each decide context: one fetch of each.
    assert data.quotes == {"BTC/USD": 1, "ETH/USD": 1}
    assert data.bars_by == {"BTC/USD": 1, "ETH/USD": 1}

    await supervisor.tick(NOW + timedelta(minutes=1))  # a new tick fetches afresh
    assert data.quotes == {"BTC/USD": 2, "ETH/USD": 2}


@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_the_semaphore_caps_concurrent_decide_calls(db: Database, limit: int):
    class SlowLLM(StubLLM):
        active = 0
        peak = 0

        async def complete(self, *args, **kwargs):
            SlowLLM.active += 1
            SlowLLM.peak = max(SlowLLM.peak, SlowLLM.active)
            try:
                await asyncio.sleep(0.05)
                return await super().complete(*args, **kwargs)
            finally:
                SlowLLM.active -= 1

    shared = SharedMarketData(StubMarketData())
    bots = [await bot_one(db)] + [await bot_two(db, f"bot-{n}") for n in (2, 3, 4)]
    runners = [runner(db, shared, bot, []) for bot in bots]
    for r in runners:
        r.llm = SlowLLM([HOLD])
        r._decider = Decider(r.llm)

    await Supervisor(runners, shared, max_concurrent_decides=limit).tick(NOW)

    assert SlowLLM.peak == limit  # four Bots decide at once only when the limit allows it


async def test_bot_one_decides_exactly_as_it_did_alone(db: Database):
    """Golden parity: under the Supervisor Bot #1's prompts, decisions and verdicts are
    byte-identical to the runner on its own."""
    one = await bot_one(db)

    async def wakeup(supervised: bool) -> tuple[list, dict]:
        data = StubMarketData()
        if supervised:
            first = runner(db, SharedMarketData(data), one, [OPEN])
            results = await Supervisor([first], first.market_data).tick(NOW)
        else:
            first = runner(db, data, one, [OPEN])
            results = await first.tick(NOW)
        row = await db.fetch_one(
            "SELECT prompt, raw_output, decision, risk_verdicts, status FROM decisions"
            " WHERE id = %s",
            (results[0].decision_id,),
        )
        return first.llm.calls, row

    alone_calls, alone = await wakeup(supervised=False)
    await db.execute("TRUNCATE decisions CASCADE")
    supervised_calls, supervised = await wakeup(supervised=True)

    assert supervised_calls == alone_calls
    assert supervised == alone
    assert alone_calls[0][0]["content"] == SYSTEM_PROMPT


async def test_one_bot_s_backoff_holds_back_that_bot_only(db: Database):
    """The running loop: a Bot whose model is down backs off; the other keeps ticking."""
    from trader.llm.base import LLMError

    one, two = await bot_one(db), await bot_two(db)
    shared = SharedMarketData(StubMarketData())
    failing = runner(db, shared, one, [LLMError("endpoint down"), HOLD])
    healthy = runner(db, shared, two, [HOLD], observes_account=False)
    supervisor = Supervisor([failing, healthy], shared, tick_seconds=0.02)

    await supervisor.start()
    await asyncio.sleep(0.3)  # many ticks
    await supervisor.stop()

    assert len(failing.llm.calls) == 1  # transient: 30 s backoff, not retried every tick
    assert failing.stats.last_error and "endpoint down" in failing.stats.last_error
    assert len(healthy.llm.calls) == 1 and healthy.stats.wakeups == 1
    assert healthy.stats.ticks > 1  # it kept ticking (its heartbeat is simply not due)


def test_a_supervised_bot_must_read_the_shared_market_data():
    class Bare:
        name = "x"
        market_data = StubMarketData()

    with pytest.raises(ValueError, match="shared market data"):
        Supervisor([Bare()], SharedMarketData(StubMarketData()))  # type: ignore[list-item]
    with pytest.raises(ValueError, match="at least 1"):
        Supervisor([], SharedMarketData(StubMarketData()), max_concurrent_decides=0)


# --- store-defined Bots ---------------------------------------------------------------


async def test_the_supervisor_loads_store_bots_and_rebuilds_on_a_new_version(db: Database):
    one = await bot_one(db)
    shared = SharedMarketData(StubMarketData())
    first = runner(db, shared, one, [HOLD])
    universe = default_registry()
    supervisor = Supervisor(
        [first],
        shared,
        factory=lambda bot: runner_for(bot, template=first, universe=universe),
    )
    await supervisor.sync()
    assert [b.name for b in supervisor.bots] == [BOT_ONE]

    await bot_two(db, symbols={"BTC/USD": "shadow", "ETH/USD": "replay"})
    await supervisor.sync()
    loaded = supervisor.bot("bot-2")
    assert [s.ticker for s in loaded.symbols] == ["BTC/USD"]  # replay is not run
    assert loaded.prompt_variant == "equity-largecap" and loaded.observes_account is False
    assert loaded.executor is None and loaded.shadow_symbols == {"BTC/USD"}
    assert loaded.decide_slots is supervisor.decide_slots

    await supervisor.sync()
    assert supervisor.bot("bot-2") is loaded  # same version: same runner, same cooldowns

    await BotDesk(db).set_symbol_mode("bot-2", "ETH/USD", "shadow")
    await supervisor.sync()
    assert supervisor.bot("bot-2") is not loaded
    assert supervisor.bot("bot-2").bot_version == 2

    await BotDesk(db).new_version("bot-2", symbols={"BTC/USD": "replay"})
    await supervisor.sync()
    with pytest.raises(BotNotFound):
        supervisor.bot("bot-2")  # nothing left to run


async def test_only_a_bot_live_on_a_symbol_gets_the_executor(db: Database):
    from tests.test_executor import FakeAlpaca
    from trader.loop.executor import Executor

    one = await bot_one(db)
    shared = SharedMarketData(StubMarketData())
    template = runner(db, shared, one, [])
    template.shadow = False
    template.executor = Executor(db, FakeAlpaca())
    universe = default_registry()

    def built(symbols: dict) -> BotRunner | None:
        spec = bot_spec(symbols=symbols, capital_usd=5_000.0)
        return runner_for(
            {"id": 9, "name": "b", "version": 1, "spec": spec}, template=template, universe=universe
        )

    assert built({"ETH/USD": "shadow"}).executor is None
    assert built({"ETH/USD": "live"}).executor is template.executor
    assert built({"ETH/USD": "replay"}) is None
    template.shadow = True  # a shadow build reaches no venue, live symbols or not
    assert built({"ETH/USD": "live"}).executor is None


async def test_the_owner_rule_refuses_a_second_live_bot_on_a_symbol(db: Database):
    await bot_one(db, shadow_mode=False)  # Bot #1 is live on BTC/USD
    desk = BotDesk(db)

    with pytest.raises(BotSpecError, match="BTC/USD already live on bot-1"):
        await bot_two(db, symbols={"BTC/USD": "live"})
    assert await db.fetch_one("SELECT id FROM bots WHERE name = 'bot-2'") is None

    await bot_two(db, symbols={"BTC/USD": "shadow", "ETH/USD": "live"})  # shadowing is fine
    with pytest.raises(BotSpecError, match="already live on bot-1"):
        await desk.set_symbol_mode("bot-2", "BTC/USD", "live")
    # A Settings change that makes Bot #1 live on Bot #2's symbol refuses the boot.
    with pytest.raises(BotSpecError, match="ETH/USD already live on bot-2"):
        await desk.seed(
            BOT_ONE, bot_one_spec(settings(loop_symbols="BTC/USD,ETH/USD", shadow_mode=False))
        )

    # Once Bot #1 only shadows BTC, Bot #2 may own it.
    await bot_one(db, shadow_mode=False, shadow_symbols="BTC/USD")
    moved = await desk.set_symbol_mode("bot-2", "BTC/USD", "live")
    assert moved["spec"]["symbols"] == {"BTC/USD": "live", "ETH/USD": "live"}


async def test_versions_are_append_only_audited_and_bot_one_follows_settings(db: Database):
    await bot_one(db)
    desk = BotDesk(db)
    created = await bot_two(db, sources=["sentiment", "brief"])
    assert created["version"] == 1
    assert created["spec"]["sleeve"] == {"capital_usd": 5_000.0}
    assert [s["source"] for s in created["spec"]["sources"]] == ["sentiment", "brief"]

    changed = await desk.new_version(
        "bot-2", capital_usd=6_000.0, triggers={"price_move_pct": 0.03}
    )
    assert changed["version"] == 2
    assert changed["spec"]["triggers"]["price_move_pct"] == 0.03
    assert changed["spec"]["sources"] == created["spec"]["sources"]  # untouched fields stay
    unchanged = await desk.new_version("bot-2", capital_usd=6_000.0)
    assert unchanged["version"] == 2  # nothing changed, nothing written

    actions = [
        r["action"]
        for r in await db.fetch_all(
            "SELECT action FROM audit_events WHERE category = 'bots' AND subject = 'bot-2'"
            " ORDER BY id"
        )
    ]
    assert actions == ["create_bot", "new_bot_version"]
    versions = (await desk.get("bot-2"))["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["spec"] == created["spec"]  # the old version is kept as it was

    with pytest.raises(BotSpecError, match="follows Settings"):
        await desk.set_symbol_mode(BOT_ONE, "BTC/USD", "live")
    with pytest.raises(BotSpecError, match="already exists"):
        await bot_two(db)
    with pytest.raises(BotNotFound):
        await desk.set_symbol_mode("nope", "BTC/USD", "shadow")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"symbols": {}}, "at least one symbol"),
        ({"symbols": {"NOPE": "shadow"}}, "unknown symbol"),
        ({"symbols": {"BTC/USD": "paper"}}, "Symbol mode"),
        ({"sources": ["tea_leaves"]}, "unknown Sources"),
        ({"prompt_variant": "yolo"}, "unknown prompt variant"),
        ({"triggers": {"nope": 1}}, "unknown trigger settings"),
        ({"capital_usd": 0}, "positive dollar amount"),
        ({"capital_usd": float("nan")}, "positive dollar amount"),
        ({"account_id": "alpaca-live"}, "unknown account"),
    ],
)
def test_a_bad_spec_is_refused(change, message):
    fields = {"symbols": {"BTC/USD": "shadow"}, "capital_usd": 5_000.0, **change}
    with pytest.raises(BotSpecError, match=message):
        bot_spec(**fields)


# --- prompt variants ------------------------------------------------------------------


def test_the_default_variant_is_bot_one_s_prompt_byte_for_byte():
    assert prompt_for("default") is SYSTEM_PROMPT
    assert bot_one_spec(settings())["prompt"] == {
        "variant": "default",
        "sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
    }
    assert prompt_spec("equity-largecap")["sha256"] == (
        hashlib.sha256(EQUITY_LARGECAP_PROMPT.encode()).hexdigest()
    )
    with pytest.raises(KeyError):
        prompt_for("nope")


def test_the_equity_largecap_prompt_asks_for_trend_confirmation_and_research():
    text = EQUITY_LARGECAP_PROMPT
    for must in ("sma_20", "ema_12", "falling tape", "sentiment", "Brief", "`hold` is the default"):
        assert must in text
    # The same output contract as the default rules.
    assert "`side` and a positive `notional_usd` are REQUIRED" in text


async def test_a_bot_s_prompt_variant_selects_its_decide_rules(db: Database):
    from trader.loop.confirm import Confirmer

    two = await bot_two(db)
    shared = SharedMarketData(StubMarketData())
    second = runner(db, shared, two, [OPEN, "{}"])
    seen: list = []

    class Recording(Confirmer):
        async def confirm(self, ctx, decision):
            from trader.loop.decider import system_prompt

            seen.append(system_prompt(ctx))
            return None

    second.confirm = Recording(second.llm)
    await Supervisor([second], shared).tick(NOW)

    assert second.llm.calls[0][0] == {"role": "system", "content": EQUITY_LARGECAP_PROMPT}
    assert seen == [EQUITY_LARGECAP_PROMPT]  # the Confirm judges by the same rules


# --- the surfaces ---------------------------------------------------------------------


async def test_loop_status_and_wake_take_a_bot(db: Database):
    from trader.surfaces.services import build_registry

    one, two = await bot_one(db), await bot_two(db)
    shared = SharedMarketData(StubMarketData())
    supervisor = Supervisor(
        [runner(db, shared, one, [HOLD]), runner(db, shared, two, [HOLD])], shared
    )
    registry = build_registry(db, supervisor)

    default = await registry.get("loop_status").handler()
    assert "bot" not in default and default["stats"]["wakeups"] == 0
    status = await registry.get("loop_status").handler(bot="bot-2")
    assert status["bot"] == "bot-2" and status["shadow_symbols"] == []

    woken = await registry.get("wake_trader").handler(bot="bot-2")
    assert woken["bot"] == "bot-2" and len(woken["results"]) == 1
    row = await Journal(db).get(woken["results"][0]["decision_id"])
    assert row["bot_id"] == two["id"]
    assert (await registry.get("loop_status").handler(bot="bot-2"))["stats"]["wakeups"] == 1
    assert (await registry.get("loop_status").handler())["stats"]["wakeups"] == 0
    with pytest.raises(BotNotFound):
        await registry.get("loop_status").handler(bot="nope")


def _reset_store() -> None:
    async def run() -> None:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await db.execute(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            await db.execute(RESET_RISK_STATE)
        finally:
            await db.close()

    asyncio.run(run())


CREATE = {
    "name": "bot-2-largecap",
    "symbols": {"BTC/USD": "shadow", "ETH/USD": "shadow"},
    "capital_usd": 5_000.0,
    "sources": ["sentiment", "brief", "held_position", "macro_view"],
    "prompt_variant": "equity-largecap",
}


def test_bot_commands_answer_the_same_on_every_door(clean_db: None, monkeypatch):
    """create → new version → set Symbol mode, and an Owner-rule refusal, on the API, MCP
    and CLI: the same results from the same starting store."""
    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    boot = settings(loop_symbols="BTC/USD", shadow_mode=False)  # Bot #1 owns BTC/USD
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot)))

    def via_api() -> list:
        with TestClient(create_app(boot)) as tc:
            out = [
                tc.post("/bots", json=CREATE).json(),
                tc.post("/bots/bot-2-largecap/versions", json={"capital_usd": 6_000.0}).json(),
                tc.post(
                    "/bots/bot-2-largecap/symbol-mode", json={"symbol": "ETH/USD", "mode": "live"}
                ).json(),
            ]
            refused = tc.post(
                "/bots/bot-2-largecap/symbol-mode", json={"symbol": "BTC/USD", "mode": "live"}
            )
            assert refused.status_code == 422 and "already live on bot-1" in refused.text
        return out

    async def via_mcp() -> list:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await BotDesk(db).seed(BOT_ONE, bot_one_spec(boot))  # what the API's boot does
            async with Client(build_mcp(build_registry(db))) as mcp_client:
                calls = [
                    ("create_bot", CREATE),
                    ("new_bot_version", {"name": "bot-2-largecap", "capital_usd": 6_000.0}),
                    (
                        "set_symbol_mode",
                        {"name": "bot-2-largecap", "symbol": "ETH/USD", "mode": "live"},
                    ),
                ]
                out = []
                for tool, args in calls:
                    result = await mcp_client.call_tool(tool, args)
                    assert not result.is_error, result.content
                    out.append(result.structured_content)
                refused = await mcp_client.call_tool(
                    "set_symbol_mode",
                    {"name": "bot-2-largecap", "symbol": "BTC/USD", "mode": "live"},
                )
                assert refused.is_error and "already live on bot-1" in str(refused.content)
            return out
        finally:
            await db.close()

    def via_cli() -> list:
        runs = [
            [
                "bot", "create", "bot-2-largecap",
                "--symbols", "BTC/USD,ETH/USD", "--capital", "5000",
                "--source", "sentiment", "--source", "brief",
                "--source", "held_position", "--source", "macro_view",
                "--prompt", "equity-largecap",
            ],
            ["bot", "version", "bot-2-largecap", "--capital", "6000"],
            ["bot", "mode", "bot-2-largecap", "ETH/USD", "live"],
        ]  # fmt: skip
        out = []
        for args in runs:
            printed = CliRunner().invoke(cli.app, args)
            assert printed.exit_code == 0, printed.output
            out.append(json.loads(printed.output))
        refused = CliRunner().invoke(cli.app, ["bot", "mode", "bot-2-largecap", "BTC/USD", "live"])
        assert refused.exit_code == 1
        return out

    api = via_api()
    _reset_store()
    mcp = asyncio.run(via_mcp())
    _reset_store()
    printed = via_cli()

    assert api == mcp == printed
    assert [r["version"] for r in api] == [1, 2, 3]
    assert api[2]["spec"]["symbols"] == {"BTC/USD": "shadow", "ETH/USD": "live"}
    assert api[2]["spec"]["sleeve"] == {"capital_usd": 6_000.0}


async def test_the_app_builds_a_supervisor_that_loads_store_bots(db: Database):
    """The real wiring: Bot #1 from Settings leads; a store Bot shares its pieces."""
    from trader.surfaces.api.app import build_runner as build_app_runner
    from trader.surfaces.api.app import build_supervisor

    keys = {"alpaca_api_key_id": "id", "alpaca_api_secret": "secret"}  # shadow: never dialed
    boot = settings(loop_symbols="BTC/USD", loop_max_concurrent_decides=3, **keys)
    first = build_app_runner(db, boot)
    supervisor = build_supervisor(first, boot)
    assert supervisor.decide_slots._value == 3  # TRADER_LOOP_MAX_CONCURRENT_DECIDES
    assert first.decide_slots is supervisor.decide_slots
    assert supervisor.market_data is first.market_data.provider  # a redeem quotes fresh

    await bot_one(db)
    await bot_two(db, symbols={"ETH/USD": "shadow"})
    await supervisor.sync()
    second = supervisor.bot("bot-2")
    assert second.market_data is first.market_data and second.llm is first.llm
    assert second.guard is first.guard and second.executor is None
    assert [s.ticker for s in second.symbols] == ["ETH/USD"]
