# Runbook: capturing research notes (EAI-177)

The Research Desk's capture layer. Hand-picked macro/market research enters through any
of the three doors below; the compiler (part 2, EAI-178) reads what `list` returns.
**Capture once, compile async** — nobody hand-edits the compiled knowledge base.

## Lifecycle in one paragraph

A note is immutable markdown: the body is stored verbatim and never edited. Every unpinned
note **auto-stales** out of compilation after its window (`TRADER_RESEARCH_STALE_DAYS`,
default 30; per-note override `stale_days`) — it keeps its status and stays queryable with
`--stale`, it just stops informing the trader. `--pin` marks a note evergreen (no window).
Retiring is always an explicit operator act, never automatic. Every submit and retire lands
in `audit_events` (category `research`).

## CLI

```bash
# body from stdin (pipe or paste, ^D ends)
pbpaste | trader note add --title "Fed pivot thesis" --tag macro --symbol ETH/USD

# body from a file / a flag
trader note add --title "Halving supply math" --file note.md --asset-class crypto
trader note add --title "One-liner" --body "ETF flows turned negative this week." \
  --source "https://example.com/article"

trader note list                 # what compilation sees
trader note list --stale --retired --limit 200
trader note retire 42
```

## API

```bash
curl -X POST "$TRADER_API_BASE_URL/research/notes" \
  -H "x-api-key: $TRADER_API_KEY" -H "content-type: application/json" \
  -d '{"title": "Fed pivot thesis", "body": "…markdown…", "tags": ["macro"],
       "symbols": ["ETH/USD"], "source": "https://example.com", "pinned": false}'

curl "$TRADER_API_BASE_URL/research/notes?include_retired=true" -H "x-api-key: …"
curl -X POST "$TRADER_API_BASE_URL/research/notes/42/retire" -H "x-api-key: …"
```

## MCP

Tools on the `/mcp` mount (same auth): `submit_research_note`, `get_research_notes`,
`retire_note`. Same parameters as the API body.

## The compiled Macro View (part 2, EAI-178)

Note changes recompile the Macro View automatically after a quiet window
(`TRADER_RESEARCH_COMPILE_DEBOUNCE_SECONDS`, default 30 s), via the `compiler` LLM role
(Grok, Nemotron backstop). Inspect or force it:

```bash
trader macro view                # current digest: thesis + per-asset-class sections
trader macro view --version 3    # any historical version (decisions journal the one they saw)
trader macro compile             # recompile now, skipping the debounce
```

API: `GET /research/view[?version=N]`, `POST /research/view/compile`.
MCP: `get_macro_view`, `compile_macro_view`.

Compiled claims cite `[note:<id>]`; uncited sections are dropped at compile time, disputes
are preserved (never LLM-resolved), and every `$`-level is stamped `(example-not-current)`.
The decide prompt receives a budgeted slice in its own lane; RiskGuard never sees it.

## The markdown mirror (part 3, EAI-179)

Every note change and compile rewrites a one-way markdown export for Obsidian-style
browsing: `research/notes/<id>-<slug>.md` (frontmatter + body verbatim) and
`research/macro-view.md`. Force a rewrite with `trader note mirror`
(`POST /research/mirror/export`, MCP `export_research_mirror`).

**Decision (per the EAI-179 checklist): the folder is gitignore-excluded, not committed.**
The DB is the only source of truth and the mirror regenerates from it at any time, so
committing generated files would only add churn. `TRADER_RESEARCH_MIRROR_DIR` moves it
(empty disables).

**Strictly one-way.** Files carry a DO NOT EDIT banner; local edits are overwritten on
the next export and nothing ever reads them back — capture happens only through the
three doors above.

## Per-candidate briefs (EAI-192)

Where the Macro View digests notes per **asset class**, a brief digests per **symbol**:
the `brief` LLM role (Grok, Nemotron backstop) compiles the notes scoped to that symbol
(`--symbol` on capture) plus the latest ok sentiment row into a versioned `briefs` row.
Store-resident inputs only — no live search, no model priors: **no symbol-scoped inputs
means no brief and no LLM call**, an uncited thesis drops the whole brief, uncited
points drop individually, and `$`-levels freeze as example-not-current (the EAI-178
rules). Briefs recompile debounced on note submit/retire; sentiment polls do NOT
trigger recompiles (bounds Grok spend).

```bash
trader brief show ETH/USD              # current brief (add --version N for history)
trader brief compile ETH/USD           # compile now, skipping the debounce
```

API: `GET /research/briefs?symbol=`, `POST /research/briefs/compile?symbol=`. MCP:
`get_brief`, `compile_brief`.

**The prompt lane is gated.** Briefs compile and store regardless, but they reach the
decide prompt only with `TRADER_BRIEFS_ENABLED=true` — injecting a lane is a
decide-behavior change, so the flip goes through `docs/runbooks/decide-change-gate.md`
(replay → shadow → flip; one gate at a time — it queues behind any open SPY/sentiment
gate). While dark, review accruing briefs with `trader brief show`; once live, every
decision journals the `brief_version` it saw, and RiskGuard never reads any of it.

## X sentiment (part 4, EAI-180)

Needs an xAI credential (`trader xai login` or `XAI_API_KEY`) — `x_search` runs
server-side on xAI, so without one the service is absent and every door answers
`unavailable`. With the loop running, each symbol is polled hourly
(`TRADER_SENTIMENT_POLL_SECONDS`) under a daily cap (`TRADER_SENTIMENT_DAILY_CALL_CAP`).

```bash
trader sentiment fetch ETH/USD   # one poll now (counts against the cap)
trader sentiment history --symbol ETH/USD
```

API: `GET /research/sentiment`, `POST /research/sentiment/fetch?symbol=`. MCP:
`get_sentiment`, `fetch_x_sentiment`.

**The degraded rule is load-bearing**: a reply with zero live citations came from model
priors, not X — it journals as `unavailable` and never carries a number. **Shadow-first**:
the sentiment wake trigger ships OFF (`TRADER_SENTIMENT_TRIGGER_ENABLED=false`); flip it
only after ≥1 week of journaled scores reads sane. When on, it wakes the trader on a
material score swing (`TRADER_SENTIMENT_SWING_THRESHOLD`) for **held symbols only**;
degraded reads never trigger.

## Hermes is a capture client, never a dependency

The capture skill lives at `docs/hermes/skills/research-capture/SKILL.md` — install it
into the Hermes agent (e.g. copy the `research-capture/` directory into Hermes's skills
location and add the trader's `/mcp` endpoint as a remote MCP server with the
`X-API-Key` header). It distills a forwarded article, screenshot, or voice memo into a
note and calls `submit_research_note`, passing `authored_at` (publication date) so
freshness anchors on when the research was created — omitted when unknown, never
guessed. The invariants that keep it safe (docs/architecture.md §Operator Research
Desk):

- **The trader never calls outward to Hermes.** Capture is push-only, into the same three
  doors any client uses; there is no Hermes adapter in this codebase and none should appear.
- **Capture works with Hermes absent.** CLI and API cover every capability; losing Hermes
  loses convenience, not function.
- A Hermes-submitted note is an operator note like any other: same auth, same audit row,
  same auto-stale window, same advisory firewall downstream (RiskGuard never reads research).

## Grok desk seat (#9, #20)

A **seat** is a stateless research function the box runs on a timer: one prompt + tool
scope, one `POST https://api.x.ai/v1/responses` with `x_search` + `web_search` (tools
exist only there — docs/research/xai-api.md), local deterministic gates, then the same
capture door Hermes uses. It is a **standalone process** (`trader seat run <seat>`), never
part of `trader-api`, and it holds exactly two credentials: an xAI credential for the
search and `TRADER_CAPTURE_API_KEY` for the write path. The operator key never touches
the seat's write path. Design: `docs/research/2026-09-16-grok-bots-macro-team.md`
(accepted 2026-09-17). One seat ships: **`crypto-flows`** — ETF flows, exchange balances,
stablecoin supply, funding/basis, on-chain activity, regulatory flow news, 1×/day.

What one run does (`src/trader/seats/runner.py`):

1. `GET /research/notes` (capture key) — the live notes, for the dedupe gate and for the
   "already filed, only file changes" list the prompt receives.
2. One `/v1/responses` call on `TRADER_SEAT_MODEL` (`grok-4.3`) with `x_search`
   (`from_date`/`to_date` = the last 24 h) and `web_search`, structured JSON out:
   `{notes: [{title, body, symbols, tags, source, authored_at, confidence, citations}]}`,
   at most `TRADER_SEAT_MAX_NOTES` (3). The prompt carries the research-capture rules
   from the Hermes skill: distill, numbers as the source states them, no opinion,
   `authored_at` only when the source shows it, **zero grounded findings → `[]` is a
   successful sweep**.
3. **Grounding gate** — a note keeps only the citations the tool layer actually
   returned; none left means the claim is the model's prior and the note is dropped
   (docs/lessons.md#sentiment-no-citations-is-unavailable). `source` must be a grounded
   URL. `authored_at` survives only tz-aware and not in the future; else omitted, never
   guessed. `symbols` is clipped to the seat's scope (`ETH/USD`, `BTC/USD`);
   `asset_classes` is always `["crypto"]`; tags are `["grok-desk","crypto-flows", …]`.
4. **Dedupe gate** — a live note from the last 7 days with the same title or source URL
   blocks the submit (and duplicates inside the batch collapse). A repeated claim with a
   new number is a new note; the compiler treats contradiction as information.
5. **Size gate** — a body over `TRADER_SEAT_NOTE_MAX_CHARS` (1,400) goes back to the
   model once to distill; still over → dropped.
6. `POST /research/notes` per survivor, capture key only. The service recompiles the
   Macro View on its own debounce; the seat never calls `compile`.
7. Append one JSON line to `TRADER_SEAT_STATE_PATH`
   (`~/.local/state/trader/seat-runs.jsonl`): submitted note ids, drops with reasons,
   token usage, and the seat's **own cost estimate** (token prices from `llm/usage.py`,
   x_search per-post/profile units from citations, web_search per call). The seat runs
   outside the service meter, so `usage_daily` never sees this spend — the file is the
   record.

**Credential rule.** Exactly one process owns the rotating SuperGrok OAuth lineage and it
is `trader-api` (docs/lessons.md#oauth-refresh-margin), so the seat authenticates with
`XAI_API_KEY` by default and refuses to run without one. `--credential oauth` (or
`auto`) reads the shared encrypted token store for a one-shot — a small race window
against the service's own refresh; use it deliberately, not from the timer.

```bash
# First manual run, on the box, from the checkout (.env supplies the keys):
uv run trader seat run crypto-flows --dry-run        # sweep + gates, POST nothing
uv run trader seat run crypto-flows                  # the real thing; prints ids + est. cost
uv run trader seat run crypto-flows --json           # the full run record
uv run trader note list                              # the notes, tagged grok-desk
tail -1 ~/.local/state/trader/seat-runs.jsonl        # the run log line
```

Then the timer (daily 13:00 UTC, after the prior session's ETF flow prints):

```bash
cp infra/systemd/trader-seat-crypto-flows.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now trader-seat-crypto-flows.timer
systemctl --user list-timers trader-seat-crypto-flows.timer
journalctl --user -u trader-seat-crypto-flows.service -n 50
```

The unit runs `uv run trader seat run crypto-flows` in `~/repos/trader`, so it reads the
same `.env` as `trader-api` (`TRADER_API_BASE_URL`, `TRADER_CAPTURE_API_KEY`,
`XAI_API_KEY`, `TRADER_SEAT_*`). Adding a second seat is a new `Seat` in
`src/trader/seats/` plus its own unit pair — after a week of cited, non-duplicative
crypto-flows notes, per the design's §3.5.

### Closing the loop: which seat notes the Macro View cites (#10)

The compiler drops uncited claims, so the real distillation-quality metric is not "notes
submitted" but "notes the compiled Macro View actually cites". `trader seat report`
reads the run log back, fetches the current view (`GET /research/view`, capture key),
and matches each run's submitted ids against the `[note:N]` markers the compiler emits
(`research/compiler.py`). Per run: cited/submitted, the cited ids, the seat's own cost
estimate, and — only when `TRADER_API_KEY` is set, because `GET /usage` is
operator-only — the day's `usage_daily` total (the service-side spend the notes
triggered: compile, briefs). The seat's own spend is never in `usage_daily`; the
`seat $` column is that record.

```bash
uv run trader seat report                 # table, newest 10 runs
uv run trader seat report --limit 30 --json
uv run trader seat report --seat crypto-flows
```

Read it the way the Hermes skill does after a PDF batch: a week of runs where most
submitted ids show up cited and few drops read `duplicate` is the bar for adding the
next seat; a run where nothing is cited means the distillation missed, not that the
compiler is wrong.
