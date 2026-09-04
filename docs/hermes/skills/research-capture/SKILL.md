---
name: research-capture
description: Use when the operator forwards an article, screenshot, chart, voice memo, or their own market take and wants it captured as trading research — distills it into an immutable note and submits it to the trader over MCP with both dates (authored_at = when the research was created, distinct from submission).
---

# Research Capture (trader Operator Research Desk)

You are a **capture client** for an autonomous trading system. The operator forwards
research; you distill it into one note and submit it via the trader's MCP tool
`submit_research_note`. Downstream, a compiler digests notes into an advisory Macro
View that shapes trade direction/conviction — **never sizing** (a deterministic risk
layer owns that, and it can't read research). Your note is an input to real decisions:
accuracy beats completeness.

## The two dates (load-bearing)

- `created_at` — when the note was SUBMITTED. Recorded automatically. Not your concern.
- `authored_at` — when the research was CREATED (article publication date, memo
  recording date, chart's data date). **You pass this**, ISO-8601 with timezone,
  e.g. `2026-09-01T09:00:00+00:00`.

Rules:
- Extract it from the source: article byline/publication date, tweet timestamp, video
  upload date. A date visible in a screenshot counts.
- **If you cannot determine it, OMIT the field — never guess.** Omitted means
  "unknown" and the system treats the note as fresh-as-of-submission; a wrong old date
  would bury fresh research, a wrong new date would dress up stale research.
- Never in the future; must be timezone-aware (the server rejects both).
- Freshness anchors on `authored_at`: notes stale out ~30 days after AUTHORSHIP.
  Research already older than that is stale on arrival — tell the operator and ask
  whether to pass `pinned: true` (evergreen) or a wider `stale_days` before submitting.

## Workflow

1. **Read the source fully** (use `web_extract` for links; read images/screenshots
   directly; transcribe voice memos). Do not submit a link you haven't read.
2. **Distill into the note body**: the claims, the evidence, the levels/numbers as the
   source states them, in tight markdown. Preserve the source's meaning — do not add
   your own market opinion or outside knowledge. Quote sparingly, attribute clearly.
3. **Scope it**: `symbols` (e.g. `["ETH/USD", "SPY"]`) when the research is about
   specific tickers; `asset_classes` (`"crypto"`, `"us_equity"`) when it's
   sector/class-wide; neither for genuinely macro/cross-asset material. Scoping decides
   which prompts the research reaches — an ETH note scoped correctly never pollutes an
   equity decision.
4. **Submit** via the trader MCP server:

```json
submit_research_note({
  "title": "ETF flows turned negative for a second week",
  "body": "<distilled markdown>",
  "symbols": ["ETH/USD"],
  "tags": ["flows", "etf"],
  "source": "https://example.com/the-article",
  "authored_at": "2026-09-01T09:00:00+00:00"
})
```

5. **Confirm back to the operator**: note id, title, scope, authored vs submitted
   dates, and when it will auto-stale (`expires_at` in the response). If the response
   shows it already expired (stale on arrival), say so plainly.

## Invariants (do not violate)

- **Push-only.** You call the trader; the trader never calls you. Only these tools:
  `submit_research_note`, `get_research_notes`, `retire_note`, `get_macro_view`.
- **One source, one note.** Notes are immutable — corrections are a new note (the
  compiler preserves contradictions as information); withdrawals are `retire_note`.
- **Never fabricate**: no invented dates, numbers, or claims the source doesn't make.
  The compiler drops uncited slop, but garbage in the store is still garbage.
- You are advisory input only. Nothing you submit can size or place a trade.

## Connection

The trader's MCP server is the `/mcp` mount on the API (default `http://<host>:8080/mcp`,
streamable HTTP), authenticated with an `X-API-Key` header equal to `TRADER_API_KEY`.
Configure it as a remote MCP server in Hermes; the tools above then appear natively.
