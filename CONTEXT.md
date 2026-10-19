# Trader

Paper trading loop: a model proposes an order, a judgment may turn that order into a hold, and a deterministic risk layer is the only thing that allows a broker submit.

## Language

### Money path

**Draft**:
The order proposed for one symbol on one wakeup: an action, a side, a notional, and a rationale that cites the prompt. A hold is a Draft with no order.
_Avoid_: decision, ticket

**Confirm**:
A judgment of a Draft that is an open or a close. It lets the Draft stand or replaces it with a hold. It cannot open a trade the Draft did not propose, and it cannot change the notional. When no Confirm is produced, the Draft stands and the record says the Confirm was skipped. A Confirm lets an open or a close stand only when its probability for that same action clears a margin, chosen later from replay. Below the margin, or if the Confirm prefers hold or the other side, the Draft becomes a hold. A Draft that is already a hold stays a hold. The Confirm runs before the critic: on a non-hold Draft the order is Draft, Confirm, critic, RiskGuard.

**Confirm, first stage**:
Journal-only. Nemotron holds the decide role, Kev is asked on every non-hold Draft through a `confirm` role (`TRADER_CONFIRM_*`), and its answer is stored in its own journal column. No order changes until a margin is chosen from that journal and a flag turns the Confirm on.
_Avoid_: shadow confirm, dry run
_Avoid_: second decider, override, veto

### Models

**Role**:
A job a model does for the agent: decide, analyst, critic, brief, compiler, sentiment, or confirm. Each Role has a contract its model must meet, so only some models fit some Roles.
_Avoid_: seat (a seat is a Grok research sweep), slot, agent

**Provider**:
A place models are served from, hosted (Anthropic, OpenAI, xAI, OpenRouter) or local (Ollama, LM Studio, vLLM). A hosted Provider needs a key; a local one needs only an address.
_Avoid_: backend, vendor, endpoint

**Chain**:
The ordered list of models that serve one Role. The first that answers serves the call; the rest are its fallbacks.
_Avoid_: fallback list, route

**Profile**:
A named combination of Chains, one per Role it covers. Exactly one Profile is active; a Role the active Profile leaves out keeps its default from the box's configuration.
_Avoid_: preset, config, setup

**Profile version**:
One saved state of a Profile. Every edit makes a new version, and reverting means making an earlier version current again. A journaled decision names the Profile version that was active and the model that actually answered.
_Avoid_: revision, snapshot

**Probe**:
One call to a model in its Role's real contract, made before the model may serve that Role. A Chain whose first model fails its Probe is not saved.
_Avoid_: health check, ping, test call

**Spend cap**:
A daily dollar limit on one Role, whichever Profile is active. When a Role reaches it, its paid models are skipped for the rest of the day and the next model in the Chain serves; the wakeup is never blocked.
_Avoid_: budget, quota

**Local model**:
A model served by a local Provider. It costs nothing to call, whatever its license; the same open-weight model on a hosted Provider is paid.
_Avoid_: free model, open-source model


### Bots

**Bot**:
One trading strategy run by the agent: the symbols it trades, the prompt and triggers it wakes on, the Sources it reads, and its Sleeve. Every Draft, order and approval belongs to exactly one Bot. The agent that ran before Bots existed is Bot #1.
_Avoid_: strategy (a Bot carries one), agent, sleeve

**Bot version**:
One saved state of a Bot. Every edit makes a new version; a journaled decision names the Bot version that made it, so the decision can be replayed with the same prompt and Sources.
_Avoid_: revision, config

**Symbol mode**:
How a Bot treats one of its symbols: replay (scored only against history), shadow (decides and journals, never orders) or live (orders through RiskGuard). A symbol moves from replay to shadow to live only through the decide-change gate.
_Avoid_: stage, status

**Owner**:
The one Bot that is live on a symbol. Other Bots may shadow that symbol but may not order it.
_Avoid_: holder

**Sleeve**:
The share of the broker account that one Bot trades with: a fixed dollar amount of capital and the positions that Bot's own fills built. A Bot sizes against its Sleeve and can only reduce or close what its Sleeve holds. The Sleeves together add up to the account.
_Avoid_: sub-account, budget (a budget is a Spend cap), allocation

**Account halt**:
A breaker trip or a position drift on the whole account. It stops every Bot from opening; exits stay managed. A Bot's own breaker or pause stops only that Bot.
_Avoid_: global kill, panic

**Source**:
A body of information a Bot reads when it decides: the Macro View, a brief, sentiment, an economic series, a held position. A Bot subscribes to the Sources it wants. A Source only reads what has already been produced; it never fetches into storage on the live path.
_Avoid_: feed, lane (a lane is one section of the prompt), input

**Source version**:
The exact state of a Source a decision read. Every decision records one per Source it used, so replay reads the same information.
_Avoid_: snapshot
