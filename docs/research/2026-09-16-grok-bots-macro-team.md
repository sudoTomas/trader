# Grok "bots" for a macro research team — product truth + integration design

**Date:** 2026-09-16 · **Status:** accepted 2026-09-17 (operator)
**Question:** the operator wants a team of xAI "Grok bot" agents collaborating on macro
research, feeding the trader's advisory-firewalled Operator Research Desk
(`POST /research/notes`). What does xAI actually ship, and what is the right integration?

**Sources:** primary only (x.ai announcements, docs.x.ai, cursor.com/help for Grok Bot
billing — the official billing surface), fetched 2026-09-16. One secondary source is
flagged as such. Branding note: x.ai pages currently present under "SpaceXAI" branding;
product names (Grok, Grok Bot, Grok API) are unchanged and this doc uses "xAI".

---

## 1. Product truth: what xAI ships (September 2026)

The operator's phrase "Grok bot" maps to a real product. xAI ships **three distinct
agent surfaces** — do not conflate them:

### 1a. Grok Bot — hosted persistent agents (beta, launched 2026-08-11)

A hosted "always-on AI teammates" product: named bots with persistent memory, each
account gets a **shared cloud computer** (browser, command line, files in `/workspace`)
that bots use 24/7 without the user's machine being on.
([x.ai/news/introducing-grok-bot](https://x.ai/news/introducing-grok-bot),
[docs.x.ai/grok-bot/overview](https://docs.x.ai/grok-bot/overview),
[docs.x.ai/grok-bot/computer-and-apps](https://docs.x.ai/grok-bot/computer-and-apps))

- **Bots**: named, avatar, persistent conversation memory; you message them like
  colleagues (desktop macOS/Windows/Linux, iPhone; Android app per docs overview).
- **Skills**: reusable instruction sets ("steps, decision rules, expected output, and
  safety boundaries"), shared across bots; created by writing instructions or
  teach-by-demonstration (≤10 min browser recording).
  ([docs.x.ai/grok-bot/skills-routines-and-automations](https://docs.x.ai/grok-bot/skills-routines-and-automations))
- **Routines**: schedule- or event-triggered runs — "Every weekday at 8:00 AM";
  event triggers via account integrations (Slack message, GitHub notification) and,
  per xAI's design essay, webhook events. Up to **50 routines per bot**, 20 run records
  retained, routines run unattended in the cloud but **may be paused after prolonged
  user absence**. ([skills-routines-and-automations](https://docs.x.ai/grok-bot/skills-routines-and-automations),
  [x.ai/news/designing-grok-bot](https://x.ai/news/designing-grok-bot))
- **Multi-bot collaboration**: bots message each other and share context in group
  chats — native orchestration exists at the UX level.
  ([introducing-grok-bot](https://x.ai/news/introducing-grok-bot))
- **Tool access**: "Bots use connectors where available and computer use for everything
  else" — connectors surface as **Plugins** in-app; there is an X plugin (search posts,
  timelines, trends) with free X API credits for paid users
  ([x.ai/news/grok-bot-and-x](https://x.ai/news/grok-bot-and-x)). The Grok Bot docs do
  **not** document custom/bring-your-own MCP for bots (the BYO-MCP surface documented
  officially is grok.com chat connectors, §1c). Not found in official sources: a
  programmatic API to create/message/steer bots.
- **Entitlement**: included with **SuperGrok, SuperGrok Plus, SuperGrok Heavy**, X
  Premium+, and paid Cursor plans (Grok Bot uses Cursor authentication/billing; a
  SuperGrok link is a usage grant). Bot usage is **separate** from Grok/Cursor plan
  usage, resets weekly; **no numeric caps are published**. Overage = on-demand billing
  through Cursor. ([x.ai/news/grok-bot-more-plans](https://x.ai/news/grok-bot-more-plans),
  [cursor.com/help/grok-bot/plans](https://cursor.com/help/grok-bot/plans))
- **Enterprise** (2026-09-03): access/network/audit controls, bots "isolated with zero
  default access". ([x.ai/news/grok-bot-for-enterprise](https://x.ai/news/grok-bot-for-enterprise))

### 1b. API-side agentic building blocks (GA on `api.x.ai`)

- **Agent Tools API** — server-side tools executed on xAI infra, enabled via the
  `tools` array on the **`/v1/responses`** endpoint (OpenAI-compatible + xai_sdk):
  `web_search`, `x_search`, `code_interpreter` (Python sandbox), `collections_search`
  (uploaded-document retrieval with citations), image generation, and **`mcp`** —
  xAI-managed connection to a remote MCP server you specify (`server_url`).
  ([docs.x.ai/developers/tools/overview](https://docs.x.ai/developers/tools/overview),
  [x.ai/news/grok-4-1-fast](https://x.ai/news/grok-4-1-fast),
  [docs.x.ai/developers/tools/remote-mcp](https://docs.x.ai/developers/tools/remote-mcp))
- **`x_search`** — keyword/semantic/user/thread search on X; parameters:
  `allowed_x_handles` / `excluded_x_handles` (≤20, mutually exclusive), `from_date` /
  `to_date` (ISO-8601), image/video understanding toggles; returns citations.
  ([docs.x.ai/developers/tools/x-search](https://docs.x.ai/developers/tools/x-search))
- **Server-side multi-agent** — model **`grok-4.20-multi-agent`** (beta,
  `/v1/responses` only): the API itself spawns 4 or 16 collaborating sub-agents
  (leader synthesizes; sub-agent state encrypted, not returned). Supports the built-in
  tools above; **no client-side function calling**, no `max_tokens`, not on chat
  completions. All sub-agent tokens are billed.
  ([docs.x.ai/developers/model-capabilities/text/multi-agent](https://docs.x.ai/developers/model-capabilities/text/multi-agent))
- **No hosted "Agents API"** (persistent server-side agent objects with schedules) on
  the developer platform — scheduling/persistence is Grok Bot's (consumer/teams) job.
  Not found in official sources: any cron/scheduled-run primitive on api.x.ai.

### 1c. Consumer Grok app agent features (context, not integration targets)

- **Grok Automations** (2026-07-16): scheduled prompts (once/daily/weekdays/weekly/
  monthly/yearly, user TZ) free for all users; **email triggers** gated to SuperGrok.
  Output → notifications/email; no API delivery.
  ([x.ai/news/grok-automations](https://x.ai/news/grok-automations))
- **Grok connectors** (grok.com/web/iOS/Android): prebuilt OAuth connectors + **custom
  MCP servers** ("New Connector → Custom → MCP server URL") — server must be reachable
  over the **public internet**.
  ([docs.x.ai/grok/connectors](https://docs.x.ai/grok/connectors),
  [x.ai/news/grok-connectors](https://x.ai/news/grok-connectors))
- **Grok Build** — xAI's coding agent (own model `grok-build-0.1`); not relevant here.
  ([docs.x.ai/build/overview](https://docs.x.ai/build/overview))

---

## 2. Capability / pricing table

### Models (docs.x.ai/developers/models + /developers/pricing, read 2026-09-16)

| Model | Context | Input /1M (<200k prompt) | Cached | Output /1M | Notes |
|---|---|---|---|---|---|
| grok-4.6 | 500k | $2.00 | $0.50 | $6.00 | frontier, Aug 2026; 2× above 200k prompt |
| grok-4.5 | 500k | $2.00 | $0.30 | $6.00 | coding/agentic, Jul 2026 |
| grok-4.3 | 1M | $1.25 | $0.20 | $2.50 | workhorse; batch −20% |
| grok-4.20-0309 (reasoning/non) | 1M | $1.25 | $0.20 | $2.50 | Mar 2026 |
| grok-4.20-multi-agent | 1M | $1.25 | $0.20 | $2.50 | beta; ×(4 or 16) sub-agent token burn |
| grok-4-1-fast (reasoning/non) | 2M | $0.20 | $0.05 | $0.50 | cheap tool-caller; agent-tools compatible |
| grok-build-0.1 | 256k | $1.00 | $0.20 | $2.00 | coding agent |

Modifiers: priority processing 2×; US regional endpoint 1.1×; batch −20% (grok-4.3,
4.20 variants). `grok-4` / "grok-4.5 vs grok-4" as the operator guessed them are no
longer the lineup — 4.20/4.3/4.5/4.6 are current.

### Server-side tools (docs.x.ai/developers/pricing)

| Tool | Price | Notes |
|---|---|---|
| web_search | $5 / 1k successful calls | |
| x_search | $5 / 1k calls → **from 2026-09-21 12:00 PT: $5 / 1k posts fetched + $10 / 1k profiles** | parent/quoted posts count; this is a real cost regime change for us |
| code_interpreter | $5 / 1k calls | Python sandbox |
| collections/file search | $2.50 / 1k calls | file attachments $10 / 1k |
| mcp (remote MCP) | token-based (no per-call fee listed) | xAI dials out to your `server_url` |

Tool-call rate limits: not published per tool in docs read; console.x.ai is the
authority per docs. Agent tools pricing was cut up to 50% in Aug 2026, capped at $5/1k
calls ([release notes](https://docs.x.ai/developers/release-notes)).

### Auth / subscription reality

- **SuperGrok subscription** grants: Grok app (+Automations incl. email triggers),
  **Grok Bot** (weekly bot usage, unpublished caps), and — since May 2026 — xAI's
  first-party **OAuth for agents** (our device-code RFC 8628 flow against auth.x.ai,
  scope `grok-cli:access api:access`), which is exactly what `xai_auth.py` implements.
- **Caveat (secondary source, flagged):** a NousResearch/hermes-agent issue
  ([#26847](https://github.com/NousResearch/hermes-agent/issues/26847), May 2026,
  closed-not-planned) documents xAI's backend 403-ing standard-tier SuperGrok OAuth
  inference while docs claimed all tiers. Our own integration already treats **403 =
  entitlement → quarantine, no retry** (`src/trader/llm/xai_auth.py`) and holds
  `XAI_API_KEY` as first-class fallback — keep both.
- **Not found in official sources:** any statement that the subscription-OAuth bearer
  is entitled to *agent tools invocations*, `grok-4.20-multi-agent`, or which models it
  may call. `/v1/responses` is the right surface either way (it hosts all agent tools);
  entitlement per tool/model must be probed empirically (extend the
  `docs/research/xai-api.md` probe matrix). Budget below assumes **API-key pricing**
  as the honest worst case; anything the OAuth bearer covers is upside.

---

## 3. Integration design: "macro research desk" → `/research/notes`

### 3.0 The decisive constraint: our trader is LAN-only, push-only

Every xAI-hosted option (Grok Bot's cloud computer, grok.com custom MCP connectors,
the API's remote-`mcp` tool) requires the counterparty to be **reachable over the
public internet** ([docs.x.ai/grok/connectors](https://docs.x.ai/grok/connectors)).
The trader's API/MCP lives at `<trader-host>:8080` (a LAN address) behind `X-API-Key`
(`docs/hermes/README.md`) and the contract is push-only capture. Two consequences:

1. **Never point xAI's remote-`mcp` tool at the trader's MCP server.** Even
   tunnel-exposed, the `/mcp` surface carries operator commands (`pause`,
   `flatten_all`, `resolve_approval`…) behind the same single `TRADER_API_KEY` — an
   xAI-side model with that MCP connection is one bad tool-choice away from an
   operator command. The Hermes contract's "capture clients only" line is enforceable
   only if xAI-side agents **never hold trader credentials at all**.
2. Therefore **orchestration lives on our box.** xAI has a hosted persistent-agent
   product (Grok Bot — it does exist, §1a), but it cannot *push* into a LAN service
   without us exposing that service, it publishes no usage caps, no programmatic bot
   API, and routines can auto-pause on user absence — wrong reliability shape for an
   unattended daily pipeline. Verdict: **Grok Bot is real but not the right harness
   for this integration.** The right harness is our scheduler calling xAI's Agent
   Tools API; the Grok "agents" are stateless research functions.

### 3.1 Components (all on the Spark, new module `src/trader/research_sweeps/` or a
sibling repo — either way a standalone process, not inside `trader-api`)

```
systemd user timers (one per desk seat)
        │  fires
        ▼
sweep runner ── POST https://api.x.ai/v1/responses ──► grok-4.3 (or 4-1-fast)
   │                 tools: [x_search{handles,from_date,to_date},
   │                         web_search]
   │                 structured-output note schema
   ▼ tool-cited candidate notes (0..3)
grounding gate (local, deterministic)
   │  – every note must cite ≥1 URL present in the API's returned citations
   │  – authored_at only if extracted from a cited source; else omitted
   │  – body ≤1,500 chars; scope required (symbols|asset_classes)
   ▼
dedupe gate (local)
   │  – content-hash + claim-key registry; GET /research/notes (localhost) recency check
   ▼
capture client (the ONLY holder of TRADER_API_KEY)
   └── POST http://localhost:8080/research/notes
        {title, body, symbols/asset_classes, tags:[desk],
         source: "grok-desk/<seat> + <cited URL>", authored_at?}
        ▼
trader's own Macro View compiler (existing) = the synthesizer
```

**Where's the "team"?** Four specialist *seats* (prompts + tool scopes), not four
persistent bots. The cross-seat synthesis step the operator imagined **already exists
in the trader**: the versioned Macro View compiler digests all live notes, cites note
ids, preserves contradictions as `disputed` (architecture.md §Operator Research Desk).
Building a Grok-side synthesizer would duplicate it and launder away per-note
citations. Optional weekly deep-dive seat uses `grok-4.20-multi-agent` (4-agent mode)
for genuine multi-perspective synthesis — its leader output still lands as 1–3
distilled notes, never as a compiled view.

### 3.2 The desk seats and schedule (systemd timers, UTC)

| Seat | Scope submitted | Cadence | Tools/anchors |
|---|---|---|---|
| crypto-flows | `symbols:["ETH/USD"]`, `asset_classes:["crypto"]` | 2×/day 00:30, 12:30 | x_search (curated ETF-flow/on-chain handles, `from_date`=last sweep), web_search |
| rates-inflation | none (macro) or `asset_classes` | 1×/day 11:30 (pre-US open) | web_search (Fed, BLS, Treasury), x_search |
| energy-geopolitics | none (macro) | 1×/day 06:00 | web_search, x_search |
| equity-breadth | `symbols:["SPY"]`, `asset_classes:["us_equity"]` | 1×/day 21:30 (post-close) | web_search, x_search |
| deep-dive (optional, phase 2) | per topic | 1×/week Sun | grok-4.20-multi-agent, 4 agents |

Every seat prompt embeds the research-capture rules (distill ≤1,500 chars, claims +
numbers as the source states them, no model opinion, `authored_at` = source
publication date or omitted, "zero grounded findings → return empty list" is success).

### 3.3 Discipline answers (the honest part)

- **Orchestration lives:** on our box (systemd timers + a ~300-line runner). xAI hosts
  the tool execution and optionally sub-agent fan-out; nothing xAI-side holds state
  between sweeps or credentials, ever.
- **Duplicate-note discipline:** notes are immutable, so dedupe happens **before
  submission, locally**: (1) per-seat claim registry (normalized claim key + content
  hash, e.g. `crypto-flows/etf-net-outflow-week`), (2) the runner injects the seat's
  last N submitted note titles/claims into the prompt as "already filed — only file
  material *changes*", (3) hard cap ≤3 notes/sweep. A repeated claim with a *new*
  number is a new note (the compiler treats contradiction as information); a repeated
  claim with the same number is dropped. The trader never dedupes — garbage kept out
  at the gate.
- **Hallucination control:** the failure mode is a note from model priors. Defense is
  structural, not prompt-only: `/v1/responses` returns tool citations; the local
  grounding gate **rejects any note whose body cites no URL that the tool layer
  actually returned** and strips `authored_at` unless it was extracted from a cited
  source (never guess — matches SKILL.md). x_search `from_date` anchoring keeps the
  model off stale priors. A sweep with zero survivors submits nothing and logs why.
- **Cost per day** (API-key rates, post-2026-09-21 x_search regime, grok-4.3):
  per sweep ≈ 6 x_search calls ×~25 posts = 150 posts → $0.75; 5 web_search → $0.025;
  ~120k input (tool results) + 4k output tokens → ~$0.16. **≈ $0.95/sweep**; 5 sweeps
  /day ≈ **$4.75/day ≈ $145/mo**. Cheap lever: `grok-4-1-fast` for the sweep loop cuts
  the token share ~85% (→ ~$0.80/sweep, tool fees dominate); post-count caps in
  x_search params are the real cost governor. Weekly multi-agent deep dive: ~4× token
  burn on one call ≈ +$1–3/week. If the SuperGrok OAuth bearer turns out to cover any
  of this, cost drops toward $0 — but do not design the budget around it.
- **What the trader must NEVER grant them:** trader credentials of any kind on the
  xAI side (no remote-MCP to our box, no tunnel); operator commands (`pause`,
  `flatten_all`, `wake_trader`, `resolve_approval`, `fetch_x_sentiment`… — the runner's
  capture client calls exactly `submit_research_note` / `get_research_notes` and
  nothing else, enforced in code, not prompt); any path around the compiler+firewall —
  notes remain the only entry, the budgeted Macro View lane remains the only exit into
  the decide prompt, and RiskGuard stays unreadable to all of it. Recommend minting a
  **capture-scoped API key** (today `TRADER_API_KEY` is all-or-nothing) as a
  prerequisite hardening if this runner ever leaves the box.

### 3.4 Risks / unknowns — explicitly not found in official sources

- Whether SuperGrok OAuth bearers can invoke agent tools / multi-agent / which models
  (probe per `docs/research/xai-api.md`; 403 = entitlement handling already exists).
  Secondary evidence of Heavy-only OAuth gating (hermes-agent #26847).
- Grok Bot: no published numeric usage caps, no programmatic bot API, no documented
  custom-MCP for bots, routines auto-pause on user absence — all reasons it lost §3.0.
- Per-tool rate limits on api.x.ai (console.x.ai is authoritative; not in docs read).
- x_search post-count control granularity (cost exposure under the 09-21 per-post
  pricing) — verify actual posts-fetched metering in the first metered week.
- `grok-4.20-multi-agent` is beta with breaking-change warning; keep it optional.
- Pages were read via fetch-and-summarize on 2026-09-16; re-verify exact prices on
  [docs.x.ai/developers/pricing](https://docs.x.ai/developers/pricing) before wiring
  budget gates.

### 3.5 Recommended MVP slice

1. Run the EAI-173 probe with the operator's OAuth lineage against `/v1/responses` ×
   {grok-4.3, grok-4-1-fast} × {x_search, web_search}; record the entitlement matrix
   in `xai-api.md`. Fall back to a funded `XAI_API_KEY` if OAuth is tier-gated.
2. Ship **one seat**: crypto-flows, 1×/day, grok-4.3, x_search+web_search, structured
   note schema, grounding + dedupe gates, capture via localhost `POST /research/notes`
   with `tags:["grok-desk","crypto-flows"]` and agent-attributed `source`.
3. Close the loop the way SKILL.md does: after each sweep, `GET /research/view` and
   log which submitted notes the compiled Macro View actually cites — the real
   distillation-quality metric — plus per-sweep cost from the usage meter.
4. Only after a week of cited, non-duplicative notes: add the other three seats, then
   consider the weekly multi-agent deep dive. Grok Bot (the hosted product) stays a
   watched option for *operator-side* convenience (e.g., a bot that drafts research
   the operator forwards through Hermes) — not a machine-to-machine capture client.

---

## Operator decision (2026-09-17)

Accepted. Hosted Grok Bot is real and stays an operator convenience (draft → Hermes).
It is **not** the M2M capture harness. Orchestration lives on the Spark: systemd
timers → `POST /v1/responses` + `x_search`/`web_search` → local grounding gate (cite a
URL the tool layer actually returned; `authored_at` only if extracted; body ≤1,500
chars) → local dedupe → localhost `POST /research/notes`. The existing Macro View
compiler is the synthesizer. Do not build a Grok-side compiled view. xAI-side agents
never hold trader credentials; do not point xAI remote-MCP or grok.com custom
connectors at this API.

Prerequisites now clear: notes 17–19 distilled 2026-09-17. Remaining: a
**capture-scoped API key** (today `TRADER_API_KEY` can flatten) before any sweep
runner is more than a localhost process.

Do **not** run a second OAuth client for the EAI-173 probe while `trader-api` owns
the refresh lineage (rotating single-use refresh token). Probe with `XAI_API_KEY`,
or as a one-shot inside the running service.

### Filed on GitHub (2026-09-19)

- Epic: [#20 box-local Grok macro desk](https://github.com/sudoTomas/trader/issues/20)
- [#7](https://github.com/sudoTomas/trader/issues/7) probe (was EAI-173)
- [#8](https://github.com/sudoTomas/trader/issues/8) capture-scoped API key
- [#9](https://github.com/sudoTomas/trader/issues/9) crypto-flows seat
- [#10](https://github.com/sudoTomas/trader/issues/10) sweep citation loop
