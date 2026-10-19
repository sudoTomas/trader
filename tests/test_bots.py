"""Bot #1 (#123): the running agent becomes a Bot with no behaviour change. Its spec is
seeded from Settings at boot as an append-only Bot version (a new one only when the spec
changes, audited), every journaled decision and order names its Bot, and the Bot reads
the same on every door (registry → API → MCP → CLI).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_executor import FakeAlpaca
from tests.test_loop import LIMITS, NOW, OPEN, build_runner
from trader.loop.bots import BOT_ONE, BotDesk, BotNotFound, bot_one_spec
from trader.loop.executor import Executor
from trader.loop.journal import EXECUTED, SHADOW
from trader.ops.config import Settings
from trader.risk.guard import RiskGuard
from trader.risk.limits import RiskLimits
from trader.store.db import Database
from trader.store.repos import BotPositionsRepo, PositionsRepo


def settings(**kwargs) -> Settings:
    return Settings(_env_file=None, auth_disabled=True, **kwargs)


# --- the spec ---------------------------------------------------------------------------


def test_the_spec_names_each_symbol_s_mode_the_triggers_sources_and_account():
    spec = bot_one_spec(
        settings(
            shadow_mode=False,
            loop_symbols="ETH/USD,SPY,AAPL",
            extra_symbols="AAPL",
            shadow_symbols="AAPL",
            briefs_enabled=True,
            sentiment_trigger_enabled=True,
        )
    )
    assert spec["account_id"] == "alpaca-paper"
    assert spec["symbols"] == {"ETH/USD": "live", "SPY": "live", "AAPL": "shadow"}
    assert spec["lanes"] == {"desk": False}
    assert [s["source"] for s in spec["sources"]] == ["fred", "macro_view", "brief"]
    assert spec["triggers"]["sentiment_enabled"] is True
    assert spec["triggers"]["heartbeat_seconds"]["crypto"] == 900
    assert spec["prompt"]["variant"] == "default" and spec["prompt"]["sha256"]
    # A shadow build shadows every symbol, whatever the per-symbol list says.
    shadow_build = bot_one_spec(settings(loop_symbols="ETH/USD,SPY"))
    assert shadow_build["symbols"] == {"ETH/USD": "shadow", "SPY": "shadow"}


# --- seeding ----------------------------------------------------------------------------


async def bot_audits(db: Database) -> list[dict]:
    return await db.fetch_all(
        "SELECT action, subject, payload FROM audit_events WHERE category = 'bots' ORDER BY id"
    )


async def test_seeding_is_idempotent_and_a_changed_spec_is_a_new_audited_version(db: Database):
    desk = BotDesk(db)
    first = await desk.seed(BOT_ONE, bot_one_spec(settings(loop_symbols="BTC/USD")))
    again = await desk.seed(BOT_ONE, bot_one_spec(settings(loop_symbols="BTC/USD")))

    assert (first["id"], first["version"]) == (1, 1)
    assert (again["id"], again["version"]) == (1, 1)
    assert len(await bot_audits(db)) == 1

    changed = await desk.seed(BOT_ONE, bot_one_spec(settings(loop_symbols="BTC/USD,ETH/USD")))
    assert (changed["id"], changed["version"]) == (1, 2)
    assert changed["spec"]["symbols"] == {"BTC/USD": "shadow", "ETH/USD": "shadow"}

    audits = await bot_audits(db)
    assert [(a["action"], a["subject"]) for a in audits] == [
        ("seed_bot_version", BOT_ONE),
        ("seed_bot_version", BOT_ONE),
    ]
    assert audits[1]["payload"]["version"] == 2
    assert audits[1]["payload"]["spec"]["symbols"] == changed["spec"]["symbols"]

    bot = await desk.get(BOT_ONE)
    assert [v["version"] for v in bot["versions"]] == [2, 1]  # newest first, both kept
    with pytest.raises(BotNotFound):
        await desk.get("nope")


async def test_bot_versions_are_append_only(db: Database):
    import psycopg

    await BotDesk(db).seed(BOT_ONE, bot_one_spec(settings()))
    with pytest.raises(psycopg.errors.RaiseException):
        await db.execute("UPDATE bot_versions SET spec = '{}'")
    with pytest.raises(psycopg.errors.RaiseException):
        await db.execute("DELETE FROM bot_versions")


def test_boot_seeds_bot_one_and_hands_it_to_the_runner(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient

    import trader.surfaces.api.app as app_module

    class IdleRunner:
        bot_id = None
        bot_version = None

        async def start(self) -> None: ...

        async def stop(self) -> None: ...

    runner = IdleRunner()
    monkeypatch.setattr(app_module, "build_runner", lambda *a, **kw: runner)
    monkeypatch.setattr(app_module, "build_supervisor", lambda runner, *a, **kw: runner)
    with TestClient(app_module.create_app(settings(loop_enabled=True))) as tc:
        assert tc.get("/bots").json()[0]["version"] == 1
    assert (runner.bot_id, runner.bot_version) == (1, 1)
    # The loop reads the Sources that version names (#124).
    assert runner.sources == bot_one_spec(settings(loop_enabled=True))["sources"]


# --- the journal ------------------------------------------------------------------------


async def seeded(db: Database) -> dict:
    return await BotDesk(db).seed(BOT_ONE, bot_one_spec(settings(loop_symbols="BTC/USD")))


async def test_shadow_decisions_carry_the_bot_and_its_version(db: Database):
    bot = await seeded(db)
    runner = build_runner(db, [OPEN], bot_id=bot["id"], bot_version=bot["version"])
    results = await runner.tick(NOW)

    assert results[0].status == SHADOW
    row = await db.fetch_one("SELECT bot_id, bot_version FROM decisions")
    assert (row["bot_id"], row["bot_version"]) == (1, 1)


async def test_live_orders_carry_the_bot(db: Database):
    bot = await seeded(db)
    runner = build_runner(
        db,
        [OPEN],
        shadow=False,
        executor=Executor(db, FakeAlpaca()),
        bot_id=bot["id"],
        bot_version=bot["version"],
    )
    results = await runner.tick(datetime.now(tz=UTC), force=True)

    assert results[0].status == EXECUTED
    order = await db.fetch_one("SELECT bot_id FROM orders")
    decision = await db.fetch_one("SELECT bot_id, bot_version FROM decisions")
    assert order["bot_id"] == 1
    assert (decision["bot_id"], decision["bot_version"]) == (1, 1)


async def test_stop_loss_closes_carry_the_bot(db: Database):
    from tests.test_loop import HOLD

    bot = await seeded(db)
    await PositionsRepo(db).upsert(
        symbol="BTC/USD", asset_class="crypto", qty=0.5, avg_entry_price=200.0
    )
    # Bot #1's own fills built it, so its Sleeve holds it (#125): the stop may close it.
    await BotPositionsRepo(db).upsert(
        bot_id=bot["id"], symbol="BTC/USD", qty=0.5, avg_entry_price=200.0
    )
    runner = build_runner(
        db,
        [HOLD],
        shadow=False,
        executor=Executor(db, FakeAlpaca()),
        guard=RiskGuard(
            db,
            RiskLimits(
                max_position_frac=0.05, approval_notional_usd=100_000.0, stop_loss_frac=0.08
            ),
        ),
        bot_id=bot["id"],
        bot_version=bot["version"],
    )
    await runner.tick(NOW)
    stopped = await runner.tick(NOW + timedelta(seconds=60))

    assert stopped[0].triggers == ["stop_loss"] and stopped[0].status == EXECUTED
    rows = await db.fetch_all("SELECT trigger, bot_id, bot_version FROM decisions ORDER BY id")
    assert {(r["bot_id"], r["bot_version"]) for r in rows} == {(1, 1)}
    assert (await db.fetch_one("SELECT bot_id FROM orders"))["bot_id"] == 1


async def test_parked_approvals_carry_the_bot_and_its_redeemed_order_does_too(db: Database):
    from tests.test_operator_doors import RedeemRunner, approve

    bot = await seeded(db)
    parking = RiskLimits(max_position_frac=0.05, approval_notional_usd=100.0, park_above_usd=400.0)
    runner = build_runner(
        db,
        [OPEN],
        shadow=False,
        executor=Executor(db, FakeAlpaca()),
        guard=RiskGuard(db, parking),
        bot_id=bot["id"],
        bot_version=bot["version"],
    )
    await runner.tick(datetime.now(tz=UTC), force=True)
    approval = await db.fetch_one("SELECT id, bot_id FROM approvals")
    assert approval["bot_id"] == 1

    redeemer = RedeemRunner(db, FakeAlpaca(), mid=159.0, limits=parking, equity=100_000.0)
    out = await approve(db, approval["id"], redeemer)
    assert out["redeemed"] is True, out
    assert (await db.fetch_one("SELECT bot_id FROM orders"))["bot_id"] == 1


async def test_bot_one_decides_exactly_as_the_agent_did_before(db: Database):
    """Golden parity: stamping the Bot changes the journal's attribution and nothing the
    model or RiskGuard sees."""
    from tests.test_loop import StubLLM

    async def wakeup(**bot) -> tuple[list, dict]:
        runner = build_runner(db, [OPEN], guard=RiskGuard(db, LIMITS), **bot)
        llm: StubLLM = runner.llm
        results = await runner.tick(NOW)
        row = await db.fetch_one(
            "SELECT prompt, raw_output, decision, risk_verdicts, status FROM decisions"
            " WHERE id = %s",
            (results[0].decision_id,),
        )
        return llm.calls, row

    bot = await seeded(db)
    before_calls, before = await wakeup()
    # The same starting journal for both runs: the prompt carries recent decisions.
    await db.execute("TRUNCATE decisions CASCADE")
    after_calls, after = await wakeup(bot_id=bot["id"], bot_version=bot["version"])

    assert after_calls == before_calls  # byte-identical decide prompts
    assert after == before  # same prompt journaled, same decision, same verdicts


# --- the migration ----------------------------------------------------------------------


def test_the_migration_backfills_existing_rows_to_bot_one():
    """Rows written before Bots existed belong to Bot #1 once the migration runs."""
    import os
    import shutil
    import tempfile

    import psycopg

    from trader.store.migrate import MIGRATIONS_DIR, run_migrations

    test_url = os.environ["TRADER_DATABASE_URL"]
    scratch_url = test_url + "_bots_backfill"
    name = scratch_url.rsplit("/", 1)[1]
    with psycopg.connect(test_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')

    bots_migration = next(p.name for p in sorted(MIGRATIONS_DIR.glob("*_bots.sql")))

    async def run() -> dict:
        db = Database(scratch_url, min_size=1, max_size=1)
        await db.open()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    if path.name < bots_migration:
                        shutil.copy(path, Path(tmp) / path.name)
                await run_migrations(db, Path(tmp))
            decision = await db.fetch_one(
                "INSERT INTO decisions (trigger, status) VALUES ('heartbeat', 'shadow')"
                " RETURNING id"
            )
            await db.execute(
                "INSERT INTO orders (client_order_id, symbol, asset_class, side, qty, decision_id)"
                " VALUES ('old', 'BTC/USD', 'crypto', 'buy', 1, %s)",
                (decision["id"],),
            )
            await db.execute("INSERT INTO approvals (order_intent, reason) VALUES ('{}', 'old')")
            await db.execute(
                "INSERT INTO eval_runs (label, symbol, start_ts, end_ts)"
                " VALUES ('old', 'BTC/USD', now(), now())"
            )
            await db.execute(
                "INSERT INTO usage_daily (day, role, provider, model)"
                " VALUES (current_date, 'decide', 'p', 'm')"
            )
            await run_migrations(db)
            out = {}
            for table in ("decisions", "orders", "approvals", "eval_runs", "usage_daily"):
                rows = await db.fetch_all(f"SELECT bot_id FROM {table}")
                out[table] = [r["bot_id"] for r in rows]
            out["bots"] = await db.fetch_all("SELECT id, name FROM bots")
            out["bot_version"] = (await db.fetch_one("SELECT bot_version FROM decisions"))[
                "bot_version"
            ]
            return out
        finally:
            await db.close()

    try:
        out = asyncio.run(run())
    finally:
        with psycopg.connect(test_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')

    assert out.pop("bots") == [{"id": 1, "name": BOT_ONE}]
    # No Bot version existed when these rows were written, so none is claimed.
    assert out.pop("bot_version") is None
    assert out == {t: [1] for t in ("decisions", "orders", "approvals", "eval_runs", "usage_daily")}


# --- every door -------------------------------------------------------------------------


def test_the_bot_reads_the_same_on_every_door(clean_db: None, monkeypatch):
    from fastapi.testclient import TestClient
    from mcp.client import Client
    from typer.testing import CliRunner

    import trader.surfaces.cli.main as cli
    from trader.surfaces.api.app import create_app
    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    boot = settings(loop_symbols="BTC/USD")
    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(boot)))

    with TestClient(create_app(boot)) as tc:  # boot seeds Bot #1
        api_list = tc.get("/bots").json()
        api_bot = tc.get(f"/bots/{BOT_ONE}").json()
        assert tc.get("/bots/nope").status_code == 404

    async def via_mcp() -> tuple:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            async with Client(build_mcp(build_registry(db))) as mcp_client:
                listed = await mcp_client.call_tool("get_bots", {})
                one = await mcp_client.call_tool("get_bot", {"name": BOT_ONE})
            assert not listed.is_error and not one.is_error, (listed.content, one.content)
            return listed.structured_content["result"], one.structured_content
        finally:
            await db.close()

    mcp_list, mcp_bot = asyncio.run(via_mcp())
    printed_list = CliRunner().invoke(cli.app, ["bot", "list"])
    printed_bot = CliRunner().invoke(cli.app, ["bot", "show", BOT_ONE])
    assert printed_list.exit_code == 0, printed_list.output
    assert printed_bot.exit_code == 0, printed_bot.output

    assert api_list == mcp_list == json.loads(printed_list.output)
    assert api_bot == mcp_bot == json.loads(printed_bot.output)
    assert [(b["id"], b["name"], b["version"]) for b in api_list] == [(1, BOT_ONE, 1)]
    assert api_bot["spec"]["symbols"] == {"BTC/USD": "shadow"}
    assert [v["version"] for v in api_bot["versions"]] == [1]
