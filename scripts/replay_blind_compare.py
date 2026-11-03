"""Named vs blind replay (#140): how much of a Bot's replay P&L is the model's memory.

Replays every symbol of each named Bot over the same window twice, as the Bot's current
version reads it (its Sources, prompt variant, Sleeve and desk lane): once **named**, once
**blind** (``config={"blind": true}``: no ticker, calendar dates or dollar values in any
prompt; see ``trader.eval.blind``). One run at a time, so one model call is in flight.
Writes ``eval_runs`` / ``eval_steps`` only, like any replay, and every fill pays the
configured cost model (``TRADER_REPLAY_*``, #139).

A blind run drops the Sources that name the asset in free text (the Macro View, briefs).
``--control`` adds a third arm, **named** with those same Sources dropped, so the gap
splits into recall (control - blind) and the dropped lanes (named - control).

    uv run python scripts/replay_blind_compare.py run --bots bot-1,bot-2-largecap --days 20 \\
        --out /tmp/blind-runs.json [--end 2026-11-02T20:00:00+00:00] [--control] [--skip ETH/USD]
    uv run python scripts/replay_blind_compare.py score --runs /tmp/blind-runs.json \\
        --out /tmp/blind-scores.json [--md docs/reports/<date>-blind-replay.md]

``score`` reads the journaled steps only (no market data, no model calls) and reports per
Bot and symbol: net P&L (the run summary's, #139), hit rate (the share of the crude book's
round trips that made money net of their fills; a trade still open at the end is marked
at the last step), the number of open decisions, and the gap named - blind for each.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ARMS = ("named", "blind")
CONTROL = "named-control"
MAX_DAYS = 20  # 480 hourly steps: under the harness's 500-step cap for 24/7 crypto


def _write(path: str, obj: Any) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, default=str))


def _window(end: str | None, days: int) -> tuple[datetime, datetime]:
    if days > MAX_DAYS:
        raise SystemExit(f"--days {days} is over {MAX_DAYS} (the 500-step replay cap)")
    stop = (
        datetime.fromisoformat(end)
        if end
        else datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    )
    if stop.tzinfo is None:
        raise SystemExit("--end must be tz-aware (e.g. 2026-11-02T20:00:00+00:00)")
    return stop - timedelta(days=days), stop


def _config(spec: dict[str, Any], arm: str) -> dict[str, Any]:
    """The run config for one arm, from the Bot version's spec."""
    from trader.eval.blind import split_sources

    sources = spec.get("sources") or []
    config: dict[str, Any] = {
        "sources": sources,
        "prompt_variant": (spec.get("prompt") or {}).get("variant", "default"),
        "desk": bool((spec.get("lanes") or {}).get("desk")),
    }
    capital = (spec.get("sleeve") or {}).get("capital_usd")
    if capital is not None:
        config["sleeve_usd"] = float(capital)
    if arm == "blind":
        config["blind"] = True
    elif arm == CONTROL:
        config["sources"] = split_sources(sources)[0]
    return config


async def run(
    bots: list[str], start: datetime, end: datetime, out: str, control: bool, skip: set[str]
) -> None:
    from trader.loop.bots import BotDesk
    from trader.ops.config import get_settings
    from trader.store.db import Database
    from trader.surfaces.api.app import build_desk, build_evaluator

    settings = get_settings()
    db = Database(settings.database_url)
    await db.open()
    # The API's replay wiring for the live stop, the min hold, the no-trade band, the cost
    # model and the desk for desk-on Bots. The models are the .env chains, not the active
    # Profile: decide is the Nemotron preset unless TRADER_DECIDE_BASE_URL is set.
    harness = build_evaluator(db, settings, desk=build_desk(db, settings))
    if harness is None:
        raise SystemExit("no market-data credentials: replay needs the bars provider")
    registry = settings.symbol_registry()
    arms = (*ARMS, CONTROL) if control else ARMS
    done: list[dict[str, Any]] = []
    try:
        for name in bots:
            bot = await BotDesk(db).get(name)
            for ticker in bot["spec"]["symbols"]:
                if ticker in skip:
                    continue
                sym = registry.get(ticker)
                for arm in arms:
                    label = f"{arm}-{name}-v{bot['version']}-{ticker}"
                    row = await harness.run(
                        sym, start, end, label=label, config=_config(bot["spec"], arm)
                    )
                    done.append(
                        {
                            "run_id": row["id"],
                            "label": label,
                            "bot": name,
                            "bot_version": bot["version"],
                            "symbol": ticker,
                            "arm": arm,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                        }
                    )
                    print(label, row["id"], row["status"], json.dumps(row["summary"]), flush=True)
                    _write(out, done)
    finally:
        await db.close()


def _trades(steps: list[dict[str, Any]]) -> list[float]:
    """The crude book's round trips, net of their fills, rebuilt from the steps: an open
    that filled starts one, a close that filled (the model's or the stop's) ends it, and
    one still open is marked at the last step's price."""
    pnls: list[float] = []
    book: dict[str, Any] | None = None
    last_price = None
    for s in steps:
        d = s["decision"] or {}
        price = float(s["price"]) if s["price"] is not None else None
        last_price = price if price is not None else last_price
        paid = (d.get("fill_cost") or {}).get("total")
        if paid is None or price is None:
            continue
        if d.get("action") == "open" and book is None and d.get("intent"):
            sign = 1.0 if d["intent"]["side"] == "buy" else -1.0
            book = {"price": price, "sign": sign, "notional": d["intent"]["notional"]}
            book["cost"] = paid
        elif d.get("action") == "close" and book is not None:
            move = book["sign"] * (price / book["price"] - 1.0) * book["notional"]
            pnls.append(move - book["cost"] - paid)
            book = None
    if book is not None and last_price is not None:
        pnls.append(
            book["sign"] * (last_price / book["price"] - 1.0) * book["notional"] - book["cost"]
        )
    return pnls


def _gap(a: float | None, b: float | None, digits: int = 2) -> float | None:
    return round(a - b, digits) if a is not None and b is not None else None


async def score(runs_path: str, out: str, md: str | None) -> None:
    from trader.ops.config import get_settings
    from trader.store.db import Database

    settings = get_settings()
    db = Database(settings.database_url)
    await db.open()
    runs = json.loads(Path(runs_path).read_text())  # noqa: ASYNC240 (once, at start)
    scored: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for r in runs:
            row = await db.fetch_one("SELECT * FROM eval_runs WHERE id = %s", (r["run_id"],))
            steps = await db.fetch_all(
                "SELECT ts, status, price, decision FROM eval_steps WHERE run_id = %s ORDER BY ts",
                (r["run_id"],),
            )
            summary = (row or {}).get("summary") or {}
            trades = _trades(steps)
            decided = [s for s in steps if s["decision"]]
            scored.setdefault((r["bot"], r["symbol"]), {})[r["arm"]] = {
                "run_id": r["run_id"],
                "status": (row or {}).get("status"),
                "net_pnl": summary.get("net_pnl"),
                "gross_pnl": summary.get("gross_pnl"),
                "opens": sum(1 for s in decided if s["decision"].get("action") == "open"),
                "trades": len(trades),
                "hit_rate": round(sum(p > 0 for p in trades) / len(trades), 3) if trades else None,
                "errors": summary.get("errors"),
                "blind": summary.get("blind"),
            }
    finally:
        await db.close()

    report: dict[str, Any] = {"by_symbol": [], "by_bot": {}}
    for (bot, symbol), arms in sorted(scored.items()):
        named, blind = arms.get("named", {}), arms.get("blind", {})
        entry = {
            "bot": bot,
            "symbol": symbol,
            **arms,
            "gap": {
                "net_pnl": _gap(named.get("net_pnl"), blind.get("net_pnl")),
                "hit_rate": _gap(named.get("hit_rate"), blind.get("hit_rate"), 3),
                "opens": _gap(named.get("opens"), blind.get("opens"), 0),
            },
        }
        report["by_symbol"].append(entry)
        totals = report["by_bot"].setdefault(bot, {})
        for arm, res in arms.items():
            t = totals.setdefault(arm, {"net_pnl": 0.0, "opens": 0, "trades": 0, "wins": 0})
            t["net_pnl"] = round(t["net_pnl"] + (res.get("net_pnl") or 0.0), 2)
            t["opens"] += res["opens"]
            t["trades"] += res["trades"]
            t["wins"] += round((res["hit_rate"] or 0.0) * res["trades"])
    for totals in report["by_bot"].values():
        for t in totals.values():
            t["hit_rate"] = round(t["wins"] / t["trades"], 3) if t["trades"] else None
        if "named" in totals and "blind" in totals:
            totals["gap"] = {
                "net_pnl": _gap(totals["named"]["net_pnl"], totals["blind"]["net_pnl"]),
                "hit_rate": _gap(totals["named"]["hit_rate"], totals["blind"]["hit_rate"], 3),
                "opens": totals["named"]["opens"] - totals["blind"]["opens"],
            }
    _write(out, report)
    table = _markdown(report)
    print(table)
    if md:
        Path(md).write_text(table + "\n")  # noqa: ASYNC240 (once, at the end)


def _fmt(x: Any) -> str:
    return "-" if x is None else f"{x:,.2f}" if isinstance(x, float) else str(x)


def _markdown(report: dict[str, Any]) -> str:
    """The tables a report starts from: per symbol, then per Bot (gap = named - blind)."""
    head = (
        "| Bot | Symbol | Net named | Net blind | Net gap | Hit named | Hit blind"
        " | Opens named | Opens blind |\n|---|---|---:|---:|---:|---:|---:|---:|---:|"
    )
    rows = [head]
    for e in report["by_symbol"]:
        n, b = e.get("named", {}), e.get("blind", {})
        rows.append(
            f"| {e['bot']} | {e['symbol']} | {_fmt(n.get('net_pnl'))} | {_fmt(b.get('net_pnl'))}"
            f" | {_fmt(e['gap']['net_pnl'])} | {_fmt(n.get('hit_rate'))}"
            f" | {_fmt(b.get('hit_rate'))} | {_fmt(n.get('opens'))} | {_fmt(b.get('opens'))} |"
        )
    rows += [
        "",
        "| Bot | Arm | Net P&L | Hit rate | Opens | Trades |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for bot, totals in report["by_bot"].items():
        for arm, t in totals.items():
            if arm == "gap":
                continue
            rows.append(
                f"| {bot} | {arm} | {_fmt(t['net_pnl'])} | {_fmt(t['hit_rate'])}"
                f" | {t['opens']} | {t['trades']} |"
            )
    return "\n".join(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bots", default="bot-1,bot-2-largecap", help="Comma-separated Bot names.")
    r.add_argument("--days", type=int, default=MAX_DAYS)
    r.add_argument("--end", help="Window end, ISO-8601 tz-aware (default: this hour, UTC).")
    r.add_argument("--control", action="store_true", help="Add the named-control arm.")
    r.add_argument("--skip", default="", help="Comma-separated symbols to leave out.")
    r.add_argument("--out", required=True)
    s = sub.add_parser("score")
    s.add_argument("--runs", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--md", help="Also write the markdown tables here.")
    a = p.parse_args()
    if a.cmd == "run":
        start, end = _window(a.end, a.days)
        bots = [b.strip() for b in a.bots.split(",") if b.strip()]
        skip = {t.strip() for t in a.skip.split(",") if t.strip()}
        asyncio.run(run(bots, start, end, a.out, a.control, skip))
    else:
        asyncio.run(score(a.runs, a.out, a.md))


if __name__ == "__main__":
    main()
