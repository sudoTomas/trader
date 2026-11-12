# Confirm as Jev Judgments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the confirm role's decision-shaped Jev questions with four atomic Noul judgments over structured state, and let a code-side rule veto weak opens (live and in replay).

**Architecture:** Question wording, thresholds, the veto rule and the state builder live together in a new module `src/trader/loop/confirm_questions.py`. `Confirmer` (src/trader/loop/confirm.py) builds state, asks Jev once per non-hold Draft through the registry (with a new structured `state` pass-through that only `JevClient` accepts), evaluates the rule, and returns the journal record. The runner and the replay walk turn a vetoed open into a hold before the desk/RiskGuard.

**Tech Stack:** Python 3.12, httpx, psycopg, pytest, typer/FastAPI/MCP surfaces (existing).

**Spec:** `docs/superpowers/specs/2026-11-12-confirm-jev-judgments-design.md` (read it first; it is the source of every value below).

## Global Constraints

- CLAUDE.md invariants: one service layer then API + MCP + CLI; RiskGuard untouched; append-only audit; never send `chat_template_kwargs` to a vendor.
- Jev never decides: no question asks what the agent should do.
- A failed Jev call or malformed answer is `skipped` with the reason and never vetoes.
- Closes are never vetoed.
- Thresholds (verbatim): veto an open when `grounded` < 0.25 (`ungrounded`); `news_against` > 0.80 (`news_against`); `catalyst` < 0.25 and `news_against` > 0.50 (`generic_and_contested`). First matching row names the reason; all rows are evaluated and recorded.
- `news_against` is not asked when `research` is empty; rows that use it then never fire.
- Setting `TRADER_CONFIRM_VETO_ENABLED`, default `true`.
- `questions_version` = `"2026-11-12.1"`.
- Commit messages end with `Claude-Session: https://claude.ai/code/session_01UDNLNu854ULHbHSL59UJKu`.
- Tests: `uv run pytest -q` (needs the compose Postgres on :5544 or a throwaway one) and `uv run ruff check . && uv run ruff format --check .`.

## Review Focus

- Jev answers missing a question, or a `noul` outside [0, 1] or NaN → `skipped`, no veto (Task 4).
- Empty research (no brief, sentiment, sources or signals for the symbol) → `news_against` absent from the request and the record; only `grounded` can veto (Tasks 2, 4).
- An open while the opposite side is held (a flip) or the same side (an add) → treated as an open: vetoable, `position` present in state (Tasks 2, 5).
- Jev slow or unreachable → the loop proceeds with the Draft unchanged within the client's 30 s timeout (Task 5).
- Blind replay → confirm skipped and recorded as `blind`, no symbol/date/dollar text reaches Jev (Task 6).

---

### Task 0: Base on the merged replay `--model` work

PR #158 (`feat/replay-provider-model`) touches `src/trader/llm/registry.py`, `src/trader/eval/replay.py` and `src/trader/surfaces/services.py`.

- [ ] **Step 1:** Once #158 is merged to `main`, `git fetch && git rebase origin/main` on `feat/confirm-jev-judgments`. If it is not merged yet, stop and ask; do not build on the unmerged branch.
- [ ] **Step 2:** `uv run pytest -q` → all pass before any change.

### Task 1: Jev answers Nouls and takes structured state

**Files:**
- Modify: `src/trader/llm/jev.py` (`QUESTION_TYPES`, `JevClient.complete`)
- Modify: `src/trader/llm/registry.py` (`LLMRegistry.complete`, and the pinned registry from #158 if it has its own path)
- Test: `tests/test_jev.py`

**Interfaces:**
- Produces: `JevClient.complete(messages, *, schema, max_tokens=1024, temperature=0.2, state: dict[str, Any] | None = None) -> Completion`. With `state`, the request body's `state` is that dict verbatim and `messages` are ignored. Completion text = JSON of `{name: answer_object}` as today; `completion.model` = the response's `model` field when present.
- Produces: `LLMRegistry.complete(role, messages, *, schema=None, max_tokens=1024, temperature=0.2, state: dict[str, Any] | None = None)`. When `state` is given, it is passed only to clients whose `name == "jev"` (or that are `JevClient` instances); any other client in the chain is recorded as an error `"<name>:<model> takes no structured state"` and skipped, never called.
- `QUESTION_TYPES = frozenset({"choice", "score", "noul"})`.

- [ ] **Step 1: Write the failing tests** in `tests/test_jev.py`:
  - `test_a_noul_question_set_is_accepted_and_answered`: mock transport returns `{"model": "jev-1.15", "answers": {"grounded": {"type": "noul", "noul": 0.91}}, "usage": {...}}`; assert the request body's `questions["grounded"]["type"] == "noul"` and `json.loads(completion.text)["grounded"]["noul"] == 0.91`, `completion.model == "jev-1.15"`.
  - `test_structured_state_is_sent_verbatim`: `state={"draft": {"action": "open"}}` → request body `state == {"draft": {"action": "open"}}`.
  - `test_registry_passes_state_only_to_jev`: a chain `[fake_openai_client, jev_client]` with `state=...` → the fake is never called, the error list names it, Jev answers.
- [ ] **Step 2:** `uv run pytest tests/test_jev.py -q` → the three new tests FAIL.
- [ ] **Step 3:** Implement per Interfaces. Keep the old `{"prompt","rules"}` path when `state is None` (other callers, e.g. the role probe, still use it).
- [ ] **Step 4:** `uv run pytest tests/test_jev.py tests/test_role_probe.py tests/test_profiles.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(jev): Noul questions and structured state`.

### Task 2: Questions, rule and state in one module

**Files:**
- Create: `src/trader/loop/confirm_questions.py`
- Test: `tests/test_confirm_questions.py`

**Interfaces:**
- Produces:
  - `QUESTIONS_VERSION = "2026-11-12.1"`
  - `GROUNDED_MIN = 0.25`, `NEWS_AGAINST_MAX = 0.80`, `CATALYST_MIN = 0.25`, `CONTESTED_NEWS_MIN = 0.50`
  - `@dataclass(frozen=True) class PositionState: side: str  # "long" | "short"; quantity: float; summary: str | None; opened_at: datetime | None; open_rationale: str | None`
  - `def build_state(ctx: DecisionContext, decision: Decision, position: PositionState | None) -> dict[str, Any]` → keys `symbol`, `draft`, `market`, optional `research`, optional `position` (spec "State"). `draft` = `{action, side, rationale, conviction}` (no notional). `research` omitted entirely when every lane is empty; empty lanes omitted individually.
  - `def questions_for(action: str, *, has_research: bool, has_open_rationale: bool) -> dict[str, dict[str, Any]]` → Noul questions keyed `grounded`, `catalyst`, `news_against` (only if `has_research`), `thesis_broken` (only if `action == "close"` and `has_open_rationale`). Each `{"type": "noul", "instructions": str, "criteria": {"true": str, "false": str}}`; instructions name state paths in backticks (e.g. `` `draft.rationale` ``); `false` criteria are concrete situations (spec).
  - `@dataclass(frozen=True) class RuleResult: would_veto: bool; reason: str | None; rows: list[dict[str, Any]]  # [{"reason": str, "fired": bool}]`
  - `def evaluate(answers: dict[str, float], *, action: str) -> RuleResult` — for `action != "open"` returns `would_veto=False, reason=None` with rows all `fired: False`. Missing `news_against` → rows using it are `fired: False`.

- [ ] **Step 1: Write the failing tests**:
  - `evaluate`: `{"grounded": 0.24, "catalyst": 0.9, "news_against": 0.1}` → `("ungrounded", True)`; `grounded 0.25` → not vetoed (strict `<`); `news_against 0.81` → `news_against`; `0.80` → pass; `catalyst 0.24, news_against 0.51` → `generic_and_contested`; `catalyst 0.24, news_against 0.50` → pass; both `ungrounded` and `news_against` fire → reason `ungrounded`, both rows `fired: True`; no `news_against` key with `catalyst 0.1` → pass; `action="close"` with `grounded 0.0` → `would_veto False`.
  - `questions_for("open", has_research=False, has_open_rationale=False)` keys == `{"grounded", "catalyst"}`; `("close", True, True)` keys == all four; every question `type == "noul"` and has both criteria; no instruction contains "should".
  - `build_state`: with a context fixture holding no brief/sentiment/sources/signals → `"research" not in state`; `state["draft"]` has no `notional` key; with `PositionState(...)` → `state["position"]["open_rationale"]` set.
- [ ] **Step 2:** run → FAIL (module missing).
- [ ] **Step 3:** Implement. `market` = `{"price": ctx.price, "indicators": {interval: dataclasses.asdict(ind)}}` unless a field is not plain data; map research slices to text by each slice's existing render. Word the questions per the spec's plain meanings.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `feat(confirm): Jev judgment questions, veto rule and state`.

### Task 3: Find the rationale that opened a position

**Files:**
- Modify: `src/trader/store/repos.py` (`DecisionsRepo`), `src/trader/loop/journal.py` (`Journal`)
- Test: the existing decisions/journal test module (grep `DecisionsRepo` in tests; add there)

**Interfaces:**
- Produces: `Journal.last_open(symbol: str, *, bot_id: int | None) -> tuple[str, datetime] | None` — rationale and `ts` of the most recent `status = 'executed'` decision for that symbol and bot whose `decision->>'action' = 'open'`.

- [ ] **Step 1:** Tests: two executed opens → the newer's rationale; an executed close after it does not count; another bot's open is ignored; none → `None`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement (one SQL query; uses `decisions_symbol_ts_idx`). **Step 4:** run → PASS.
- [ ] **Step 5: Commit** `feat(journal): the decision that opened a position`.

### Task 4: Confirmer asks the judgments and returns the new record

**Files:**
- Modify: `src/trader/loop/confirm.py` (replace `QUESTIONS`, `Confirmer`, record shape; keep `draft_of`, `skipped`)
- Modify: `src/trader/ops/config.py` (new `confirm_veto_enabled`; update `confirm_provider` description, which still says "Journal-only")
- Modify: `docs/configuration.md` (the setting) and the confirm paragraph in `docs/architecture.md`
- Test: `tests/test_confirm.py`

**Interfaces:**
- Consumes: Task 1 `LLMRegistry.complete(..., state=)`; Task 2 everything.
- Produces: `Confirmer(llm, *, veto_enabled: bool)`; `await Confirmer.confirm(ctx, decision, position: PositionState | None = None) -> dict | None`. Record keys: `draft`, `answers` (id → p), `rule` (`RuleResult.rows`), `would_veto`, `veto_reason`, `vetoed` (= would_veto and veto_enabled and action open), `verdict` (`"veto"` | `"pass"` | `"recorded"` for closes), `questions_version`, `model`, `latency_ms`, `skipped` (None). `skipped(...)` records keep today's shape plus `vetoed: False`.
- Setting: `confirm_veto_enabled: bool = setting(True, ...)` → env `TRADER_CONFIRM_VETO_ENABLED`, description: "Veto a proposed open when the confirm role's judgments fail the rule in loop/confirm_questions.py. Off: still asked and recorded as would_veto."

- [ ] **Step 1:** Tests (fake registry returning Noul answers):
  - an open with grounded 0.1 and `veto_enabled=True` → `vetoed True`, `verdict "veto"`, `veto_reason "ungrounded"`; same with `veto_enabled=False` → `would_veto True`, `vetoed False`, `verdict "pass"`.
  - a close with grounded 0.0 → `verdict "recorded"`, `vetoed False`.
  - the registry received `state=` (not messages) containing `draft.rationale`, and the question keys from `questions_for`.
  - Review Focus: answers missing `catalyst` → `skipped` startswith `"malformed answers"`, `vetoed False`; `noul` = 1.7 or NaN → `skipped`; `NoRoute` → `None`; `LLMError("jev: HTTP 500")` → `skipped == "HTTP 500"`.
  - golden: one open's full request (`state` + `questions`) matches a checked-in JSON snapshot under `tests/golden/` (follow the repo's existing golden-file helper).
  - settings: `Settings()` default `confirm_veto_enabled is True`; env `TRADER_CONFIRM_VETO_ENABLED=false` → False; the running-config view lists it.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_confirm.py tests/test_config*.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(confirm): atomic Jev judgments and the open veto`.

### Task 5: The live loop applies the veto

**Files:**
- Modify: `src/trader/loop/runner.py` (the `self.confirm` block, ~L900), `src/trader/surfaces/api/app.py` (~L1005 `Confirmer(llm)` → `Confirmer(llm, veto_enabled=settings.confirm_veto_enabled)`)
- Test: `tests/test_confirm.py` (runner section; reuse whatever runner fixture the existing confirm tests use)

**Interfaces:**
- Consumes: Task 3 `Journal.last_open`; Task 4 `Confirmer.confirm(ctx, decision, position)`.
- The runner builds `PositionState` when `ctx.position_qty != 0` (side from the sign, `quantity=abs(qty)`, `summary` = joined `ctx.held_position.entries` or None, `opened_at`/`open_rationale` from `last_open(symbol.ticker, bot_id=self.bot_id)`).
- On `confirm["vetoed"]`: `decision = Decision(symbol=decision.symbol, action=Action.HOLD, conviction=decision.conviction, rationale=f"confirm veto: {confirm['veto_reason']}")`, before the desk review; the original Draft is already in `confirm["draft"]`.

- [ ] **Step 1:** Tests: vetoed open → `guard.evaluate` never called, journaled decision action `hold` with rationale `"confirm veto: ungrounded"` and `confirm["vetoed"] is True`; veto disabled → open reaches the guard and `confirm["would_veto"] is True`; a close is never replaced; a held position passes `PositionState` with `open_rationale` from the journal; a Confirmer raising → loop continues, `confirm["skipped"]` set (existing test still passes); an add (open with same-side position) can be vetoed and its state has `position`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → all PASS.
- [ ] **Step 5: Commit** `feat(loop): a vetoed open becomes a hold before review`.

### Task 6: Replays run the confirm stage

**Files:**
- Modify: `src/trader/eval/replay.py` (`ReplayHarness.__init__`, `create_run`, `_walk` after `self._decider.decide`, before desk review)
- Modify: `src/trader/surfaces/services.py` (`run_replay_eval(..., confirm_veto: bool = False)`), `src/trader/surfaces/api/app.py` (`/eval/replay` query `confirm_veto`), `src/trader/surfaces/cli/main.py` (`trader eval run --confirm-veto/--no-confirm-veto`); MCP follows the service signature
- Modify: `docs/runbooks/decide-change-gate.md` (how to run the paired veto check)
- Test: `tests/test_replay_eval.py`, `tests/test_blind_replay.py`

**Interfaces:**
- `ReplayHarness` constructs `Confirmer(llm, veto_enabled=config.get("confirm_veto", False))` per run; a `NoRoute` from the registry means no confirm stage (record nothing).
- Replay `PositionState`: from the walk's own book (side/qty) and the in-memory rationale + `as_of` of the step that opened it.
- The step's confirm record is stored under the step's `decision` JSON as `decision["confirm"]` (no migration). Run `config["confirm_veto"]` is always written (bool). Blind runs: no Jev call; step `decision["confirm"] = {"skipped": "blind"}`.

- [ ] **Step 1:** Tests: a replay with a confirm route and `confirm_veto=True` turns the same open live would veto into a hold (book stays flat at that step); `confirm_veto=False` keeps the open and records `would_veto`; a run without a confirm route records no confirm; a blind run makes zero Jev calls and records `skipped: "blind"`; API/CLI pass `confirm_veto` through.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(eval): replays ask Jev and can apply the veto`.

### Task 7: Rollout (operator, after merge and deploy)

- [ ] **Step 1:** Before the deploy restart, set `TRADER_CONFIRM_VETO_ENABLED=false` in the Spark `.env` (back it up first). Check `date` and the NYSE session; restart only outside market hours.
- [ ] **Step 2:** State the Jev request count and cost for the check, then run, for each Bot #1 symbol over the last ~2 weeks: `trader eval run --symbol <S> --start <T0> --end <T1> --label veto-off --no-confirm-veto` and the same with `--confirm-veto --label veto-on`.
- [ ] **Step 3:** Report to Tomas: opens vetoed per symbol, by reason, and the net P&L difference; read a sample of vetoed rationales against their state (TypeSafe: read individual answers).
- [ ] **Step 4:** On his OK, set `TRADER_CONFIRM_VETO_ENABLED=true` and restart outside market hours; confirm the first live record has `questions_version "2026-11-12.1"`.
