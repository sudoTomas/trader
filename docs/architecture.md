# Trader — Architecture

Agentic-first autonomous trading agent. Greenfield successor to **trading-agent** (v1),
which is frozen as a private reference archive. This document is the source of truth for the
design; where a decision was learned the hard way, the lesson is catalogued in
[`lessons.md`](lessons.md) and linked by slug.

## Prime directive

**The LLM proposes, the deterministic RiskGuard disposes, the broker executes.** The only
path from an idea to a submitted order runs through the RiskGuard chain in pure Python. No
LLM output, no MCP tool, and no API caller can place an order any other way.

## Principles

1. **Deterministic safety chain.** Every order intent passes one ordered, fail-closed
   `RiskGuard`. Limits are code, never prompt text.
2. **No market-data storage.** The live loop fetches bars on demand and computes indicators
   in-process. A small immutable `bars` cache exists only for replay-shaped work. (v1 stored
   full candle windows per tick → a 3.6 GB table and 19 GiB loads;
   [no-market-data-storage](lessons.md#no-market-data-storage).)
3. **One service layer, three surfaces.** Every capability is a command or query registered
   once in `surfaces/services.py` and exposed identically via REST API, MCP server, and CLI.
   Agentic-first means the MCP tools and the API are first-class, not afterthoughts.
4. **Per-role model routing.** A registry maps each LLM role → (provider, model, credential).
   The money-path decide runs on any OpenAI-compatible endpoint (`TRADER_DECIDE_*`) or the
   self-hosted Nemotron preset; research/compiler roles run on Grok (or `TRADER_RESEARCH_*`).
   No global endpoint assumption. (v1 had one hardcoded URL + `api_key="EMPTY"`.)
5. **Paper-only, hard-gated.** Live trading is impossible unless an explicit env flag is set;
   the default build cannot construct a live client.
6. **Everything auditable.** Every state-changing action writes an append-only `audit_events`
   row. Every decision journals the exact context version it saw (research, prompt, output).

## What we deliberately did NOT carry from v1

- **LangGraph.** The pipeline is linear and deterministic; a StateGraph bought nothing but
  14 GB of never-resumed Postgres checkpoints. Future desk fan-out is `asyncio.gather`;
  human-in-the-loop is an approvals table, not a graph interrupt
  ([no-langgraph-checkpoints](lessons.md#no-langgraph-checkpoints)).
- **The evolution / strategy-discovery engine.** Mothballed in v1; its fitness signal was
  computed on corrupt data anyway ([no-market-data-storage](lessons.md#no-market-data-storage)).
  Discovery, if it returns, is a separate offline service over the bars cache — parametric
  families first.
- **IBKR / IB Gateway.** Its auth/session model dominated its integration cost (daily
  re-login, gateway container, autoheal; [ibkr-deferred](lessons.md#ibkr-deferred)).
  Abandoned for now. To add it back: implement the `Broker` protocol against v1's
  `apps/agent/src/trading_agent/adapters/broker/ibkr.py` and its reconcile
  (`.../adapters/broker/reconcile.py`) — the reference is preserved.
- **macro-lab.** Retired in v1; forecasts lost to a naive baseline 25/26, VaR was
  `np.random`, regime was degenerate ([macro-lab-retired](lessons.md#macro-lab-retired)).
  Macro context now comes from the
  operator-curated Research Desk, not a forecasting service.

## Package layout

```
src/trader/
├── core/       # domain types (Symbol, AssetClass, Decision, OrderIntent, Position, Bar);
│               # market calendars (always_open, nyse). Pure, no I/O.
├── data/       # MarketData protocol; alpaca_data (primary), twelve_data (fallback), fred;
│               # local indicators (RSI/SMA/EMA/realized-vol); bars read-through cache.
├── llm/        # provider registry {role→(provider,model,credential)}; nemotron client
│               # (MUST send chat_template_kwargs.enable_thinking=false — lessons.md);
│               # xai device-code OAuth (+ XAI_API_KEY fallback); usage metering.
├── brokers/    # Broker protocol; alpaca/ (REST + trade-updates stream). ibkr = docs only.
├── risk/       # RiskGuard ordered chain; account breakers; approvals; kill switches; kelly.
├── loop/       # SignalScheduler → ContextBuilder → Decider → Executor → Journal.
├── research/   # brief generator (Grok); x_sentiment (x_search, degraded-honest);
│               # Research Desk (notes store, Grok macro-view compiler, md-mirror export);
│               # screener (M5).
├── store/      # db.py; repos; ONE baseline migration.
├── surfaces/   # services.py (command/query registry) → api/ (FastAPI) + mcp/ (FastMCP
│               # streamable HTTP /mcp) + cli/ (typer). Health identical on all three.
└── ops/        # config (pydantic-settings); structlog; lifespan; /health.
```

## The money path (crypto slice = first vertical)

```
SignalScheduler → ContextBuilder → (analyst desk) → Decider → (Confirm) → (critic)
  → RiskGuard → Executor → Journal
```

- **SignalScheduler** — event-driven wakeups: heartbeat, price-move, vol-spike, sentiment,
  position-risk. Per-asset-class cadence and calendar gating (crypto = always_open; equities
  = nyse). Cooldowns start on dispatch, not evaluation
  ([cooldowns-start-on-dispatch](lessons.md#cooldowns-start-on-dispatch)).
- **ContextBuilder** — fetch ~100 bars/interval from Alpaca Data, compute indicators locally,
  attach the Macro View slice + X-sentiment + open positions + recent journal. No LLM tool
  calls in this hot path (v1 bound 28 MCP tools here → 120 s timeouts;
  [indicators-over-mcp-returned-null](lessons.md#indicators-over-mcp-returned-null)).
- **Decider** — one Nemotron completion, JSON-schema-validated, 1 retry with the validation
  errors fed back. Transient (infra) vs crash errors classified so infra blips don't arm
  backoff ([transient-vs-crash-backoff](lessons.md#transient-vs-crash-backoff)). Runs in
  **shadow mode** first: journal the decision, submit nothing. Its output is the
  **Draft**: action, side, notional, cited rationale (a hold is a Draft with no order).
- **Confirm** (`confirm` role, `TRADER_CONFIRM_*`, absent unless configured) — Kev, a Jev
  judgment model, answers typed questions about every open or close Draft: the action
  (hold / open / close) and the side for the Draft's action, as probabilities. It always
  judges the raw Draft, before the critic. **First stage (#99) is journal-only**: the
  answer lands in the decision's `confirm` column (`verdict: journal_only`) and nothing
  reads it back. Unreachable, slow, HTTP error or malformed answers journal a skip with
  the status text, and the Draft proceeds. The margin that later lets a Confirm turn a
  Draft into a hold is chosen from this column by replay, in a second change. A Confirm
  can never open a trade the Draft did not propose or change its notional.
- **Critic** (desk, `TRADER_DESK_ENABLED`) — a cross-model second opinion that can only
  approve, shrink or veto the Draft; fails open to a journaled pass-through.
- **RiskGuard** — ordered, fail-closed chain; each check returns allow / deny(reason) /
  clamp(size, reason): paper-gate → kill switches (trading paused, drift) → per-class policy
  (crypto long-only, equity shortability, session open) → stop cooldown (no opens on a
  symbol the stop just closed) → sizing clamps (max position frac,
  notional floor, kelly cap) → concurrency + trades/day → daily-loss + weekly-drawdown
  breakers → approval threshold (large orders park as pending). Unreadable input = deny.
  (Consolidates what v1 scattered across sleeve/translator/tournament;
  [risk-chain-consolidated](lessons.md#risk-chain-consolidated).)
- **Stop-loss** (#74) — deterministic, in the loop, before the model: a held position whose
  loss against its entry reaches `TRADER_RISK_STOP_LOSS_FRAC` (default 8%) on two ticks in a
  row is closed in full, by quantity, through the same RiskGuard chain, journaled with trigger
  `stop_loss`. Pause and drift still block it; a blocked stop retries every tick and journals
  once per outcome. A position with no entry price is journaled as unprotected.
- **Executor** — Alpaca paper only. Idempotent `client_order_id`; persist the order row
  before submit; trade-updates WebSocket → fills ledger. Closes must eventually land; opens
  are best-effort + journaled ([closes-must-land](lessons.md#closes-must-land),
  [crypto-fees-from-asset](lessons.md#crypto-fees-from-asset)).
- **Journal** — every wakeup (including suppressed/shadow) writes a row: trigger, full
  prompt, raw output, risk verdicts, desk verdicts, the Confirm, `macro_view_version`,
  cost, latency. Replay-ready.

### Universe

The seed registry (`core/symbols.py`) lists BTC/USD and ETH/USD (crypto, always open),
SPY (US equity, long-only until shortability earns its own gate), and the commodity ETF
slice GLD / SLV / USO (`COMMODITY_ETF`: long-only by class default, NYSE calendar,
fractionable, Alpaca spells them like equities so they ride the equities data chain —
SIP bars behind the delayed end, IEX quotes — and the same notional day orders). A
symbol is class defaults plus overrides; a new ticker joins `TRADER_LOOP_SYMBOLS` and
`TRADER_SHADOW_SYMBOLS` together and earns its flip in the journal
([per-symbol-shadow](lessons.md#per-symbol-shadow)).

## Operator Research Desk

Lets the operator feed hand-picked macro/market research the trader weighs when deciding.
Design informed by the ai-quoting LLM-wiki audit (borrow the consumption discipline, avoid
the unversioned out-of-repo maintenance).

- **`research_notes`** — immutable markdown notes (title, body, tags, target asset
  classes/symbols, source, status `active|disputed|retired`). **Auto-stale** after a default
  window unless pinned/evergreen. Captured from anywhere via `submit_research_note` (MCP),
  `POST /research/notes` (API), `trader note add` (CLI).
- **`macro_views`** — a **versioned compiled digest**. A Grok "compiler" role recompiles
  (debounced) on note change: a global thesis + per-asset-class sections. Rules (from the
  ai-quoting anti-slop pattern): every claim cites note ids; contradictions preserved as
  `disputed`, never LLM-resolved; **price levels frozen as example-not-current — the pattern
  is the knowledge**; stale/retired notes drop out. Nemotron fallback if xAI is quarantined.
- **Advisory firewall (structural, not just prompt).** The decide prompt receives a budgeted
  Macro View slice in its **own lane** — operator judgment and market facts starve
  independently; entries drop whole, never truncated mid-thought; empty → the block is
  omitted. **RiskGuard never reads the Macro View**: research shapes direction and conviction,
  never sizing or breakers. Every decision journals its `macro_view_version`.
- **Read-only markdown mirror.** Notes + the current view export one-way to a git-tracked
  `research/` folder for Obsidian-style browsing. Edits there do NOT flow back (single owner).
- **Hermes is a capture client, never a dependency.** A Hermes skill can distill a forwarded
  article or voice memo and call `submit_research_note` — but the trader never calls outward,
  and capture works from CLI/API with Hermes absent.
- **Graduation path.** A claim that recurs and is quantifiable ("VIX > 30 → halve crypto
  exposure") graduates from advisory prose into a deterministic, backtestable RiskGuard/config
  parameter and leaves the Desk's advisory role.

## Screener (M5)

`research/screener.py` reduces a **static universe shipped in the repo**
(`data/universe.py`: the S&P 100, source and date cited in the file) to a bounded
shortlist. Data path: one or two Alpaca free-tier multi-symbol daily-bars requests
(`AlpacaData.get_daily_bars_multi`, `feed=sip` with a delayed `end`, `next_page_token`
followed), fetched on each query and never stored — the `bars` cache is not involved
([no-market-data-storage](lessons.md#no-market-data-storage)). The ranker is pure and
deterministic, no LLM: 20d and 60d momentum, 20d annualized realized vol (from
`data/indicators.py`), 20d average dollar volume; the score is risk-adjusted momentum
(mean of the two returns over the vol), dollar volume is a liquidity floor
(`TRADER_SCREENER_MIN_DOLLAR_VOLUME_USD`), the loop's own `TRADER_LOOP_SYMBOLS` are
excluded, ties break on the ticker, and every candidate carries its metrics and a
plain-language reason. It is one query on three doors — `GET /screener`, the
`get_screener` MCP tool, `trader screener run` — and is **not wired into the decide
loop**: a name earns its place in `TRADER_LOOP_SYMBOLS` (shadow first) by the operator's
hand, never by the screener.

## LLM layer

- **Provider registry**: `{role → (provider, model, credential_source)}`. Roles: `decide`
  (`TRADER_DECIDE_*`, else the Nemotron preset), `brief` / `compiler` / `sentiment` (Grok,
  else `TRADER_RESEARCH_*`), with per-role fallback. `confirm` is Jev alone
  (`TRADER_CONFIRM_*`): Jev answers question sets, never a JSON schema, so it is never a
  decide, analyst or research backstop.
- **Providers** (model picker, #102; slice #103): hosted and local model servers stored as
  data (`llm_providers`), added from presets on every surface (`trader provider …`,
  `/providers`, MCP). Keys are Fernet-encrypted with `TRADER_SECRETS_KEY`, write-only
  (`••••last4`), operator key only, audited without the value
  ([ADR 0002](adr/0002-provider-keys-in-postgres.md)). A Provider on a loopback or private
  address is local. A Provider a current Chain uses cannot be removed.
- **Anthropic adapter** (#104, `llm/anthropic.py`): Claude serves any chat Role through the
  official `anthropic` SDK (1.x, built on httpx2), never an OpenAI-compatible shim. System
  → `system`, a schema → `output_config.format` after `anthropic.transform_schema` (bounds
  the API does not take become description hints; callers still validate with pydantic).
  Model lists come from the Models API; a Chain entry with no model gets
  `claude-opus-5-5`. No SDK retries (the Chain falls through), status-only errors, and a
  reply cut off at `max_tokens` or refused is an error, not a half answer. A free-form map
  in a schema (the compiler's `sections`) is closed to `{}` on the wire, so a Claude
  compiler writes the global thesis only.
- **Profiles** (#107): named, renamable combinations of Chains (`llm_profiles`,
  append-only `llm_profile_versions`), one active. Its current version overrides the
  `.env` chain per Role, per call: the process builds ONE registry (`create_app`) that asks
  the Profile store for each call's Chain, so a switch reaches the loop, desk, compiler,
  briefs and replays on their next call with no restart; a Role the Profile leaves out
  keeps `.env`. The Role filter: decide/analyst/critic/brief/compiler take chat Providers,
  sentiment xAI only (x_search), confirm Jev only or an empty Chain (off). Saving a Chain,
  activating and reverting the active Profile run the **Probe** first (`llm/probe.py`): one
  unmetered call per entry in the Role's real contract on a synthetic context; a failing
  first entry refuses the change, a failing fallback saves with a warning
  ([ADR 0001](adr/0001-instant-decide-model-swap.md)). Every journaled decision records
  `profile_version` (NULL on `.env`) and `served_by` (`provider:model`, fallback included).
  Changes are audited with the caller and surface (`api`/`cli`/`desk`/`mcp`). The Nemotron
  thinking guard follows the URL: a Provider at `TRADER_NEMOTRON_BASE_URL` gets it, no
  other does. Surfaces: `trader profile …`, `/profiles`, `/roles`, MCP.
- **Pricing** (#105, `llm/pricing.py`): every (Provider, model) prices from, first hit
  wins: local Provider → $0 (whatever the model; the same open-weight id on a hosted
  Provider is paid); a manual override (`llm_price_overrides`, any surface, audited);
  the Provider's own list (OpenRouter; xAI and Together unconfirmed, not used); LiteLLM's
  public catalog (Provider-prefixed id first); else unknown ($0, logged once). Remote
  sources are fetched at most once a day per URL and a failed fetch keeps the last copy.
  Model lists and `/roles` show each price with its source; Profile-served clients are
  priced at build, so `usage_daily` meters what the picker shows. `.env`-served roles
  keep the `PRICES` table in `llm/usage.py`.
- **Spend caps** (#108, `llm/caps.py`): a daily USD cap per Role (`llm_spend_caps`), apart
  from Profiles, so a switch never lifts one; the critic's default is
  `TRADER_DESK_CRITIC_DAILY_USD` (the old desk budget, now enforced here). Before each
  call the shared registry checks the Role's spend today in `usage_daily`; at the cap its
  paid models (non-zero metered price) sit out the UTC day and a free one later in the
  Chain serves. The first hit a day is logged and audited. With nothing free left the
  Role fails as in an outage (decide: journaled error, transient backoff). Saving a Chain
  returns an estimate (7-day average token volume × the first model's price), and a
  hosted model with no price is refused in a capped Role. Surfaces: `trader cap …`,
  `/caps`, MCP.
- **Nemotron preset** (`TRADER_NEMOTRON_BASE_URL`, default `http://localhost:8009/v1` — a
  vLLM you host): every request MUST send `chat_template_kwargs: {enable_thinking: false}`
  or throughput collapses to ~0.35 tok/s
  ([nemotron-thinking-off](lessons.md#nemotron-thinking-off)); the preset bakes it in and a
  generic URL equal to the preset's gets the same flag, but it is never sent to a vendor
  API. A self-hosted endpoint typically has no auth — that is network trust, so it stays
  on a LAN. A shared instance should keep the decide loop off contention (research roles
  live on Grok partly for this reason).
- **xAI / Grok** via SuperGrok **device-code OAuth** (RFC 8628, `auth.x.ai`): persist the
  rotating refresh token BEFORE serving the access token (single-use), lock-serialized
  refresh, 403 = entitlement (quarantine, no retry). `XAI_API_KEY` is a first-class fallback.
  Endpoint surface settled by a spike (`/v1/responses` hosts x_search regardless).
- **Usage metering**: real token counts surfaced from every provider (vLLM returns them),
  priced per model; budget gates see actual spend. (v1 recorded `None` locally → budgets
  always read $0.)

## Data layer

- **Alpaca Data** primary (crypto + US equities, free with broker keys), **Twelve Data**
  fallback, **FRED** for macro series (VIX etc.). REST for the engine; providers' MCP servers
  are for agent research only, never the hot path.
- **Indicators** computed locally from fetched bars (RSI/SMA/EMA/realized-vol) — ~30 lines of
  numpy, no network. (v1 fetched these over MCP and got null for 987/987 decisions.)
- **`bars` cache**: immutable `(symbol, interval, ts) → OHLCV`, read-through, MB-scale,
  bounded retention. Not a live-path dependency; for replay/screener only.

## Surfaces (agentic-first)

`surfaces/services.py` is the single registry of commands (state-changing) and queries
(read-only). API routes, MCP tools, and CLI subcommands are thin adapters over it — one
capability, defined once, three doors.

- **REST API** (FastAPI): mandatory API-key auth; startup fails without a key unless
  `AUTH_DISABLED=true` (explicit dev opt-out). (v1 silently disabled auth when the var was
  unset.)
- **MCP server** (FastMCP, streamable HTTP at `/mcp`): ~19 tools generated from the service
  registry. Deliberately **no raw place-order tool** — orders exist only through the
  RiskGuarded loop. Tools: health, portfolio, positions, decisions, wake_trader,
  pause/resume, risk status, resume_breaker, approvals (list/approve/deny), briefs, sentiment,
  bars, usage, research (submit/list/retire note, get_macro_view), flatten_all (confirm-gated).
- **CLI** (typer, `trader ...`): wraps the same services over the API for operator use.

## Safety invariants (summary)

- Live client construction refuses unless `ALPACA_LIVE_ENABLED=true` (default absent).
- No order path exists outside RiskGuard; unreadable risk input = deny, never allow.
- Breaker + pause state is DB-backed and survives restart.
- `audit_events` is append-only (no UPDATE/DELETE in code).
- API auth mandatory outside explicit dev mode.
- Blocking I/O (broker/data fetches) runs off the event loop so `/health` stays responsive
  ([sync-tick-blocked-health](lessons.md#sync-tick-blocked-health) — a sync tick blocked the loop
  and autoheal restart-looped the container).

## Deployment

Two supported shapes, one runbook ([`docs/runbooks/deploy.md`](runbooks/deploy.md)):
**compose** — Postgres 16 + the published image `ghcr.io/sudotomas/trader` (reference,
`infra/docker-compose.yml`) — or the API as a **host systemd user unit** over the compose
db (`infra/systemd/` templates). Configuration is entirely `.env`; the decide model is
whatever `TRADER_DECIDE_*` names, or a self-hosted Nemotron at `TRADER_NEMOTRON_BASE_URL`.
The machine the project was developed on (a GPU box sharing memory with vLLM, hence the
compose memory cap) is an appendix, [`docs/deploy/dgx-spark.md`](deploy/dgx-spark.md),
not a requirement.

## Milestones

M0 Foundation (scaffold, CI, service layer + 3 surfaces, store + auth) · M1 Market data ·
M2 LLM layer · M3 Money path crypto (shadow → gated live-paper) · M4 Research (Desk + briefs
+ sentiment) · M5 Expansion (equities, commodity ETFs, screener, desk, replay eval, IBKR
add-back). Tracked on GitHub: [sudoTomas/trader/issues](https://github.com/sudoTomas/trader/issues)
([`docs/tracker.md`](tracker.md)).
