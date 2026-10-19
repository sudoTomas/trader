# Runbook: the second Bot, in shadow (#126)

Bot #2 is `bot-2-largecap`: the six large caps Bot #1 has shadowed since 2026-10-09/13
(AAPL, MSFT, NVDA, AMZN, GOOGL, META), on its own `equity-largecap` prompt, reading
sentiment, the brief, the held position and the Macro View, with a $5,000 Sleeve on the
Alpaca paper account. Every symbol starts in **shadow**: it decides and journals, and has
no executor at all. Bot #1 keeps shadowing the same six names, so the journal holds a
head-to-head of the two prompts on the same ticks (filter decisions by `bot_id`).

It is created **once**, by an operator command, not by a migration. Do it on a deployment
that runs the Supervisor (#126 or later).

## Before

- The six tickers must be in the build's universe: `TRADER_EXTRA_SYMBOLS` lists them on
  the deployment already (Bot #1 shadows them). A ticker the build does not know is
  refused.
- Nothing else to configure. `TRADER_LOOP_MAX_CONCURRENT_DECIDES` (default 2) already
  caps how many Bots call the decide model at once.

## Create it

CLI (over the API; `TRADER_API_KEY` and `TRADER_API_BASE_URL` as for any `trader` call):

```bash
trader bot create bot-2-largecap \
  --symbols AAPL,MSFT,NVDA,AMZN,GOOGL,META \
  --capital 5000 \
  --source sentiment --source brief --source held_position --source macro_view \
  --prompt equity-largecap \
  --account alpaca-paper
```

Each ticker is in shadow unless written `TICKER=live` (do not: going live is #127 and the
decide-change gate). Or the same through MCP, tool `create_bot`:

```json
{
  "name": "bot-2-largecap",
  "symbols": {"AAPL": "shadow", "MSFT": "shadow", "NVDA": "shadow",
              "AMZN": "shadow", "GOOGL": "shadow", "META": "shadow"},
  "capital_usd": 5000,
  "sources": ["sentiment", "brief", "held_position", "macro_view"],
  "prompt_variant": "equity-largecap",
  "account_id": "alpaca-paper"
}
```

or `POST /bots` with that body. The answer is the Bot at version 1. It is audited
(`category=bots`, `action=create_bot`). Running it twice is refused ("already exists").

## Check

```bash
trader bot show bot-2-largecap            # version 1, spec, Sleeve: capital 5000, no positions
trader loop status --bot bot-2-largecap   # running; shadow_symbols lists all six
trader wake --bot bot-2-largecap          # one forced pass (during NYSE hours)
trader decisions --limit 20               # rows with bot_id = 2, status shadow/denied
```

The Supervisor loads the new Bot on its next tick; no restart. Its decide prompt shows
`Sleeve equity: 5,000.00 USD`, its history is its own rows only, and its system prompt
is the `equity-largecap` rules (`prompt.sha256` in the spec).

## Changing it later

Every change is a new, audited, append-only version, picked up on the next tick:

```bash
trader bot version bot-2-largecap --capital 6000          # or --prompt / --source / --symbols / --trigger key=value
trader bot mode bot-2-largecap NVDA replay                # stop running one symbol
trader bot pause bot-2-largecap                           # this Bot only
trader bot resume bot-2-largecap
```

`trader bot mode … live` is refused while another Bot is live on that symbol (the Owner
rule). Promotion to live is #127, through the decide-change gate.

## Known gap

The sentiment poller only polls symbols a Bot holds, so an all-shadow Bot often has no
recent sentiment row for these names; the sentiment lane is then simply absent from its
prompt, and the `equity-largecap` rules treat missing research as no support.
