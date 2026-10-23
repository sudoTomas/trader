"""Replay leg of the decide-change gate for a Bot's prompt variant (#127).

Replays the same symbols and window under two prompt variants, one run at a time (one
Nemotron call in flight), then scores every replayed ``open`` by its forward return from
the next 1h bar's open. Writes ``eval_runs`` / ``eval_steps`` only, like any replay.

    uv run python scripts/replay_bot_compare.py run   --out runs.json [--sample N]
    uv run python scripts/replay_bot_compare.py score --runs runs.json --out scores.json

Bars for scoring come from the market-data provider (fetch-on-read, nothing stored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"]
START = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)
END = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)  # 500 hourly steps; shadow starts after
# Bot #2 (bot-2-largecap) v1's Sources and Sleeve, held constant; only the rules differ.
SOURCES = [
    {"source": "sentiment"},
    {"source": "brief"},
    {"source": "held_position"},
    {"source": "macro_view"},
]
SLEEVE_USD = 5000.0
VARIANTS = {"bot1-default": "default", "bot2-largecap": "equity-largecap"}
SESSION_BARS = 6  # regular-session 1h bars a day the calendar calls open (14:00..19:00 UTC, EDT)


def _write(path: str, obj: Any) -> None:
    Path(path).write_text(json.dumps(obj, indent=2, default=str))


async def run(out: str, sample: int) -> None:
    from trader.data.composite import build_market_data
    from trader.eval.replay import ReplayHarness
    from trader.llm.registry import build_llm_registry
    from trader.llm.usage import UsageMeter
    from trader.ops.config import get_settings
    from trader.store.db import Database
    from trader.store.repos import UsageRepo

    settings = get_settings()
    db = Database(settings.database_url)
    await db.open()
    llm = build_llm_registry(settings, UsageMeter(UsageRepo(db)))
    harness = ReplayHarness(db, llm, build_market_data(settings))
    registry = settings.symbol_registry()
    done: list[dict[str, Any]] = []
    try:
        for ticker in SYMBOLS:
            sym = registry.get(ticker)
            for name, variant in VARIANTS.items():
                label = f"{name}-{ticker}-s{sample}"
                row = await harness.run(
                    sym,
                    START,
                    END,
                    label=label,
                    config={
                        "sources": SOURCES,
                        "sleeve_usd": SLEEVE_USD,
                        "prompt_variant": variant,
                        "desk": False,
                    },
                )
                done.append({"run_id": row["id"], "label": label, "symbol": ticker, "prompt": name})
                print(label, row["id"], row["status"], json.dumps(row["summary"]), flush=True)
                _write(out, done)
    finally:
        await db.close()


def _forward(bars: list[Any], ts: datetime, n: int) -> dict[str, float] | None:
    """Return / worst drawdown from the open of the first bar at or after ``ts`` over the
    next ``n`` regular-session bars (None if history does not reach that far)."""
    after = [b for b in bars if b.ts >= ts]
    if len(after) < n:
        return None
    entry = after[0].open
    window = after[:n]
    return {
        "ret": window[-1].close / entry - 1.0,
        "mdd": min(b.low for b in window) / entry - 1.0,
    }


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs) * 100, 3) if xs else None


async def score(runs_path: str, out: str) -> None:
    from trader.data.composite import build_market_data
    from trader.ops.config import get_settings
    from trader.store.db import Database

    settings = get_settings()
    provider = build_market_data(settings)
    registry = settings.symbol_registry()
    db = Database(settings.database_url)
    await db.open()
    runs = json.loads(Path(runs_path).read_text())  # noqa: ASYNC240 (once, at start)
    bars_by: dict[str, list[Any]] = {}
    for ticker in SYMBOLS:
        sym = registry.get(ticker)
        bars = await provider.get_bars(sym, "1h", 1500)
        # Regular session only: the decider wakes on NYSE hours; score on the same bars.
        bars_by[ticker] = [b for b in bars if b.ts >= START and sym.market_calendar.is_open(b.ts)]
    report: dict[str, Any] = {"runs": []}
    try:
        for r in runs:
            run_row = await db.fetch_one("SELECT * FROM eval_runs WHERE id = %s", (r["run_id"],))
            steps = await db.fetch_all(
                "SELECT ts, status, price, decision FROM eval_steps WHERE run_id = %s ORDER BY ts",
                (r["run_id"],),
            )
            bars = bars_by[r["symbol"]]
            opens = [s for s in steps if s["decision"] and s["decision"]["action"] == "open"]
            scored = []
            for s in opens:
                f1 = _forward(bars, s["ts"], SESSION_BARS)
                f5 = _forward(bars, s["ts"], SESSION_BARS * 5)
                fe = _forward(bars, s["ts"], len([b for b in bars if b.ts >= s["ts"]]))
                scored.append(
                    {
                        "ts": s["ts"].isoformat(),
                        "conviction": s["decision"]["conviction"],
                        "fwd_1d": f1,
                        "fwd_5d": f5,
                        "fwd_to_now": fe,
                    }
                )
            decided = [s for s in steps if s["status"] == "decided"]
            report["runs"].append(
                {
                    **r,
                    "status": run_row["status"],
                    "summary": run_row["summary"],
                    "decisions": len(decided),
                    "errors": len([s for s in steps if s["status"] == "error"]),
                    "opens": len(opens),
                    "actions_by_ts": {
                        s["ts"].isoformat(): s["decision"]["action"] for s in decided
                    },
                    "mean_fwd_1d_pct": _mean([o["fwd_1d"]["ret"] for o in scored if o["fwd_1d"]]),
                    "mean_fwd_5d_pct": _mean([o["fwd_5d"]["ret"] for o in scored if o["fwd_5d"]]),
                    "mean_mdd_5d_pct": _mean([o["fwd_5d"]["mdd"] for o in scored if o["fwd_5d"]]),
                    "mean_fwd_to_now_pct": _mean(
                        [o["fwd_to_now"]["ret"] for o in scored if o["fwd_to_now"]]
                    ),
                    "open_steps": scored,
                }
            )
        # Every regular-session step in the window, for the unconditional baseline.
        report["all_steps"] = {}
        for ticker, bars in bars_by.items():
            window = [b for b in bars if b.ts < END]
            f1 = [_forward(bars, b.ts, SESSION_BARS) for b in window]
            f5 = [_forward(bars, b.ts, SESSION_BARS * 5) for b in window]
            report["all_steps"][ticker] = {
                "n": len(window),
                "mean_fwd_1d_pct": _mean([f["ret"] for f in f1 if f]),
                "mean_fwd_5d_pct": _mean([f["ret"] for f in f5 if f]),
                "mean_mdd_5d_pct": _mean([f["mdd"] for f in f5 if f]),
                "window_ret_pct": round((window[-1].close / window[0].open - 1) * 100, 3),
                "last_bar": bars[-1].ts.isoformat(),
            }
    finally:
        await db.close()
    _write(out, report)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--sample", type=int, default=1)
    s = sub.add_parser("score")
    s.add_argument("--runs", required=True)
    s.add_argument("--out", required=True)
    a = p.parse_args()
    if a.cmd == "run":
        asyncio.run(run(a.out, a.sample))
    else:
        asyncio.run(score(a.runs, a.out))


if __name__ == "__main__":
    main()
