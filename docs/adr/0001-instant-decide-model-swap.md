# The decide model swaps instantly behind a Probe, not through the decide-change gate

`docs/runbooks/decide-change-gate.md` requires replay, a shadow window and a deliberate flip for every decide-behavior change, and a model swap is one of them. The model picker deliberately exempts the decide model: a new Chain takes effect on the next wakeup once its first model passes a Probe in the decide contract. We chose speed of trying models over the gate's evidence because the agent trades paper money only, and because the two failures the gate guards against are covered more cheaply: a model that cannot meet the contract is refused by the Probe, and attribution survives because every decision names the Profile version that was active and the model that answered. Replay stays one click away as an option, not a requirement. Prompt edits, new prompt lanes and new symbols still go through the gate.

## Consequences

- A decide swap no longer counts as an open gate, so it may land on the same day as another flip; the journal's Profile version is what keeps the two apart.
- If the agent ever trades live money (`TRADER_ALPACA_LIVE_ENABLED`), revisit this: the cost of a bad swap stops being journal noise.
