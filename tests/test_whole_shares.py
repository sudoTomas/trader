"""Whole shares (#73): a symbol the venue will not fill fractionally trades in whole shares.

Seams: ``evaluate_chain`` (RiskGuard rounds, and the rounding is a journaled verdict),
``AlpacaBroker.get_asset`` (the venue's flags), the loop build (symbols carry the venue's
flags; a failed lookup fails closed), and the executor (a share count reaches the venue).
"""

from __future__ import annotations

from tests.test_risk_chain import LIMITS, NOW, checks, ctx, denied_by
from trader.core.symbols import AssetClass, make_symbol
from trader.core.types import OrderIntent, Side
from trader.risk.guard import AccountState, evaluate_chain

WHOLE = make_symbol("BALY", AssetClass.US_EQUITY, shortable=False, fractionable=False)
PRICE = 18.0


def whole(intent: OrderIntent, *, held: float = 0.0, **kw):
    return evaluate_chain(
        ctx(
            intent=intent,
            symbol=WHOLE,
            price=PRICE,
            account=AccountState(equity=100_000.0, positions={"BALY": held}),
            **kw,
        )
    )


def test_an_open_rounds_down_to_whole_shares_and_says_so():
    outcome = whole(OrderIntent("BALY", Side.BUY, notional=100.0))

    assert outcome.approved
    assert (outcome.intent.qty, outcome.intent.notional) == (5, None)  # 100 / 18 = 5.55
    assert checks(outcome)[-1] == "whole_shares"
    last = outcome.verdicts[-1]
    assert last.action == "clamp" and last.notional == 90.0
    assert "5 whole shares" in last.reason


def test_an_open_worth_less_than_one_share_is_denied():
    outcome = whole(OrderIntent("BALY", Side.BUY, notional=10.0), limits=LIMITS)

    assert denied_by(outcome) == "whole_shares"
    assert "less than one share" in outcome.reason


def test_a_close_rounds_down_or_takes_the_whole_holding():
    partial = whole(OrderIntent("BALY", Side.SELL, notional=100.0), held=10)
    assert partial.approved and partial.intent.qty == 5

    # A request at or above the holding's value closes exactly what is held.
    full = whole(OrderIntent("BALY", Side.SELL, notional=500.0), held=10)
    assert full.approved and full.intent.qty == 10
    assert full.verdicts[-1].reason == "closes the whole holding (10 shares)"

    dust = whole(OrderIntent("BALY", Side.SELL, notional=10.0), held=10)
    assert denied_by(dust) == "whole_shares"
    assert "less than one share" in dust.reason


def test_fractionable_symbols_are_untouched():
    outcome = evaluate_chain(ctx())
    assert outcome.approved and outcome.intent.notional is not None
    assert "whole_shares" not in checks(outcome)


def test_a_symbol_the_venue_will_not_trade_takes_no_opens_but_can_close():
    halted = make_symbol("DAIC", AssetClass.US_EQUITY, shortable=False, tradable=False)

    def run(side, held):
        return evaluate_chain(
            ctx(
                intent=OrderIntent("DAIC", side, notional=100.0),
                symbol=halted,
                price=PRICE,
                account=AccountState(equity=100_000.0, positions={"DAIC": held}),
                now=NOW,
            )
        )

    opened = run(Side.BUY, 0)
    assert denied_by(opened) == "class_policy" and "not tradable" in opened.reason
    assert run(Side.SELL, 10).approved


async def test_the_broker_reads_the_venue_s_asset_flags():
    import httpx
    import pytest

    from tests.test_executor import broker_with
    from trader.brokers.base import BrokerError

    seen = []

    def venue(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={"symbol": "BALY", "status": "active", "tradable": True, "fractionable": False},
        )

    asset = await broker_with(venue).get_asset("BALY")
    assert seen == ["/v2/assets/BALY"]
    assert (asset.symbol, asset.tradable, asset.fractionable) == ("BALY", True, False)

    inactive = broker_with(
        lambda r: httpx.Response(200, json={"status": "inactive", "tradable": True})
    )
    assert (await inactive.get_asset("OLD")).tradable is False  # inactive is not tradable

    missing = broker_with(lambda r: httpx.Response(404, json={"message": "not found"}))
    with pytest.raises(BrokerError, match="HTTP 404"):
        await missing.get_asset("NOPE")


class VenueWithAssets:
    """FakeAlpaca plus the assets endpoint: per-symbol flags, or an outage."""

    def __init__(self, flags: dict[str, tuple[bool, bool]], *, down: bool = False):
        from tests.test_executor import FakeAlpaca

        self.fake = FakeAlpaca()
        self.flags = flags
        self.down = down
        self.asked: list[str] = []

    def __getattr__(self, name):
        return getattr(self.fake, name)

    async def get_account(self):
        from trader.brokers.base import AccountSnapshot

        return AccountSnapshot(100_000.0, 100_000.0, 200_000.0, 100_000.0, "ACTIVE", True)

    async def get_asset(self, symbol):
        from trader.brokers.base import BrokerError, VenueAsset

        self.asked.append(symbol)
        if self.down:
            raise BrokerError("alpaca: HTTP 503")
        tradable, fractionable = self.flags[symbol]
        return VenueAsset(symbol, tradable, fractionable)


def runner_on(db, venue, symbols, replies=()):
    from tests.test_loop import StubLLM, StubMarketData
    from trader.loop.executor import Executor
    from trader.loop.runner import LoopRunner
    from trader.risk.guard import RiskGuard

    return LoopRunner(
        db=db,
        market_data=StubMarketData(),
        llm=StubLLM(list(replies)),
        guard=RiskGuard(db, LIMITS),
        symbols=symbols,
        shadow=False,
        equity=100_000.0,
        broker=venue,
        executor=Executor(db, venue),
    )


async def test_the_loop_takes_each_stock_s_flags_from_the_venue(db):
    from tests.test_loop import BTC

    baly = make_symbol("BALY", AssetClass.US_EQUITY, shortable=False)  # class default: True
    daic = make_symbol("DAIC", AssetClass.US_EQUITY, shortable=False)
    venue = VenueWithAssets({"BALY": (True, False), "DAIC": (False, False)})
    runner = runner_on(db, venue, [BTC, baly, daic])

    await runner.refresh_venue_flags()

    assert venue.asked == ["BALY", "DAIC"]  # crypto is not asked
    by = {s.ticker: s for s in runner.symbols}
    assert (by["BALY"].tradable, by["BALY"].fractionable) == (True, False)
    assert (by["DAIC"].tradable, by["DAIC"].fractionable) == (False, False)
    assert by["BTC/USD"].fractionable and by["BTC/USD"].tradable
    assert runner.symbol("BALY").fractionable is False


async def test_a_failed_lookup_fails_closed_to_whole_shares(db):
    spy = make_symbol("SPY", AssetClass.US_EQUITY, shortable=False)
    runner = runner_on(db, VenueWithAssets({}, down=True), [spy])

    await runner.refresh_venue_flags()

    assert runner.symbol("SPY").fractionable is False
    assert runner.symbol("SPY").tradable is True  # an outage is not a halt


async def test_a_model_open_reaches_the_venue_as_whole_shares(db):
    from tests.test_loop import NOW, OPEN

    baly = make_symbol("BALY", AssetClass.US_EQUITY, shortable=False)
    venue = VenueWithAssets({"BALY": (True, False)})
    runner = runner_on(db, venue, [baly], replies=[OPEN])  # $500 at the stub's $159
    await runner.refresh_venue_flags()

    [result] = await runner.tick(NOW)

    assert result.status == "executed", result.reason
    assert venue.fake.submitted[0]["qty"] == 3  # 500 / 159 = 3.14
    order = await db.fetch_one("SELECT qty, notional FROM orders")
    assert float(order["qty"]) == 3 and order["notional"] is None
