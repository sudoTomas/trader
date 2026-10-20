"""Per-Bot sentiment polling for symbols not held (#127).

The poller scores held symbols only (docs/lessons.md#sentiment-held-symbols-only). A Bot
whose sentiment subscription sets ``poll_unheld`` also has its other symbols (shadow and
live) scored, at the same cadence; the default is off and changes nothing. The setting is
flipped by its own command, a new audited Bot version, on every door; Bot #1 follows
Settings and is refused.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_loop import BTC, HOLD, StubMarketData
from tests.test_supervisor import (
    _reset_store,
    bot_one,
    bot_two,
    runner,
    settings,
)
from trader.core.symbols import default_registry
from trader.loop.bots import BOT_ONE, BotDesk, BotSpecError, bot_one_spec, bot_spec
from trader.loop.sources import POLL_UNHELD, polls_unheld
from trader.loop.supervisor import SharedMarketData, Supervisor, runner_for
from trader.store.db import Database
from trader.store.repos import PositionsRepo, SentimentRepo

ETH = default_registry().get("ETH/USD")


class JournalingSentiment:
    """Records each poll and journals an ok row, as the real service does."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.polled: list[str] = []

    async def get_x_sentiment(self, symbol, **kwargs):
        self.polled.append(symbol.ticker)
        await SentimentRepo(self.db).insert(symbol=symbol.ticker, status="ok", score=0.1, model="t")
        return {"status": "ok", "symbol": symbol.ticker}


def sentiment_sub(poll_unheld: bool | None = None) -> dict:
    params = {} if poll_unheld is None else {POLL_UNHELD: poll_unheld}
    return {"source": "sentiment", "params": params}


# --- the poller's symbol set ----------------------------------------------------------


async def test_default_polls_only_held_symbols(db: Database):
    two = await bot_two(db, sources=["sentiment"], symbols={"BTC/USD": "shadow"})
    svc = JournalingSentiment(db)
    bot = runner(
        db, SharedMarketData(StubMarketData()), two, [HOLD] * 2, sentiment=svc,
        observes_account=False,
    )  # fmt: skip
    await bot.tick(datetime.now(tz=UTC), force=True)
    assert svc.polled == []  # nothing held, toggle off: no spend


async def test_toggle_on_polls_the_bots_unheld_symbols_at_the_same_cadence(db: Database):
    two = await bot_two(
        db, sources=[sentiment_sub(True)], symbols={"BTC/USD": "shadow", "ETH/USD": "shadow"}
    )
    svc = JournalingSentiment(db)
    bot = runner(
        db, SharedMarketData(StubMarketData()), two, [HOLD] * 6, sentiment=svc,
        symbols=[BTC, ETH], observes_account=False,
    )  # fmt: skip
    now = datetime.now(tz=UTC)
    await bot.tick(now, force=True)
    assert sorted(svc.polled) == ["BTC/USD", "ETH/USD"]
    await bot.tick(now + timedelta(hours=2), force=True)
    assert len(svc.polled) == 2  # inside the 3h cadence
    await bot.tick(now + timedelta(hours=3), force=True)
    assert len(svc.polled) == 4


async def test_runner_for_carries_the_toggle_from_the_spec(db: Database):
    one = await bot_one(db)
    template = runner(db, SharedMarketData(StubMarketData()), one, [])
    template.sentiment = JournalingSentiment(db)
    universe = default_registry()

    def built(sub) -> object:
        spec = bot_spec(symbols={"ETH/USD": "shadow"}, capital_usd=5_000.0, sources=[sub])
        return runner_for(
            {"id": 9, "name": "b", "version": 1, "spec": spec}, template=template, universe=universe
        )

    assert built(sentiment_sub()).sentiment_poll_unheld is False
    # One cadence per symbol across Bots: the runners share Bot #1's poll stamps.
    assert built(sentiment_sub()).sentiment_stamps is template.sentiment_stamps
    assert built(sentiment_sub(False)).sentiment_poll_unheld is False
    assert built(sentiment_sub(True)).sentiment_poll_unheld is True


async def test_two_bots_on_one_symbol_poll_it_once_per_cadence(db: Database):
    """The poller's set is held ∪ toggled-on Bots' symbols: a symbol both Bot #1 holds and
    Bot #2 polls is scored once, not twice."""
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=100.0
    )
    one = await bot_one(db)
    two = await bot_two(db, sources=[sentiment_sub(True)], symbols={"BTC/USD": "shadow"})
    svc = JournalingSentiment(db)
    shared = SharedMarketData(StubMarketData())
    first = runner(db, shared, one, [HOLD] * 4, sentiment=svc)
    second = runner(
        db, shared, two, [HOLD] * 4, sentiment=svc, observes_account=False,
        sentiment_stamps=first.sentiment_stamps,  # what runner_for shares under a Supervisor
    )  # fmt: skip
    supervisor = Supervisor([first, second], shared)

    now = datetime.now(tz=UTC)
    await first.tick(now, force=True)  # Bot #1 polls its holding first
    await second.tick(now + timedelta(minutes=1), force=True)
    assert svc.polled == ["BTC/USD"]
    await supervisor.tick(now + timedelta(hours=3, minutes=2), force=True)
    assert svc.polled == ["BTC/USD", "BTC/USD"]


# --- the spec and the command ----------------------------------------------------------


def test_polls_unheld_reads_the_sentiment_subscription():
    assert polls_unheld([]) is False
    assert polls_unheld([sentiment_sub()]) is False
    assert polls_unheld([sentiment_sub(True), {"source": "brief"}]) is True
    assert polls_unheld([{"source": "brief", "params": {POLL_UNHELD: True}}]) is False


def test_a_non_boolean_toggle_is_refused():
    with pytest.raises(BotSpecError, match="poll_unheld must be true or false"):
        bot_spec(
            symbols={"BTC/USD": "shadow"},
            capital_usd=5_000.0,
            sources=[{"source": "sentiment", "params": {POLL_UNHELD: "yes"}}],
        )


async def test_flipping_the_toggle_is_a_new_audited_version(db: Database):
    await bot_one(db)
    desk = BotDesk(db)
    await bot_two(db, sources=["sentiment", "brief"])

    on = await desk.set_sentiment_poll("bot-2", poll_unheld=True)
    assert on["version"] == 2
    assert on["spec"]["sources"][0] == sentiment_sub(True)
    assert on["spec"]["sources"][1]["source"] == "brief"  # other Sources untouched
    assert (await desk.set_sentiment_poll("bot-2", poll_unheld=True))["version"] == 2

    off = await desk.set_sentiment_poll("bot-2", poll_unheld=False)
    assert off["version"] == 3
    assert off["spec"]["sources"][0] == sentiment_sub()  # off is the default: no param

    audits = await db.fetch_all(
        "SELECT action, payload FROM audit_events WHERE category = 'bots' AND subject = 'bot-2'"
        " ORDER BY id"
    )
    assert [a["action"] for a in audits] == [
        "create_bot",
        "set_sentiment_poll",
        "set_sentiment_poll",
    ]
    assert audits[1]["payload"]["from_version"] == 1
    assert audits[1]["payload"]["poll_unheld"] is True


async def test_bot_one_and_a_bot_without_sentiment_are_refused(db: Database):
    await bot_one(db)
    desk = BotDesk(db)
    with pytest.raises(BotSpecError, match="bot-1 follows Settings"):
        await desk.set_sentiment_poll(BOT_ONE, poll_unheld=True)

    await bot_two(db, sources=["brief"])
    with pytest.raises(BotSpecError, match="does not read sentiment"):
        await desk.set_sentiment_poll("bot-2", poll_unheld=True)
    # Turning off what is not on is no change, not an error.
    assert (await desk.set_sentiment_poll("bot-2", poll_unheld=False))["version"] == 1


# --- every door -------------------------------------------------------------------------

CREATE = {
    "name": "bot-2-largecap",
    "symbols": {"ETH/USD": "shadow"},
    "capital_usd": 5_000.0,
    "sources": ["sentiment", "brief"],
    "prompt_variant": "equity-largecap",
}


def test_the_toggle_answers_the_same_on_every_door(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    boot = settings(loop_symbols="BTC/USD")
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot)))

    def via_api() -> list:
        with TestClient(create_app(boot)) as tc:
            assert tc.post("/bots", json=CREATE).status_code == 200
            out = [
                tc.post("/bots/bot-2-largecap/sentiment-poll", json={"poll_unheld": True}).json(),
                tc.post("/bots/bot-2-largecap/sentiment-poll", json={"poll_unheld": False}).json(),
            ]
            refused = tc.post("/bots/bot-1/sentiment-poll", json={"poll_unheld": True})
            assert refused.status_code == 422 and "bot-1 follows Settings" in refused.text
        return out

    async def via_mcp() -> list:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            await BotDesk(db).seed(BOT_ONE, bot_one_spec(boot))
            async with Client(build_mcp(build_registry(db))) as mcp_client:
                made = await mcp_client.call_tool("create_bot", CREATE)
                assert not made.is_error, made.content
                out = []
                for value in (True, False):
                    result = await mcp_client.call_tool(
                        "set_sentiment_poll", {"name": "bot-2-largecap", "poll_unheld": value}
                    )
                    assert not result.is_error, result.content
                    out.append(result.structured_content)
                refused = await mcp_client.call_tool(
                    "set_sentiment_poll", {"name": "bot-1", "poll_unheld": True}
                )
                assert refused.is_error and "bot-1 follows Settings" in str(refused.content)
            return out
        finally:
            await db.close()

    def via_cli() -> list:
        made = CliRunner().invoke(
            cli.app,
            [
                "bot", "create", "bot-2-largecap", "--symbols", "ETH/USD", "--capital", "5000",
                "--source", "sentiment", "--source", "brief", "--prompt", "equity-largecap",
            ],
        )  # fmt: skip
        assert made.exit_code == 0, made.output
        out = []
        for flag in ("--poll-unheld", "--no-poll-unheld"):
            printed = CliRunner().invoke(cli.app, ["bot", "sentiment", "bot-2-largecap", flag])
            assert printed.exit_code == 0, printed.output
            out.append(json.loads(printed.output))
        refused = CliRunner().invoke(cli.app, ["bot", "sentiment", "bot-1", "--poll-unheld"])
        assert refused.exit_code == 1
        return out

    api = via_api()
    _reset_store()
    mcp = asyncio.run(via_mcp())
    _reset_store()
    printed = via_cli()

    assert api == mcp == printed
    assert [r["version"] for r in api] == [2, 3]
    assert api[0]["spec"]["sources"][0] == sentiment_sub(True)
    assert api[1]["spec"]["sources"][0] == sentiment_sub()
