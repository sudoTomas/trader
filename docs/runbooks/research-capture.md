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

## Hermes is a capture client, never a dependency

A Hermes skill may distill a forwarded article, screenshot, or voice memo into a note and
call `submit_research_note` over MCP — that is the *intended* mobile capture path. The
invariants that keep it safe (docs/architecture.md §Operator Research Desk):

- **The trader never calls outward to Hermes.** Capture is push-only, into the same three
  doors any client uses; there is no Hermes adapter in this codebase and none should appear.
- **Capture works with Hermes absent.** CLI and API cover every capability; losing Hermes
  loses convenience, not function.
- A Hermes-submitted note is an operator note like any other: same auth, same audit row,
  same auto-stale window, same advisory firewall downstream (RiskGuard never reads research).
