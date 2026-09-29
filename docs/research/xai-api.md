# Spike: xAI API surface for the SuperGrok OAuth bearer

**Question (#7, was EAI-173):** does the SuperGrok OAuth access token work on
`POST /v1/chat/completions`, or only on `POST /v1/responses`? And which models and
server-side tools (`x_search`, `web_search`) does each credential reach? v1 never resolved
this — Hermes used `/responses`, embry0 used `/messages`. `/v1/responses` hosts `x_search`
regardless, so the sentiment role already lives on `/responses`.

## Status: probe built; live matrix pending the operator's run

OAuth is live on the deployed service, and **the running `trader-api` is the only process
allowed to hold the rotating refresh lineage** (docs/lessons.md#oauth-refresh-margin: two
owners of one single-use token break each other). So the probe is not a script — it is an
operator-only diagnostic capability, `probe_xai`, that runs *inside* the service on the
same authenticated client the research roles use (`llm.xai.xai_http_client`: OAuth first,
`XAI_API_KEY` fallback). Nothing in the repo calls xAI on its own; the matrix below is
filled by hand from the operator's run.

What the probe does (`src/trader/llm/xai_probe.py`):

- one minimal request per **endpoint × model × tool** cell (`max_tokens` /
  `max_output_tokens` 16; tool cells ask the model to "search for the word test", with a
  one-day `from_date`/`to_date` window on `x_search`; `web_search` is sent bare because it
  takes no window on the wire);
- sequential, with a short pause between cells, so it never bursts the vendor and burns at
  most one OAuth refresh;
- never raises: each cell returns `status_code`, `ok`, `credential_used`
  (`oauth` | `api_key`), `error_class`, `usage` (input/output tokens), `latency_ms`,
  `citations_count`, and a short `detail` excerpt of a failing body;
- 401/403 classify as `entitlement` (the manager's quarantine rules are untouched — the
  probe only reports); timeouts as `timeout`; 429 as `rate_limited`; 5xx as
  `server_error`; other 4xx as `bad_request` (usually an unknown model id or a tool the
  endpoint does not accept);
- unmetered (diagnostics, not a role) but counted: the token usage comes back so the cost
  of a run is visible. One `probe_xai` audit row summarises each run.

## How to run the probe (operator, once, after deploy)

The CLI goes through the API (unlike `trader xai login`, which is local by design), so
the service must be running with its credential path configured.

```bash
# The #7 matrix on the credential the service resolves (OAuth, else XAI_API_KEY):
uv run trader xai probe

# Pin one credential to fill each column separately:
uv run trader xai probe --credential oauth
uv run trader xai probe --credential api_key

# Narrow a re-run (all flags repeatable); --json for the full per-cell payload:
uv run trader xai probe --model grok-4.3 --endpoint responses --tool x_search --json
```

Defaults: models `grok-4`, `grok-4.3`, `grok-4-1-fast`; endpoints `chat/completions` and
`responses`; tools `none`, `x_search`, `web_search` — 18 cells, ~16 output tokens each.
The same capability is `POST /xai/probe` (JSON body `{models, endpoints, tools,
credential}`) and the MCP tool `probe_xai` behind the operator key.

Then record the matrix below (status code + error class per cell, plus whether tool cells
returned citations) and, if `/chat/completions` rejects the OAuth bearer (401/403 with a
working `/responses`), point `xai_client` at `/responses` and adapt the payload mapping
in `llm/xai.py`. Close #7 with the recorded matrix.

## Findings

Recorded by the operator from `trader xai probe` (date: _pending_). A cell reads
`<http> <error_class or ok> [cites=n]`.

### SuperGrok OAuth bearer (`--credential oauth`)

| endpoint | model | none | `x_search` | `web_search` |
|---|---|---|---|---|
| `/v1/chat/completions` | `grok-4` | _pending_ | _pending_ | _pending_ |
| `/v1/chat/completions` | `grok-4.3` | _pending_ | _pending_ | _pending_ |
| `/v1/chat/completions` | `grok-4-1-fast` | _pending_ | _pending_ | _pending_ |
| `/v1/responses` | `grok-4` | _pending_ | _pending_ | _pending_ |
| `/v1/responses` | `grok-4.3` | _pending_ | _pending_ | _pending_ |
| `/v1/responses` | `grok-4-1-fast` | _pending_ | _pending_ | _pending_ |

### `XAI_API_KEY` (`--credential api_key`)

| endpoint | model | none | `x_search` | `web_search` |
|---|---|---|---|---|
| `/v1/chat/completions` | `grok-4` | _pending_ | _pending_ | _pending_ |
| `/v1/chat/completions` | `grok-4.3` | _pending_ | _pending_ | _pending_ |
| `/v1/chat/completions` | `grok-4-1-fast` | _pending_ | _pending_ | _pending_ |
| `/v1/responses` | `grok-4` | _pending_ | _pending_ | _pending_ |
| `/v1/responses` | `grok-4.3` | _pending_ | _pending_ | _pending_ |
| `/v1/responses` | `grok-4-1-fast` | _pending_ | _pending_ | _pending_ |

Decision after the run: _pending_ (which endpoint `xai_client` targets, and whether the
research roles can move off `grok-4`).

## Known facts carried from v1

- The refresh token rotates on every use and is single-use — hence the
  persist-before-serve discipline in `llm/xai_auth.py`.
- 403 from `auth.x.ai` or `api.x.ai` is entitlement (SuperGrok-tier gating; X Premium+
  does NOT include API access) — quarantine, never retry-spin.
- Seeding from the Grok CLI's `~/.grok/auth.json` breaks the CLI's own login
  (two owners of one rotating lineage) — deliberately not implemented.
