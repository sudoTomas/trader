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

