# Confirm as Jev judgments, with a veto on opens

Date: 2026-11-12. Status: approved in conversation, pending this document.

## Why

The confirm role asks TypeSafe's Jev (hosted `jev:jev-latest` since 2026-11-12) one
decision-shaped question, "what should the agent do now: hold, open or close?", plus a side
question, over the whole decide prompt as one text blob (`src/trader/loop/confirm.py`,
`QUESTIONS`). The TypeSafe integration guidance (skill `typesafe-ai`, plugin
`typesafe@typesafe-ai`; live docs at https://docs.typesafe.ai/llms.txt) calls that the wrong
layer: Jev should answer small, observable judgments and code should hold the decision.

Tomas wants the confirm role to **veto weak trades**: when Jev's judgments say a proposed open
is weak, code turns it into a hold before the critic and RiskGuard see it. He chose to switch the
veto on **right away**, not after a journal-only period.

## Goals

- Replace the decision-shaped questions with atomic judgments Jev answers about named state fields.
- A deterministic veto rule in code, next to the question wording, that can turn a proposed
  **open** into a hold.
- Closes are judged and recorded but **never vetoed**.
- Every judgment, the rule's outcome and the rule that fired are journaled in the decision's
  `confirm` column.
- A settings switch turns the veto off without a deploy; with it off, everything is still asked
  and recorded ("would veto").
- **Replays run the confirm stage** (Tomas's choice): a replay with a confirm route asks Jev
  and applies the veto exactly as live does, so a threshold or question change can be tested by
  replaying with and without it. Blind replays (#140) skip confirm and record that they did.
- Before the veto is enabled on the Spark, a check replays roughly the last two weeks for Bot #1's
  symbols twice, veto off and veto on, and reports how many opens were vetoed and the P&L
  difference. This informs Tomas; it is not a gate written into code.

## Non-goals

- No change to RiskGuard, sizing, the stop-loss, or the critic.
- No veto of closes (a wrong close-veto holds a loser; the stop-loss is the only backstop).
- No position sizing from Jev's answers.
- No new Jev Provider plumbing: the `jev` Provider and `JevClient` exist (`src/trader/llm/jev.py`).

## Anti-goals (a failure even if it works)

- A failed Jev call or malformed answer treated as "veto" or as "pass with judgment". A failure
  is `skipped` with the reason, the Draft proceeds unchanged, exactly as today.
- Jev asked to decide ("should the agent open?") anywhere.
- Arithmetic, trend direction or indicator thresholds asked of Jev: those are code.
- Thresholds or criteria tuned to specific past trades (TypeSafe's "prompt gymnastics").

## Decided by Tomas

**Purpose:** veto weak trades. **Rollout:** veto on immediately. **Scope:** opens only; closes
recorded.

**The judgments** (one Jev request per non-hold Draft, questions bundled because they share state):

| id | Asked on | Type | Plain meaning |
|---|---|---|---|
| `grounded` | open, close | Noul | Every number and fact stated in `draft.rationale` appears in `market` or `research`. |
| `news_against` | open, close | Noul | `research` gives a reason to expect the symbol to move opposite to `draft.side`. Not asked when `research` is empty; code treats it as neutral (0.5 is NOT used; the rule simply skips news terms). |
| `catalyst` | open, close | Noul | `draft.rationale` names a specific signal or event behind the trade, rather than generic language ("momentum looks good"). |
| `thesis_broken` | close only | Noul | Something in `market` or `research` since `position.opened_at` contradicts `position.open_rationale`. |

Noul `false` criteria are written as concrete situations (TypeSafe guidance), e.g. for
`grounded`: "the rationale cites a number or event that `market` and `research` do not show".

**The veto rule for opens** (probabilities are Jev's calibrated p(true)):

| Veto when | Reason code |
|---|---|
| `grounded` < 0.25 | `ungrounded` |
| `news_against` > 0.80 | `news_against` |
| `catalyst` < 0.25 and `news_against` > 0.50 | `generic_and_contested` |

Anything else passes. First matching row wins for the recorded reason, but all rows are
evaluated and recorded. Thresholds live as named constants beside the questions.

## State (my call, per TypeSafe "design the state")

Named JSON fields, not the decide prompt string:

- `symbol`: ticker and asset class.
- `draft`: action, side, rationale (the decide model's), conviction. Not notional: sizing is
  code's, and dollar amounts invite irrelevant judgments.
- `market`: last price, and per interval the indicator values the decide prompt shows
  (from `DecisionContext.indicators`), labelled with plain names. Recent bars only if the
  indicators don't already carry what the rationale cites; the builder decides after checking
  what rationales actually cite.
- `research`: the symbol-scoped brief, sentiment and news/source lanes from `DecisionContext`
  (`brief`, `sentiment`, `sources`, `signal_lanes`), as named text fields with their dates.
  Omit empty lanes (an empty list can read as a signal). Macro view only if symbol-relevant.
- `position` (closes only): side, quantity, entry price, `opened_at`, and `open_rationale`,
  the rationale of the decision that opened it, read from the journal. `HeldPositionSlice`
  does not carry it today; the builder adds the lookup.

Apply the human test: a smart person shown only this state and one question could answer it
quickly. Drop any field no question needs.

## Where it runs

Unchanged position in the loop (`src/trader/loop/runner.py`, the `self.confirm` block): after
decide, before the desk review and RiskGuard. When the rule vetoes an open and the switch is on,
the runner replaces the Draft with a hold decision whose rationale names the veto reason; the
original Draft stays in the confirm record. It applies to shadow symbols the same way (journaled
as shadow). Replay (`src/trader/eval/replay.py`) has no confirm stage today; this change adds one
(see "Calls made after the builder check").

## Journal record (the `confirm` column)

Keep the existing envelope (`draft`, `model`, `latency_ms`, `skipped`) and replace
`action`/`side`/`p_same_action`/`prefers`/`margin` with: `answers` (id to p(true)),
`rule` (the rows and whether each fired), `would_veto` (bool), `veto_reason` (code or null),
`vetoed` (bool: would_veto and switch on and action is open), and a `questions_version` string
so later analysis can tell question sets apart. `verdict` becomes `veto`, `pass` or
`recorded` (closes). Old rows keep their shape; readers must tolerate both.

## Settings

`TRADER_CONFIRM_VETO_ENABLED` (default **true**, per Tomas) in `src/trader/ops/config.py`,
shown by the running-config view (`GET /config`, MCP `get_config`, `trader config show`).
No other new setting.

## Surfaces

The veto changes the money path, so per CLAUDE.md: the confirm record must be visible wherever
decisions are (existing journal views), and the switch must appear on all three config surfaces.
No new command is required.

## Testing (TDD at agreed seams)

- `Confirmer` with a fake Jev transport: correct questions per action (open vs close),
  `news_against` omitted with empty research, state field names, the rule table (each row, the
  boundaries, the both-firing case), failure paths yield `skipped` and never a veto.
- Runner: a vetoed open becomes a hold before RiskGuard is called; switch off records
  `would_veto` and lets the open through; a close is never vetoed; a confirm exception still
  never stops the loop.
- Replay: a replay with a confirm route vetoes the same open live would; a veto-off replay
  records would_veto only; a blind replay skips confirm.
- Golden/state snapshot of one rendered request payload.

## Live API probing (before enabling on the Spark)

TypeSafe guidance: ask permission, set a budget, disclose the data. Data sent: market indicators,
research text and the decide model's rationale for this paper-trading account; no personal data.
Tomas has approved the two-week check in principle; the builder still states the request count
and expected cost before running it. Keep each exact payload so results can be rerun.

## Calls made after the builder check (mine, not Tomas's)

- **Rollout order.** Code default for `TRADER_CONFIRM_VETO_ENABLED` is true. At the first
  Spark deploy the Spark `.env` sets it to `false`; the two-week replay check runs; Tomas sees the
  result; then it is flipped to `true`. The operator (Claude) runs the check and states the Jev
  request count and cost first.
- **Structured state to Jev.** `JevClient.complete` today builds `{"prompt", "rules"}` from the
  chat messages. Add an optional structured-state path: the registry's `complete` accepts an
  optional `state: dict` keyword that `JevClient` sends as the request's state and other clients
  refuse (the confirm Role's kinds are Jev only). The old `QUESTIONS`, `prompt`/`rules` state go.
- **No recoverable opening rationale** (manual order, other bot, pre-journal position, lookup
  miss): omit `thesis_broken`, ask the rest, and record `open_rationale: null`. The lookup is the
  most recent executed open decision for the same symbol and `bot_id`.
- **Adds count as opens.** An open while a same-side position is held is vetoable, and
  `position` is included in its state (without `open_rationale` questions).
- **Journal status of a vetoed open.** It is recorded through the existing hold path (status as
  any hold), with reason `confirm veto: <reason code>` and `confirm.vetoed = true`, so views can
  tell a veto from a model hold without a new status value.
- **Replay wiring.** `ReplayHarness` gets the Confirmer when a confirm route exists (the process
  registry or a pinned one); replay `config` records `confirm_veto` on/off so paired runs can be
  compared; the veto switch for a replay is a replay parameter, not the live setting.

## Left to the builder

- Exact Noul payload shape (`type` value, criteria fields): read
  https://api.typesafe.ai/openapi.json and https://docs.typesafe.ai/go/api.md; extend
  `JevClient` only if it can't send Nouls today.
- Wording of each question and criterion within the plain meanings above.
- How bars/indicators are labelled in `market`.
- The two-week check's script (a one-off under `scripts/` or an eval command).
