# Twelve Data vs Alpaca Market Data — data-backbone comparison

**Date:** 2026-09-08
**Question:** Operator pays for Twelve Data **Grow** ($79/mo) but the key is not wired in; the
live system runs entirely on Alpaca's **free** data tier. Cancel TD and pay Alpaca? Keep both?
Broker is Alpaca regardless — this is purely about DATA.
**Method:** Primary sources only (official pricing pages, docs, support KB, MCP repos, status
pages), fetched 2026-09-08. Every claim cited. Gaps listed in §6.

System requirements (from `src/trader/data/{alpaca,twelve_data}.py` and
`docs/architecture.md` §Data layer): ETH/USD + SPY hourly bars ×100 on every wakeup
(~60/day/symbol), latest quotes ~1/min/symbol during open hours, indicators local, bursty
weeks-to-months hourly backfills for replay, backlog = commodity ETFs (GLD/SLV/USO), a
full-US-equity screener to a bounded shortlist per cycle, macro series (VIX etc., today via
keyless FRED).

---

## 1. Twelve Data — facts

### 1.1 Pricing tiers ([twelvedata.com/pricing](https://twelvedata.com/pricing))

| Tier | Price | API credits/min | Daily cap | WebSocket | Markets |
|---|---|---|---|---|---|
| Basic | $0 | 8 | 800/day | 8 **trial** credits | 3 (US stocks, forex, crypto) |
| **Grow** | **$79/mo ($66/mo annual)** | **377** | none | **8 trial credits only** | "20+ markets": real-time US stocks, global **EOD** equities/ETFs, commodities, fundamentals |
| Pro | $229/mo ($191 annual) | 1,597 | none | 1,500 credits | 70+ markets, real-time EU |
| Ultra | $999/mo ($832 annual) | 10,946 | none | 10,000 credits | all 84 markets, **99.95% SLA** |

SLA appears **only** on Ultra; Grow lists none. (Support articles still reference a legacy
"Pro 610" plan at $99 with 610 credits + 500 WS credits — not on the current pricing page.)

### 1.2 Credits & rate-limit behavior

- `/time_series` = **1 credit per symbol**; batch requests scale linearly (3 symbols = 3
  credits). Heavier endpoints exist (e.g. `/income_statement` = 100 credits/symbol). Credits
  **reset every minute**. ([support: Credits](https://support.twelvedata.com/en/articles/5615854-credits))
- Exceeding the per-minute quota returns **HTTP 429** ("You have run out of API credits");
  quota restores at the start of the next minute. ([support: API credits & limits](https://support.twelvedata.com/en/articles/5194820-api-credits-limits), [support: Credits](https://support.twelvedata.com/en/articles/5615854-credits))
  No `Retry-After` header documented (§6).
- Note (adapter-hardened, v1): TD also reports request errors as HTTP 200 +
  `{"status":"error"}` — already handled in `twelve_data.py`.

### 1.3 US equities data quality — the headline finding

From the official support article [US equities market data](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data):

> real-time "pricing data is sourced from venues that do not require additional licensing.
> These venues collectively represent **approximately 5% of total US trading volume**."

- Consolidated full-market real-time = "contact our team to arrange licensed access" (custom,
  not Grow, no listed price).
- Historical/EOD data **does** cover "100% of total US trading volume, aggregated from every
  stock exchange" — but becomes available **after midnight ET the following trading day**.
- Real-time US stocks are included on **every** plan (Basic upward), so Grow's real-time
  equities are the same ~5% feed the free tier gets.

**Consequence:** TD Grow does NOT buy consolidated intraday equities. Today's SPY hourly bars
come from a ~5%-of-volume feed — the same coverage class as Alpaca's free IEX feed. Yesterday
and older is consolidated on both providers (see §2.3 for Alpaca's version).

### 1.4 History depth & pagination

- Max **5,000 data points per request** (`outputsize` 1–5000); deeper ranges via
  `start_date`/`end_date` windowing (no cursor token; omit `outputsize` when using both
  dates). ([support: Getting historical data](https://support.twelvedata.com/en/articles/5214728-getting-historical-data), [support: How to get historical prices](https://support.twelvedata.com/en/articles/5656039-how-to-get-historical-prices))
- 5,000 hourly bars ≈ 208 days of 24/7 crypto or ≈ 2.5 years of RTH-only SPY — a
  weeks-to-months replay backfill is **1–2 credits per symbol**. Per-interval maximum depth
  (how far back intraday goes) is not stated in official docs (§6).

### 1.5 WebSocket

Real WebSocket access starts at the **Pro** plan; Basic and Grow get trial-only (8 trial
credits, specific test symbols). Up to 3 concurrent connections; `/quotes/price` = 1 WS
credit per subscribed symbol; Pro-class ≈ up to 1,500 symbols, Ultra up to 10,000.
([support: WebSocket FAQ](https://support.twelvedata.com/en/articles/5194610-websocket-faq), [support: Trial](https://support.twelvedata.com/en/articles/5335783-trial), [pricing](https://twelvedata.com/pricing))
**On Grow, streaming is effectively unavailable** — polling-replacement is a Pro ($229)
feature on TD.

### 1.6 Indices / macro

- TD markets "5,000+ worldwide indices" ([twelvedata.com/indices](https://twelvedata.com/indices)), but the **VIX index itself could not be confirmed** in any official source:
  TD's own public `symbol_search?symbol=VIX` returns only VIX-themed ETFs/ETNs (checked
  2026-09-08 against `api.twelvedata.com`), and no official page shows the CBOE VIX index as
  an available symbol (§6).
- **No FRED-style macro/economic series** (rates, CPI, etc.) appear anywhere in TD's official
  product/docs pages — their catalog is stocks/forex/crypto/ETF/indices/fundamentals.
  ([twelvedata.com/market-data](https://twelvedata.com/market-data))

### 1.7 MCP server

**Official, and hosted.** [github.com/twelvedata/mcp](https://github.com/twelvedata/mcp):

- Cloud server at `https://mcp.twelvedata.com/mcp` — nothing to install; **OAuth** browser
  login links your TD account/API key automatically. Local self-run (Python 3.10+, API key in
  `.env`) also supported.
- Tools: real-time + historical prices (stocks/ETF/forex/crypto/commodities), 60+ technical
  indicators, fundamentals (statements, earnings, dividends, IPO calendar), ETF/fund
  profiles, news, movers, SEC filings, insider/institutional data.
- **Read-only** — no trading surface at all.
- Whether MCP calls draw from the same per-minute credit quota is not documented (§6).

### 1.8 Reliability/ops

- Status page: [twelvedata.statuspage.io](https://twelvedata.statuspage.io/) (90-day uptime
  history, incident feed).
- SLA: **99.95% on Ultra only** ([pricing](https://twelvedata.com/pricing)); no SLA commitment
  published for Grow.

---

## 2. Alpaca Market Data — facts

### 2.1 Plans ([alpaca.markets/data](https://alpaca.markets/data), [docs: About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api))

| | **Basic (free)** | **Algo Trader Plus** |
|---|---|---|
| Price | $0 | **$99/mo** |
| Real-time equities feed | **IEX only** | **All US exchanges (SIP)** — CTA+UTP, "100% of market volume" |
| Historical equities | "7+ years" / "Since 2016" | same |
| API rate limit | **200 calls/min** | **10,000 calls/min** (docs; pricing page says "unlimited") |
| WebSocket | **30 symbols**, IEX feed | **Unlimited symbols**, SIP feed |
| Crypto | included (Alpaca's own venue) | included |
| Options | indicative feed | real-time OPRA |

### 2.2 Rate-limit behavior

Exceeding the limit returns **HTTP 429 "Too Many Requests"**; limits are per-account and not
self-serve increasable below ATP. ([support: usage limit](https://alpaca.markets/support/usage-limit-api-calls))
No `Retry-After` header documented for market data (§6).

### 2.3 The free-SIP loophole (decisive for this system)

> "For historical queries, the `end` parameter must be at least **15 minutes old** to query
> SIP data without a subscription." ([docs: Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq))

The stock-bars endpoint defaults to `feed=sip` and accepts `feed=iex|sip|otc|boats`
([reference: stockbars](https://docs.alpaca.markets/us/reference/stockbars)). So the **free**
tier already gets **consolidated, full-volume hourly bars** for SPY — the only restriction is
that the query window must end ≥15 minutes ago. The current adapter pins `feed=iex`
(`alpaca.py:89`); for hourly bars this leaves consolidated data on the table.

### 2.4 Bars endpoints, batching, pagination

- Stocks: `symbols` is comma-separated multi-symbol; `limit` up to **10,000 data points per
  response counted across all symbols**; `next_page_token`/`page_token` pagination.
  ([reference: stockbars](https://docs.alpaca.markets/us/reference/stockbars)) The adapter
  already handles the pagination trap (`alpaca.py:94-101`, EAI-190).
- Crypto: v1beta3, Alpaca's own venue, quote-mid-price bars, `limit` up to 10,000, same
  pagination; free on both plans. ([docs: Historical Crypto Data](https://docs.alpaca.markets/us/docs/historical-crypto-data-1), [reference: cryptobars](https://docs.alpaca.markets/reference/cryptobars-1))
- Crypto historical **depth** is not stated officially; docs examples query from mid-2022,
  and crypto *quotes* on the `us-1` location only go back to 2025-10-14 (§6).

### 2.5 Indices / macro

Alpaca's historical API covers exactly four categories: **stocks, crypto, options, news**
([docs: Historical API](https://docs.alpaca.markets/us/docs/historical-api)). **No index data
(no VIX, no SPX) and no macro/economic series** — confirmed absent from the official docs
tree. FRED stays the macro source either way.

### 2.6 MCP server

**Official, self-hosted only.** [github.com/alpacahq/alpaca-mcp-server](https://github.com/alpacahq/alpaca-mcp-server):

- "Alpaca does not provide a hosted remote MCP server" — run it yourself; stdio (default) or
  streamable-http transports. Auth via `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` env vars.
- ~80+ tools: market data (stock/crypto/option bars, quotes, trades, snapshots, greeks,
  orderbooks, corporate actions, news, calendar) **plus full trading** — order placement
  (market/limit/stop/trailing, multi-leg options), position management, account ops.
- Toolsets are filterable via `ALPACA_TOOLSETS` — relevant here, since the research desk must
  never get an order-placement tool (architecture invariant: orders only through RiskGuard).
- Active: v2 is a recent FastMCP/OpenAPI rewrite; v1 deprecated and incompatible.

### 2.7 Reliability/ops

- Status page: [status.alpaca.markets](https://status.alpaca.markets/) — per-component
  monitoring incl. stocks/crypto/options REST and every websocket feed (SIP, IEX, crypto),
  90-day uptime shown (currently 100.0% on data components, 99.99% Broker API).
- **No published SLA** on any data tier.

---

## 3. Requirement-by-requirement comparison

| Requirement | Twelve Data **Grow** ($79) | Alpaca **free** ($0) | Alpaca **ATP** ($99) |
|---|---|---|---|
| **ETH/USD hourly bars** | Yes, aggregated crypto, 1 credit/call | Yes, own-venue, free, same keys as broker | same as free |
| **SPY hourly bars (intraday)** | ~**5% of US volume** feed; consolidated only next day ([TD support](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data)) | IEX real-time; **consolidated SIP when window ends ≥15 min ago** ([FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)) — covers hourly-bar wakeups | Consolidated SIP, real-time |
| **Latest quote ~1/min** | `/quote` = last price both sides (no book on this plan; adapter `twelve_data.py:106`) | Real IEX bid/ask, real-time | Real SIP NBBO |
| **Replay backfills** | 5,000 pts/request, date-windowed; 100%-volume historical; depth per interval undocumented | 10,000 pts/request, `page_token`; equities **since 2016**; SIP historical usable free (15-min rule) | same, 10k req/min headroom |
| **Screener (~5,000 symbols/cycle)** | 1 credit/symbol even batched → **377 symbols/min** → ~13+ min per full scan; feasible, slow | Multi-symbol bars/snapshots, 10k points/response, 200 req/min → **full-universe scan in a handful of requests** | 10,000 req/min — trivial |
| **Commodity ETFs (GLD/SLV/USO)** | Yes (US-listed ETFs; also raw commodities on Grow) | Yes (they're US equities) | Yes |
| **Macro (VIX, rates)** | **No macro series; VIX index unconfirmed** (only VIX ETFs in their own catalog) | **None** (stocks/crypto/options/news only) | None |
| **WebSocket (future)** | **Trial-only on Grow** — real WS starts at Pro $229 | 30 symbols (IEX + crypto) — enough for 2–5 symbols today | Unlimited, SIP |
| **MCP research tools** | **Hosted**, OAuth, read-only, indicators+fundamentals+news | Self-run, 80+ tools incl. trading (must filter toolsets) | same |
| **429 behavior** | 429, per-minute reset documented | 429 documented | 429 documented |
| **SLA / status** | statuspage.io; SLA only on Ultra | status.alpaca.markets; no SLA | no SLA |

---

## 4. Costed scenarios (monthly)

| # | Scenario | Data cost | What you get / give up |
|---|---|---|---|
| **A** | **Cancel TD Grow; Alpaca free only** (+ keep TD **Basic $0** key wired as fallback) | **$0** (saves $79/mo) | Everything the live system needs today: free crypto, IEX quotes, **consolidated SIP hourly bars via the 15-min rule**, screener feasible inside 200 req/min via multi-symbol requests. Fallback duty at TD Basic = 8 credits/min, 800/day — enough for outage bridging of 2–5 symbols, not for screening. |
| **B** | **Cancel TD; buy Alpaca Algo Trader Plus** | **$99** (+$20 vs today) | Real-time consolidated SIP quotes+bars, 10,000 req/min, unlimited WS symbols (polling→streaming path), OPRA options. Still zero macro/indices. |
| **C** | **Keep both: Alpaca free + TD Grow** (status quo, but actually wire the key) | **$79** ($66 annual) | Redundancy + commodities/global EOD + fundamentals + hosted MCP. But the $79 buys a **~5%-volume equities feed with no WebSocket** — hot-path-wise it duplicates what Alpaca gives free, and its screener ceiling (377 sym/min) is *worse* than Alpaca free's batching. |
| **D** | Alpaca ATP + TD Grow | **$178** | Over-buying; TD adds nothing ATP lacks except commodities spot/fundamentals and the hosted MCP. |

---

## 5. Verdict (weighting: hot-path reliability crypto+equities > screener > macro > MCP; integration effort ignored)

1. **A — cancel Twelve Data, run Alpaca free ($0).** The live hot path (ETH/USD + SPY hourly
   ×100, quotes 1/min) fits comfortably in 200 req/min (~4 req/wakeup + 2 quotes/min ≪ limit),
   crypto is native, and the 15-minute SIP rule means completed hourly bars are
   **consolidated-quality for free** — strictly better equity bars than TD Grow's ~5% intraday
   feed. The screener backlog is *more* feasible on Alpaca free (multi-symbol batching) than on
   the plan currently being paid for. Downgrade TD to the free Basic key for composite-fallback
   duty. Action item that falls out: switch `alpaca.py` to `feed=sip` for bar requests with
   `end ≤ now−15min`.
2. **B — Algo Trader Plus ($99) when it's earned.** Buy it the day either (a) real-time NBBO
   quote quality starts driving fills/decisions, or (b) the screener goes to production and
   wants sub-minute full-universe scans + streaming. It dominates TD Grow on every weighted
   axis for $20 more than the current TD spend.
3. **C — keep TD Grow ($79): not justified.** Its two genuine exclusives — commodities spot
   data and a hosted read-only MCP for the research desk — sit at the bottom of the weighting.
   It fails the top-weighted axis (intraday equities = ~5% feed, no WS on Grow) and the
   second (377 symbols/min screener ceiling). VIX/macro, the one gap it might have filled,
   it doesn't fill.
4. **D — both paid ($178): dominated** by B.

**Bottom line:** the $79 Grow subscription buys nothing this system's weighting values above
what Alpaca gives away free with the broker keys it already requires. Cancel it; bank the $79;
spend $99 on ATP later if/when quotes-or-screener demand it.

---

## 6. Not found in official sources

- **TD:** VIX index availability (own catalog shows only VIX ETFs; `/indices` reference
  endpoint returned empty without a key — inconclusive); per-interval intraday history depth;
  whether MCP usage consumes API credits; `Retry-After` header on 429; any SLA for Grow;
  intraday-bar latency figures.
- **Alpaca:** crypto bars historical depth (examples from 2022; `us-1` crypto *quotes* only
  since 2025-10-14); IEX's exact %-of-consolidated-volume (docs contrast "IEX" vs "100% of
  market volume" via CTA/UTP but give no IEX percentage — the ~2-3% figure is from IEX
  exchange statistics, not Alpaca docs); `Retry-After`/`X-RateLimit-*` headers for the data
  API; max symbols per multi-symbol request (only the 10,000-data-point response cap is
  documented); any SLA.
- **Both:** documented bar-publication latency (how soon after the hour a completed hourly
  bar is queryable).

## Sources

Twelve Data: [pricing](https://twelvedata.com/pricing) · [US equities market data](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data) · [Credits](https://support.twelvedata.com/en/articles/5615854-credits) · [API credits & limits](https://support.twelvedata.com/en/articles/5194820-api-credits-limits) · [Getting historical data](https://support.twelvedata.com/en/articles/5214728-getting-historical-data) · [How to get historical prices](https://support.twelvedata.com/en/articles/5656039-how-to-get-historical-prices) · [WebSocket FAQ](https://support.twelvedata.com/en/articles/5194610-websocket-faq) · [Trial](https://support.twelvedata.com/en/articles/5335783-trial) · [indices page](https://twelvedata.com/indices) · [MCP repo](https://github.com/twelvedata/mcp) · [statuspage](https://twelvedata.statuspage.io/) · live `symbol_search`/`indices` probes against `api.twelvedata.com` (2026-09-08).

Alpaca: [data plans](https://alpaca.markets/data) · [About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api) · [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) · [stockbars reference](https://docs.alpaca.markets/us/reference/stockbars) · [cryptobars reference](https://docs.alpaca.markets/reference/cryptobars-1) · [Historical API](https://docs.alpaca.markets/us/docs/historical-api) · [Historical Crypto Data](https://docs.alpaca.markets/us/docs/historical-crypto-data-1) · [usage-limit support article](https://alpaca.markets/support/usage-limit-api-calls) · [MCP repo](https://github.com/alpacahq/alpaca-mcp-server) · [status page](https://status.alpaca.markets/).

---

## Operator addendum (2026-09-08, post-research empirical checks)

**Tier correction**: the operator's actual Twelve Data subscription is a ~$30/mo hobbyist
tier, not Grow ($79) — scenario savings adjust accordingly; the ranking logic is
unchanged (the credit-per-symbol screener math and ~5%-of-volume equities feed apply to
all standard tiers per TD's KB).

**Live three-way test** (same 4-day SPY hourly window, run on our keys):
- Alpaca free `feed=sip` historical (window ending ≥20 min back): **16 bars** — works
  on the free tier as documented.
- Alpaca free `feed=iex` (what the adapter pins today): **8 bars** — IEX doesn't just
  carry 3.5% of the volume (measured), it *omits entire hourly bars* when IEX trading
  is thin. Bar gaps, not price error, are the real IEX cost (max close diff vs SIP:
  0.07%).
- Twelve Data (operator key): 60 bars served fine, **but hourly bars align to 9:30 ET
  (:30 stamps) vs Alpaca's :00** — a provider fallback mid-stream shifts bar alignment,
  so mixed-provider indicator continuity is imperfect by construction.

**Actions taken**: TD key wired into `.env` (equity chain is now `alpaca_data →
twelve_data`); fallback exercised through the composite with a simulated primary outage
— PASS (30 bars + quote served by TD). Follow-up candidate: switch adapter historical
bars to `feed=sip` with a ≥16-min-delayed `end` (free consolidated bars; needs care —
the price the decider sees currently comes from the last bar, so a delayed end must not
stale the mark).
