# Lessons

The hardened lessons behind the code, one entry per lesson, keyed by a slug that comments
in `src/` and `tests/` cite as `see docs/lessons.md#<slug>`. Most were earned in the
predecessor project (v1, a private archive; paths given as "in v1: `...`") or during the
first live-paper weeks of this one. Each entry says **what broke** and **what we do now**,
and where the rule lives. Nothing here is a tracker: open work is in
[`tracker.md`](tracker.md).

## nemotron-thinking-off

**What broke.** With the model's thinking mode left on, the self-hosted Nemotron vLLM
endpoint collapsed to ~0.35 tok/s. Thinking consumed whole token budgets (a 300-token cap
produced no answer at all), and guided JSON decoding depends on direct output.

**What we do now.** Every Nemotron request carries
`chat_template_kwargs: {"enable_thinking": false}`. The preset client bakes it in
(`apply_thinking_guard`), a generic decide endpoint whose URL equals the preset's gets the
same flag forced, and it is never sent to a vendor API. Deliberation comes from the desk,
not from thinking tokens.

**Where.** `src/trader/llm/nemotron.py`, `TRADER_NEMOTRON_BASE_URL` in
`src/trader/ops/config.py`.

## no-market-data-storage

**What broke.** v1 stored a full candle window inside every decision row. The table grew to
3.6 GB, one `fetch_all` loaded 19 GiB, and the strategy-discovery fitness signal was
computed on the corrupt result.

**What we do now.** The live loop fetches bars on each tick and computes indicators in
memory; nothing is stored. The `bars` cache is one immutable candle per row, MB-scale,
bounded retention, and replay-only: a test asserts that no live-path data module imports
the store. Large result sets stream through `fetch_iter`, never `fetch_all`.

**Where.** `src/trader/loop/context.py`, `src/trader/data/bars_cache.py`,
`src/trader/store/db.py`, `src/trader/store/migrations/0001_baseline.sql`,
`tests/test_bars_cache.py`.

## no-langgraph-checkpoints

**What broke.** v1 ran a linear, deterministic pipeline through a LangGraph StateGraph. The
graph bought nothing, and its Postgres checkpointer accumulated 14 GB of checkpoints that
were never resumed.

**What we do now.** Plain async. Desk fan-out is `asyncio.gather`; human-in-the-loop is the
approvals table, not a graph interrupt.

## sync-tick-blocked-health

**What broke.** A synchronous tick blocked the event loop; `/health` stopped answering and
the container's autoheal restart-looped it.

**What we do now.** Everything in the loop is async and blocking broker/data fetches run
off the event loop, so `/health` answers even under a slow model call.

**Where.** `src/trader/loop/runner.py`.

## transient-vs-crash-backoff

**What broke.** v1 treated an endpoint outage or a timeout like a bug: the trader crashed
on transient errors and armed its crash backoff for infra blips.

**What we do now.** `DeciderError.transient` classifies infra blips (endpoint down,
timeout, refusal); those back off gently and never touch the crash counter. A genuine
exception backs off exponentially. Either way the loop keeps running: a dead loop is an
unmanaged position book.

**Where.** `src/trader/loop/decider.py`, `src/trader/loop/runner.py`.

## cooldowns-start-on-dispatch

**What broke.** In v1 evaluating a trigger started its cooldown. A trigger that fired while
a run was already in flight was suppressed, then silenced by a cooldown it never used.

**What we do now.** `SignalScheduler.evaluate()` updates baselines but never starts
cooldowns; `mark_dispatched()` does, and only when a wakeup actually launches. The runner
is single-flight, so a suppressed trigger re-fires on the next tick. (in v1:
`engine/signal_monitor.py`.)

**Where.** `src/trader/loop/scheduler.py`, `src/trader/loop/runner.py`.

## indicators-over-mcp-returned-null

**What broke.** v1 bound 28 MCP tools into the decide call and fetched indicators through
them: 120-second timeouts, and the indicators came back null for 987 of 987 decisions.

**What we do now.** No LLM tool calls in the hot path. `ContextBuilder` pre-fetches bars,
indicators are a few dozen lines of local numpy, and the prompt is a finished string
before the model runs. Desk analysts read the same already-built context and do no new
I/O.

**Where.** `src/trader/loop/context.py`, `src/trader/data/indicators.py`,
`src/trader/desk/analysts.py`.

## closes-must-land

**What broke.** v1's broker mirror treated opens and closes alike, so a failed close was as
quiet as a failed open, and a close sized from `qty` rather than `qty_available` oversold
shares already committed to open orders.

**What we do now.** Opens are best-effort and journaled; a failed close is journaled loudly
because an unmanaged position is the worse risk. Position rows carry `qty_available`.
(in v1: `engine/sleeve_broker_mirror.py`.)

**Where.** `src/trader/loop/executor.py`, `src/trader/brokers/alpaca.py`.

## approval-price-tolerance

**What broke.** An approval granted against a stale quote was executed as-is, producing
fills at a price nobody had approved.

**What we do now.** Redeeming an approval re-validates freshness (`approval_ttl_seconds`)
and price (`approval_price_tolerance_frac`) against the current mark and re-runs the
guard; a stale approval is refused.

**Where.** `src/trader/risk/approvals.py`, `src/trader/risk/limits.py`,
`tests/test_risk_state.py`.

## breaker-resume-rebaselines

**What broke.** A drawdown breaker measured against a monotonic high-water mark re-tripped
on the very next tick after the operator resumed, because resume cleared the halt but not
the baseline.

**What we do now.** Operator resume clears the halt and re-baselines the high-water. A
breach halts new opens only; exits stay managed. (in v1: `engine/sleeve.py::resume`.)

**Where.** `src/trader/risk/breakers.py`.

## risk-chain-consolidated

**What broke.** v1 spread its risk limits across a sleeve, a translator, and a tournament
module; no single place said what would stop an order.

**What we do now.** One ordered, fail-closed `RiskGuard` chain in pure Python. Each check
returns allow / deny / clamp; unreadable input is a deny. Limits are code, never prompt
text.

**Where.** `src/trader/risk/guard.py`, `src/trader/risk/limits.py`.

## migration-runner-aborts

**What broke.** v1's migration runner logged a failure and continued, leaving half-applied
schemas.

**What we do now.** A failed migration rolls back, raises, and nothing after it is applied.

**Where.** `src/trader/store/migrate.py`.

## ibkr-deferred

**What broke.** IBKR's auth and session model (daily re-login, a gateway container,
autoheal) dominated the cost of v1's integration.

**What we do now.** Alpaca paper only. `Broker` stays a narrow protocol rather than an
Alpaca-shaped class, so IBKR can be added back against the preserved v1 adapter and its
reconcile (in v1: `adapters/broker/{ibkr,reconcile}.py`).

**Where.** `src/trader/brokers/base.py`.

## macro-lab-retired

**What broke.** v1's macro-lab forecasting service lost to a naive baseline on 25 of 26
series, its VaR was `np.random`, and its regime detector was degenerate.

**What we do now.** Macro context comes from the operator-curated Research Desk (notes
compiled into a versioned Macro View), advisory by construction.

**Where.** `src/trader/research/`.

## macro-view-advisory-firewall

**What broke.** An audit of an LLM-maintained wiki (the ai-quoting project) showed how
compiled knowledge rots: uncited claims laundered into the digest, contradictions quietly
resolved by the model, and stale price levels read as current. Research prose that could
reach sizing or breakers would turn operator opinion into an order path.

**What we do now.** The Macro View slice occupies its own budgeted lane in the decide
prompt; entries drop whole, never truncated mid-thought; an empty render omits the block.
RiskGuard has no parameter that can carry it (asserted by test) and every decision
journals its `macro_view_version`. The compiler enforces the anti-slop rules in code:
sections that cite no included note id are dropped, disputed notes are preserved and
labeled, every `$` amount is stamped example-not-current, and stale/retired notes never
reach the prompt. Briefs apply the same rules per symbol. Research roles must work with
no Grok credential at all, on the Nemotron backstop, so the desk never depends on xAI.

**Where.** `src/trader/loop/context.py`, `src/trader/research/compiler.py`,
`src/trader/research/briefs.py`, `src/trader/llm/registry.py`,
`tests/test_research_compiler.py`.

## global-thesis-cross-asset-only

**What broke.** During the SPY shadow review, with only ETH-scoped notes in the desk, the
compiler wrote an ETH-flavoured "global" thesis and the global lane rendered it into SPY
prompts.

**What we do now.** `global_thesis` carries only cross-asset claims and is empty when the
notes are single-asset; asset-class sections scope as before.

**Where.** `src/trader/research/compiler.py`, `tests/test_research_compiler.py`.

## no-inputs-no-brief

**What broke.** Nothing fired in production; this is the [sentiment
rule](#sentiment-no-citations-is-unavailable) applied to briefs. A brief synthesized with
no store-resident inputs would be the model's prior presented as research.

**What we do now.** A brief is compiled only from notes scoped to the symbol plus the
latest ok sentiment row; no inputs means no brief and no LLM call. An uncited or
ghost-cited thesis drops the whole brief; uncited points drop individually.

**Where.** `src/trader/research/briefs.py`, `tests/test_research_briefs.py`.

## research-desk-capture-once

**What broke.** The same wiki audit: a hand-maintained compiled knowledge base drifted
out-of-band, and analytics nobody audited (v1's macro-lab) accumulated without a
staleness rule.

**What we do now.** Notes are immutable markdown captured once from any surface; the
compiler recompiles asynchronously from what `list()` returns; every note auto-stales
after a window unless pinned, and retiring is an explicit operator act. The markdown
mirror is strictly one-way (DB to files, regenerated from scratch, gitignored) so nothing
reads those files back.

**Where.** `src/trader/research/notes.py`, `src/trader/research/mirror.py`,
`tests/test_research.py`, `tests/test_research_mirror.py`.

## sentiment-no-citations-is-unavailable

**What broke.** v1's X-sentiment poll accepted answers that cited nothing while search
filters were active. Those numbers were the model's prior, not live X, and were journaled
as if measured.

**What we do now.** Zero citations with filters active means the poll is journaled
`unavailable` and never carries a score, and a degraded read can never fire the swing
trigger. Every call is metered under the `sentiment` role with a per-day cap.

**Where.** `src/trader/research/sentiment.py`, `src/trader/loop/scheduler.py`,
`src/trader/store/migrations/0003_sentiment.sql`, `tests/test_sentiment.py`.

## sentiment-held-symbols-only

**What broke.** Sentiment polling was 98% of LLM spend (about $3 a day after the x_search
repricing) for nineteen sentiment wakeups ever.

**What we do now.** Poll every three hours, held symbols only: the swing trigger exists to
re-examine an open position, not to chase the feed. The trigger itself stays off until a
week of journaled scores earns the operator's flip of `TRADER_SENTIMENT_TRIGGER_ENABLED`.
A store-defined Bot may opt its unheld symbols in, per Bot, with `poll_unheld` on its
sentiment subscription (#127; default off, flipped on the desk's Bots page or `trader bot
sentiment`); each extra symbol is one more poll per cadence, so it stays off by default.

**Where.** `src/trader/loop/runner.py` (`_poll_sentiment`), `src/trader/loop/scheduler.py`,
`TRADER_SENTIMENT_POLL_SECONDS` in `src/trader/ops/config.py`.

## oauth-refresh-margin

**What broke.** SuperGrok's device-code OAuth issues single-use rotating refresh tokens and
about fifteen-minute access tokens. A fixed thirty-minute refresh margin was always past
the TTL, so every call refreshed and burned a rotating token. Seeding from the Grok CLI's
`~/.grok/auth.json` would have stolen its single-use token and broken the CLI's own login.

**What we do now.** The refresh margin adapts to the observed TTL (`min(margin, ttl // 3)`).
The rotated refresh token is persisted before the access token is served; refreshes
serialize under a lock; exactly one process owns the lineage; a 403 on refresh is an
entitlement failure and quarantines without retry; `XAI_API_KEY` is a first-class
fallback. Across processes — the crypto-flows seat timer shares trader-api's login — a
`flock` on `<token_path>.lock` is held across read → refresh → write and the store is
re-read after taking it, so a rotation made elsewhere is adopted instead of double-spent
(#68).

**Where.** `src/trader/llm/xai_auth.py`. One manager per process — `create_app` builds
it once and every builder shares it (#63); see `surfaces/api/app.py::build_xai_manager`.

## guided-decoding-omitted-keys

**What broke.** During the desk gate replay, under the longer analyst-enriched prompt, the
decider emitted opens without `side` or `notional`: schema-invalid twice each, five times
in 336 steps. Guided decoding allowed it because optional keys were omittable, and prompt
nudging did not fix it.

**What we do now.** `require_all_keys()` marks every key required on the wire schema, so
omission is ungrammatical; nullable values stay nullable and holds write nulls. Pydantic
validation stays lenient for stubs and non-guided providers. The decider and the critic
both use it.

**Where.** `src/trader/llm/base.py`, `src/trader/loop/decider.py`,
`src/trader/desk/critic.py`, `tests/test_loop.py`.

## analyst-stance-is-earned

**What broke.** In the same gate replay the technical analyst read `neutral` on 330 of 331
steps: no signal at all.

**What we do now.** The technical prompt reserves neutral for genuinely conflicting
indicators; a stance is the default, not the exception.

**Where.** `src/trader/desk/analysts.py` (`TECHNICAL_PROMPT`), `tests/test_desk.py`.

## alpaca-start-default-caps-to-today

**What broke.** The replay harness's first live run: a 100-bar hourly request came back as
15 bars. Without an explicit `start`, Alpaca's bars endpoint defaults to the beginning of
the current day and `limit` silently caps to today's bars.

**What we do now.** Every bars request sends a `start` that covers the lookback with 5x
slack for closed sessions; `limit` plus `sort=desc` still bounds the response to the most
recent bars.

**Where.** `src/trader/data/alpaca.py`, `tests/test_data.py`.

## alpaca-pagination-cap

**What broke.** The desk gate replay: two-week backfills came back as about 210 bars and
the replay skipped six days as no-data. Alpaca caps one response (~210 rows observed)
regardless of `limit` and continues via `next_page_token`.

**What we do now.** Follow `next_page_token` until the lookback is met (bounded at 50
pages); pages continue oldest-ward under `sort=desc`. Never run two replays at once: both
backfill the cache at full speed, 429 Alpaca, and starve the live loop.

**Where.** `src/trader/data/alpaca.py`, `tests/test_data.py`.

## fills-stream-must-be-owned

**What broke.** The first live fill (2026-09-11) never reached the ledger. `run_stream()`
existed but had never been started, and it was fatal on its first error despite its own
docstring saying otherwise. The fill row had to be recovered by hand.

**What we do now.** `LoopRunner.start()` owns the fills-stream task alongside the loop.
The stream reconnects with backoff on every failure; only cancellation ends it. A
background consumer is running because a task owns it, not because the code exists.

**Where.** `src/trader/loop/runner.py`, `src/trader/loop/executor.py` (`run_stream`),
`tests/test_executor.py`.

## crypto-fees-from-asset

**What broke.** The first crypto fill: Alpaca deducts crypto fees from the asset, so the
gross fill quantity overstated the position by the fee (~25 bps); positions came back
venue-spelled (`ETHUSD`); and on 2026-09-18 two ETH closes were rejected because
notional-to-quantity conversion requested 0.196488897 against 0.196485132 available.

**What we do now.** `OrderEvent.position_qty` is venue truth for the ledger;
`get_positions` normalizes symbols through the registry; crypto orders are
quantity-based (notional needs TIF `day`, which crypto lacks); a crypto sell is clamped to
the held quantity.

**Where.** `src/trader/brokers/alpaca.py`, `src/trader/loop/executor.py`.

## operator-doors-on-all-surfaces

**What broke.** Recovering from the fills-stream bug on 2026-09-11 meant resolving parked
approvals and adopting the venue's position by direct service calls in a shell.

**What we do now.** `risk approve|deny` and `broker adopt --confirm ADOPT` are registered
commands with API, MCP, and CLI doors and audit parity across them. One capability is
defined once in the registry; the three surfaces are thin adapters, and no surface has a
raw place-order tool.

**Where.** `src/trader/surfaces/services.py`, `tests/test_operator_doors.py`,
`tests/test_surfaces.py`.

## per-symbol-shadow

**What broke.** Nothing fired; a global shadow flag would have paused the proven ETH slice
to gate SPY.

**What we do now.** `TRADER_SHADOW_SYMBOLS` lists tickers that decide and risk-check but
never submit while the rest of the build trades live-paper; a new symbol earns its flip
there first. Equities joined long-only despite the venue allowing shorts; shortability is
its own later gate.

**Where.** `src/trader/core/symbols.py`, `src/trader/loop/runner.py`,
`tests/test_equities_slice.py`.

## replay-before-flip

**What broke.** Every decide-behavior change (a prompt edit, a model swap, a new prompt
lane, the desk) changes what reaches the venue, and there was no way to compare
candidates without trading them.

**What we do now.** The replay harness reruns the decide stage over the bars cache under
the candidate configuration, frozen at each step's instant (research lanes pinned to the
row that existed then), and journals to `eval_runs` / `eval_steps`, never `decisions`.
Replay, then a shadow window, then one flip, one gate at a time
(`runbooks/decide-change-gate.md`). Scoring is crude and labeled as such; it ranks
configurations, it does not predict PnL.

**Where.** `src/trader/eval/replay.py`, `src/trader/store/migrations/0004_eval.sql`,
`tests/test_replay_eval.py`, `tests/test_desk.py`.

## usage-metering-real-tokens

**What broke.** v1 recorded `None` for local-model token usage, so every budget gate read
$0 spend.

**What we do now.** Real token counts from every provider (vLLM returns them), priced per
model in `PRICES`; an unknown model prices at the configured default and is logged once.
The self-hosted decide model is $0 by definition; that is a fact about hosting, not a
missing number.

**Where.** `src/trader/llm/usage.py`.
