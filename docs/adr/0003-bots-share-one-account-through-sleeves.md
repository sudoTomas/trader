# Bots share one paper account through Sleeves, checked by the one RiskGuard chain

Several Bots, each with its own symbols, prompt, triggers and Sources, need to trade side by side. We keep one Alpaca paper account and split it into Sleeves: each Bot has a fixed dollar amount of capital and a sub-ledger of the positions its own fills built, and the Sleeves together must equal the account positions, which must equal the venue. RiskGuard stays one chain; a Bot's Sleeve is passed into it as data next to the account, so sizing, "is this reducing", full close and the held-quantity clamp read the Sleeve, and concurrency, trades per day and breakers are checked per Bot and per account. Only one Bot (the Owner) may be live on a symbol, because the venue nets positions per symbol. Every Bot carries an account id from the start, so moving a Bot to its own paper account later is a configuration change, not a schema change.

## Considered Options

- A separate Alpaca paper account per Bot: perfect isolation and attribution, but N credentials, N fills streams and N reconcilers before the second Bot can even shadow.
- A per-Bot risk checker beside the chain: what v1's sleeve did, and why its limits drifted from the chain's (see `docs/lessons.md#risk-chain-consolidated`).
- Shared symbols with netting from the start: needs a cross-Bot lock and a netting check before there is a second strategy worth sharing a symbol with.

## Consequences

- Reconcile gains a check: the Sleeves sum to the account positions. A mismatch is an Account halt.
- An Account halt stops every Bot opening; a Bot's own breaker or pause stops only that Bot. Exits stay managed in both.
- A Bot can never close another Bot's holding, even on the same account.
- Shared symbols, per-Bot Profiles and per-Bot Spend caps remain possible later; for now Profiles, Spend caps and the Macro View are shared by every Bot.
