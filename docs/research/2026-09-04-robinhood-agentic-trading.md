# Spike: Robinhood Agentic Trading (MCP) — primary-source findings and broker-fit assessment

**Question:** Robinhood shipped "agentic trading" with MCP tools. What exactly is it, is there a
public integration surface, does it have a paper environment, and could it serve this system as
(a) a second broker, (b) a market-data provider, or (c) a research-only MCP source?

**Method:** Primary sources only — Robinhood newsroom, official support articles, official product
pages, SEC filing, official docs portal — plus a direct unauthenticated probe of the live MCP
endpoint (2026-09-04) to verify the auth model first-hand. Tech-press was used only to locate
first-party pages. Where an official source says nothing, that absence is called out explicitly.

Researched: 2026-09-04.

---

## 1. What Robinhood shipped

- **Product:** "Agentic Trading" (alongside a sibling "Agentic Credit Card"). Launched
  **May 27, 2026**, in **beta**, equities-only at launch; not a September launch — September-era
  coverage reflects the crypto rollout completing, not a new product.
  Source: [Robinhood Newsroom — "Robinhood is Now Open to Agents"](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/)
- **What it concretely is:** a hosted **remote MCP server** operated by Robinhood
  (`https://agent.robinhood.com/mcp/trading`) that a customer connects their *own* third-party AI
  agent to (Claude Code, Claude Desktop, ChatGPT, Codex, Cursor, Grok, "other MCP-compatible AI
  platforms"). It is **not** an in-app Robinhood agent and **not** a conventional REST trading API.
  The agent trades a **dedicated real-money "Agentic account"**, separate from the customer's main
  portfolio (counts as one of up to 10 self-directed individual accounts).
  Source: [Support — Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- The newsroom post describes "AI-native Model Context Protocol (MCP) servers for fast, seamless
  integration" and the product page's pitch is "Paste one URL into your MCP config to connect most
  agents out of the box."
  Sources: [newsroom post](https://robinhood.com/us/en/newsroom/robinhood-is-now-open-to-agents/),
  [product page](https://robinhood.com/us/en/agentic-trading/)
- **Asset coverage today:** equities, options, and crypto. Launch was equities-only; the July 1,
  2026 "World is Flat" keynote announced the crypto rollout ("Agentic Trading for Crypto will begin
  rolling out soon to eligible US traders", "no additional cost"). Robinhood's Q2 2026 8-K exhibit
  confirms: "customers are now able to trade equities, options, and crypto through AI-powered
  agents" and "nearly 100 thousand customers have opened Agentic Trading accounts, with over
  $100 million in AUC."
  Sources: [Newsroom — global expansion post](https://robinhood.com/us/en/newsroom/robinhood-accelerates-global-expansion-robinhood-chain-mainnet-stock-tokens-agentic-trading/),
  [SEC 8-K Q2 2026 exhibit 99.1](https://www.sec.gov/Archives/edgar/data/0001783879/000178387926000113/q22026robinhoodexhibit991.htm)
- A second MCP server exists for the credit-card side (`https://banking-agent.robinhood.com/mcp/banking`),
  not relevant here.
  Source: [Support — Agentic Credit Card](https://robinhood.com/us/en/support/articles/agentic-credit-card/)

## 2. Integration surface

### Endpoint and auth (verified by direct probe, 2026-09-04)

Unauthenticated `POST https://agent.robinhood.com/mcp/trading` (MCP `initialize`) returns
`401` with:

```
www-authenticate: Bearer resource_metadata="https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"
```

The published OAuth metadata (fetched live):

- `authorization_endpoint`: `https://robinhood.com/oauth` (interactive Robinhood login)
- `token_endpoint`: `https://api.robinhood.com/oauth2/token/`
- `registration_endpoint`: `https://agent.robinhood.com/oauth/trading/register` — **dynamic client
  registration (RFC 7591) is open**, i.e. any MCP client can self-register; there is no partner
  program or API-key issuance
- `grant_types_supported`: `authorization_code`, `refresh_token`; PKCE `S256`;
  `token_endpoint_auth_methods_supported`: `none` (public clients)
- `scopes_supported`: `["internal"]` — a single opaque scope; **no granular read-only scope is
  published**

So: **standard MCP OAuth for retail customers** — no API keys, no developer signup, but the
authorization step is an interactive browser login ("You can only open an agentic account and
authenticate your agent on a desktop device", per the overview article), with onboarding that
creates and funds the Agentic account. Eligibility: a primary individual Robinhood investing
account in good standing, US customers; crypto additionally requires a Robinhood Crypto account
and is unavailable in some states (e.g. New York).
Source: [Support — Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/); probe output above (primary observation).

### Tools / capabilities exposed

Per the official [Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
support article, the agent gets:

- **Account/portfolio:** accounts, portfolio snapshots, realized P&L, trade history
- **Market data:** real-time quotes, OHLCV bars, technical indicators (RSI, MACD, Bollinger
  Bands), fundamentals/financials/earnings, Level 2 order book, index data
- **Watchlists:** create/update, add/remove symbols
- **Execution:** place, review (simulate a specific order pre-submission), and cancel orders —
  "long equities, options, and crypto orders" only; margin borrowing "not yet enabled"; agent
  cannot transfer, stake, or lend crypto; agent can only trade the Agentic account
- Read access covers "all account details", positions, balances, full transaction and order
  history ([overview article](https://robinhood.com/us/en/support/articles/agentic-trading-overview/))

**Not found in official sources:** a formal tool-by-tool API reference, machine-readable schema,
rate limits, idempotency semantics, or an order-event stream. There is no public developer portal
for the trading MCP (the support articles *are* the documentation), and **no official GitHub
repo** — a GitHub search finds no MCP repos under the `robinhood` org, only community projects.

### Pricing and terms

- "No additional cost" for Agentic Trading; standard brokerage fee schedule applies.
  Sources: [expansion newsroom post](https://robinhood.com/us/en/newsroom/robinhood-accelerates-global-expansion-robinhood-chain-mainnet-stock-tokens-agentic-trading/),
  [Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
- Automated trading by the connected agent is explicitly sanctioned — "if you've asked your agent
  to take action without asking your approval, it can place trades without your confirmation" —
  with responsibility on the customer: "You are ultimately responsible for the trades your AI
  agent places." Onboarding presents updated customer agreements plus "agentic disclosures"
  (crypto requires accepting an updated [Robinhood Crypto customer agreement](https://robinhood.com/us/en/crypto-customer-agreement)).
  Sources: [Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/),
  [overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- Guardrails are account-scoped, not API-scoped: dedicated budget in a separate account,
  per-trade notifications, instant disconnect in-app.
  Source: [product page](https://robinhood.com/us/en/agentic-trading/)

## 3. Paper trading — the deciding question

**No. Robinhood has no paper/simulated trading environment, and the agentic surface does not add
one.**

- No official Robinhood source — newsroom, product page, all three support articles, the docs
  portal — mentions a paper, demo, sandbox, or simulated-account environment. (Explicit absence:
  I searched `site:robinhood.com` and read each page for it.)
- The Agentic account is funded with real money as part of onboarding ("deciding exactly how much
  capital to allocate" — [expansion post](https://robinhood.com/us/en/newsroom/robinhood-accelerates-global-expansion-robinhood-chain-mainnet-stock-tokens-agentic-trading/));
  the risk disclosure is "possible loss of your entire investment"
  ([product page](https://robinhood.com/us/en/agentic-trading/)).
- The closest official features are pre-trade tools: the MCP "review" tool simulates a specific
  order before placing it, and the app's "[Simulated Returns](https://robinhood.com/us/en/support/articles/simulated-returns/)"
  visualizes hypothetical option-position outcomes. Neither is an executable paper venue — no
  virtual balance, no simulated fills, no order lifecycle.
- The pre-existing [Robinhood Crypto trading API announcement](https://robinhood.com/us/en/newsroom/robinhood-crypto-trading-api/)
  likewise mentions no sandbox.

## 4. Crypto vs equities; relation to the existing Crypto API

- The **Robinhood Crypto Trading API** (launched **May 30, 2024**) is a separate, pre-existing
  surface: REST, **API keys** self-issued via a credentials portal ("Only customers with active
  Robinhood Crypto accounts will be able to gain access and create their API keys"), crypto only —
  market data, portfolio/account, order placement. Docs: [docs.robinhood.com/crypto/trading](https://docs.robinhood.com/crypto/trading/)
  (JS-rendered SPA; rate-limit specifics live there but could not be captured in this spike —
  flagged as unverified rather than quoted from memory).
  Source: [newsroom announcement](https://robinhood.com/us/en/newsroom/robinhood-crypto-trading-api/)
- The MCP surface is **not** an evolution of that API: different auth (OAuth vs API keys),
  different account (dedicated Agentic account vs the main crypto account), different asset scope
  (equities+options+crypto vs crypto only). The Crypto API remains the only conventional REST
  order API Robinhood offers; **equities still have no public REST API** — the MCP server is the
  only sanctioned programmatic path to equities, and it is conversational-agent-shaped.

---

## 5. Fit assessment for this system

Context: `src/trader/brokers/base.py` (Broker protocol: account/positions/order-by-client-id/
submit with `client_order_id` idempotency/cancel/close + `stream_trade_updates()`),
`src/trader/brokers/alpaca.py` (paper-only guard, REST + binary trade-updates WS, 200 req/min),
`docs/architecture.md` invariants: **PAPER ONLY today**, no order path outside deterministic
RiskGuard, provider MCP servers are research-only and never in the decide hot path, fetch-on-tick
REST market data, indicators computed locally.

### (a) Second broker behind the Broker protocol — **blocked outright**

1. **No paper environment** (§3). The system is PAPER ONLY; the Agentic account is real money
   from the first fill. This alone disqualifies it today, exactly as the task anticipated.
2. **No idempotency contract.** The protocol is built around `client_order_id` as the idempotency
   key with `DuplicateOrder` semantics (Alpaca's 422). Nothing in Robinhood's official material
   documents client-supplied order IDs or duplicate-submit behavior on the MCP tools. Unknown —
   and unknowable without connecting a funded real-money account.
3. **No order-event stream.** `stream_trade_updates()` needs push events; MCP here is
   request/response tools plus in-app push notifications to the human. Fills would have to be
   polled. Not documented otherwise.
4. **Interactive OAuth, headless-hostile.** Browser login on desktop plus mobile-app verification
   to mint tokens; refresh tokens exist (per the AS metadata) but token lifetime/revocation
   behavior is undocumented. An unattended daemon may need periodic human re-auth.
5. **No documented rate limits or SLAs** for the MCP surface at all.

Even if Robinhood ships a paper mode later, points 2–5 mean an adapter would be built against
undocumented behavior — the opposite of the Alpaca spike that grounds `alpaca.py`.

### (b) Market-data provider — **poor fit, redundant**

The MCP exposes quotes/OHLCV/indicators/L2, but only through an authenticated MCP session tied to
a funded real-money Agentic account, and the architecture mandates REST fetch-on-tick with
**no MCP in the hot path** and locally computed indicators (the v1 987/987-null lesson). Alpaca +
existing providers already cover this over plain REST. The separate Crypto API offers REST crypto
market data with API keys and no funded-agentic-account prerequisite — the only defensible
data-shaped use, and still redundant for current needs.

### (c) Research-only MCP source — **the only plausible slot, with a live-fire caveat**

Architecture explicitly allows provider MCP servers for agent research. Robinhood's fundamentals/
earnings/L2/indicator tools would slot there mechanically. Two problems:

1. **Access requires creating (and, per onboarding flow, funding) a real-money Agentic account.**
   Whether one can connect with a zero balance is not documented — unverified.
2. **The session carries live order-placement tools with a single `internal` scope** — no
   read-only scope exists. Handing that session to a research agent violates the spirit of
   "research only, never places orders" unless it sits behind a tool-filtering MCP proxy that
   whitelists read-only tools. That proxy is buildable but is new safety-critical surface for
   marginal data gain.

## Verdict

Robinhood Agentic Trading is real, retail-accessible, and technically open (hosted MCP + standard
OAuth with open dynamic client registration — verified by live probe), but it is a
**consumer product for supervised real-money agents, not a broker API**: no paper environment
anywhere at Robinhood, no idempotency or streaming contract, no rate-limit or tool-reference
documentation, no developer portal, no official SDK/repo.

- **(a) Second broker: NO** — no paper environment is a hard block against the PAPER ONLY
  invariant, with the missing idempotency/streaming contracts as independent blockers behind it.
- **(b) Market data: NO** — MCP-only surface conflicts with the fetch-on-tick/no-MCP-hot-path
  invariants and duplicates existing providers.
- **(c) Research MCP: MAYBE, not now** — viable only behind a read-only tool-filtering proxy and
  only if a zero-funding connection proves possible; not worth it for the data on offer today.

**Recommendation: no action.** Revisit only if Robinhood ships a sandbox/paper mode or a
documented REST trading API for equities. The cheap watch-item is their support hub
([robinhood.com/us/en/support/agentic-trading](https://robinhood.com/us/en/support/agentic-trading/))
and newsroom for a "sandbox"/"developer" announcement.
