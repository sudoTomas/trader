# Telegram Channel Calls Scorecard (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the Chart Chasers ALERTS Telegram channel from the Spark, parse each post into a
call, an update or other, and score every call (backfill and live) by simulating the Phase 2
exit ladder against Alpaca crypto bars, on all three surfaces and the desk. No orders.

**Architecture:** A new package `trader.calls` holds four focused modules: a Telethon-backed
channel reader behind a small Protocol, a poller that stores posts and their edit versions, a
parser that asks the new model-picker Role `calls` for validated JSON, and a pure scorer that
walks 15-minute bars. Three tables (`channel_posts`, `call_parses`, `call_scores`) hold the
results; one service capability per view wires API, MCP and CLI. The poller runs as an in-process
task started in the app lifespan, like the settlement loop.

**Tech Stack:** Python 3.12, asyncio, psycopg (async), Telethon (new optional extra `telegram`),
pydantic v2, FastAPI, typer, Electron desk (vanilla JS).

**Spec:** `docs/superpowers/specs/2026-11-12-telegram-signals-bot-design.md` (read it first; this
plan implements Phase 1 only).

## Global Constraints

- Phase 1 places no orders: nothing in `trader.calls` imports the executor, the broker client or RiskGuard.
- Read-only on Telegram: never send, react, or mark read (`client.send_read_acknowledge` and any `send_*` are forbidden). Only the configured channel is read.
- The Telegram session is a full-account credential: stored only Fernet-encrypted with `TRADER_SECRETS_KEY`; never logged, journaled, put in a prompt, returned by any surface, or written to `.env`.
- No market-data storage in the live path: bars are fetched on demand for scoring; `call_scores` stores results, not bars.
- One service layer, three surfaces: every capability is a `services.py` registry entry, an API route and a CLI command (MCP is automatic).
- Scoring rules (verbatim from the spec): entry = market at the open of the next 15-minute bar after the post; stop = the tighter of the call's stop and `TRADER_RISK_STOP_LOSS_FRAC` (fixed, not vol-tightened), 8% when the call has none; a third off at each stated TP; stop to break-even after TP1 or when an update says so; close the rest on a "closed" update; a bar touching both a TP and the stop counts the stop first; costs from `costs_from_settings` (#139).
- Backfilled posts are parsed **levels only** (outcome text added by later edits is ignored); live posts use their first-seen text; edits are stored as new versions and never re-parse a live call's levels.
- Gate figures (reported, not enforced): tradeable = kind `call`, side `long`, Alpaca has bars for `<COIN>/USD`. Sleeve $10,000; size per call = sleeve × allocation × leverage (allocation 1% and leverage 1 when the call omits them). `gate_pass` = backfill + ≥ 28 live days, sleeve-weighted net P&L > 0, and ≥ half of tradeable calls reach TP1 before the stop.
- Settings go through `setting(...)` in `src/trader/ops/config.py`, section `"Telegram calls"` (add it to `SECTIONS`); regenerate `docs/configuration.md` and `.env.example` with `scripts/gen_config_reference.py`.
- Tests run with `PGTZ=UTC TZ=UTC`; new tables go in `tests/conftest.py` `ALL_TABLES`. No test touches the network: Telegram, the LLM and bars are faked.
- Implementer subagents are Opus or better.

## Review Focus

1. A post edited after it was parsed: the poller stores a new version; the call's levels stay those of the first parse (Task 3 test `test_edit_adds_a_version_and_keeps_the_first_parse`).
2. An update that replies to another update or to commentary: linking walks the reply chain up to the call; with no reply, it links to the newest call on the same coin within 30 days, else stays unlinked (Task 5 tests).
3. A call on a coin with no Alpaca bars, or posted before the bars start: scored `unscorable` with a reason; the scorecard never fails because of one call (Task 6 test `test_unscorable_call_does_not_break_the_scorecard`).
4. Telegram errors mid-poll (FloodWait, disconnect): the loop logs, waits for the next interval, and a re-poll inserts no duplicate rows (Task 3 test `test_poll_is_idempotent_after_a_failure`).
5. A parse with inconsistent levels (long with a TP below entry, stop above entry, TPs out of order): validation drops the bad levels and records a `flags` list instead of guessing; the scorer then uses what is left (Task 4 test `test_inconsistent_levels_are_dropped_and_flagged`).

## File Structure

- `src/trader/store/migrations/0021_channel_calls.sql`: tables `channel_sessions`, `channel_posts`, `call_parses`, `call_scores`.
- `src/trader/store/repos.py`: `ChannelSessionsRepo`, `ChannelPostsRepo`, `CallParsesRepo`, `CallScoresRepo`.
- `src/trader/calls/__init__.py`
- `src/trader/calls/reader.py`: `RawPost`, `ChannelReader` Protocol, `TelethonReader`, session encryption helpers.
- `src/trader/calls/poller.py`: `ChannelPoller` (poll, backfill, run_forever).
- `src/trader/calls/parse.py`: pydantic schema, `CallParser`.
- `src/trader/calls/link.py`: `link_updates`.
- `src/trader/calls/score.py`: `simulate`, `CallScore`, `scorecard`.
- `src/trader/calls/desk.py`: `CallsDesk` (the orchestration the services call).
- `src/trader/llm/{registry,caps,profiles,probe}.py`: Role `calls`.
- `src/trader/data/alpaca.py`, `src/trader/data/composite.py`: `get_bars_between`.
- `src/trader/ops/config.py`: settings.
- `src/trader/surfaces/{services.py,api/app.py,cli/main.py}`: capabilities.
- `clients/desk/{renderer.js,desk_logic.js,desk_logic.test.js}`: Calls panel.
- `CONTEXT.md`: glossary terms **Channel call**, **Call update**, **Call scorecard**.

---

### Task 1: Tables, repos, settings, dependency

**Files:**
- Create: `src/trader/store/migrations/0021_channel_calls.sql`
- Modify: `src/trader/store/repos.py`, `src/trader/ops/config.py`, `pyproject.toml`, `tests/conftest.py`, `docs/configuration.md`, `.env.example`
- Test: `tests/test_calls_store.py`

**Interfaces:**
- Produces:
  - Table `channel_sessions(name text PRIMARY KEY, api_id bigint NOT NULL, api_hash_enc text NOT NULL, session_enc text NOT NULL, created_at timestamptz DEFAULT now())`.
  - Table `channel_posts(channel_id bigint, msg_id bigint, version int, posted_at timestamptz NOT NULL, edited_at timestamptz, seen_at timestamptz NOT NULL DEFAULT now(), reply_to bigint, text text NOT NULL DEFAULT '', media text, backfilled bool NOT NULL, PRIMARY KEY (channel_id, msg_id, version))`.
  - Table `call_parses(id bigint identity PK, channel_id bigint, msg_id bigint, version int, kind text NOT NULL CHECK (kind IN ('call','update','other','unparsed')), payload jsonb NOT NULL, links_to bigint, model text, profile_version int, cost_usd numeric(12,6), error text, created_at timestamptz DEFAULT now(), UNIQUE (channel_id, msg_id))` (one parse per post: the first one).
  - Table `call_scores(channel_id bigint, msg_id bigint, status text NOT NULL CHECK (status IN ('closed','open','unscorable')), tradeable bool NOT NULL, result jsonb NOT NULL, scored_at timestamptz DEFAULT now(), PRIMARY KEY (channel_id, msg_id))`.
  - `ChannelPostsRepo(db)`: `async upsert_version(post: RawPost, *, channel_id: int, backfilled: bool) -> bool` (inserts version n+1 only when text or edited_at differs from the latest version; returns True when it inserted); `async latest(channel_id, msg_id) -> Row | None`; `async first(channel_id, msg_id) -> Row | None`; `async unparsed(channel_id, limit=50) -> list[Row]` (first versions with no `call_parses` row, oldest first); `async listing(channel_id, *, limit=50, before: int | None = None) -> list[Row]`; `async max_msg_id(channel_id) -> int`.
  - `CallParsesRepo(db)`: `async add(*, channel_id, msg_id, version, kind, payload, model, profile_version, cost_usd, error) -> int`; `async set_link(channel_id, msg_id, links_to: int | None)`; `async calls(channel_id) -> list[Row]` (kind call, joined with the first post's `posted_at` and `backfilled`); `async updates_for(channel_id, call_msg_id) -> list[Row]` (joined with `posted_at`, ordered); `async get(channel_id, msg_id) -> Row | None`.
  - `CallScoresRepo(db)`: `async put(channel_id, msg_id, *, status, tradeable, result)` (upsert); `async all(channel_id) -> list[Row]`.
  - `ChannelSessionsRepo(db)`: `async put(name, *, api_id, api_hash_enc, session_enc)`; `async get(name) -> Row | None`.
  - Settings (section "Telegram calls"): `calls_enabled: bool = False` (GATE), `calls_channel_id: int = -1003097376178` (TUNING), `calls_poll_seconds: float = 300.0` (TUNING), `calls_recheck_posts: int = 50` (TUNING, how many newest posts each poll re-reads for edits), `calls_session_name: str = "spark"` (TUNING), `calls_sleeve_usd: float = 10_000.0` (TUNING).
  - `pyproject.toml`: optional extra `telegram = ["telethon>=1.40"]`.

- [ ] **Step 1: Write failing tests** in `tests/test_calls_store.py` using the `db` fixture:
  - `test_upsert_version_inserts_once_and_versions_edits`: upsert the same RawPost twice → second returns False, one row; upsert with changed `text` → returns True, `latest(...).version == 2`, `first(...).text` is the original.
  - `test_unparsed_lists_first_versions_without_a_parse`: two posts, parse one → `unparsed` returns only the other.
  - `test_one_parse_per_post`: a second `CallParsesRepo.add` for the same post raises `psycopg.errors.UniqueViolation`.
  - `test_scores_upsert`: `put` twice → one row with the second `status`.
- [ ] **Step 2: Run** `PGTZ=UTC TZ=UTC uv run pytest -q tests/test_calls_store.py` → FAIL (no table / no repo).
- [ ] **Step 3: Implement** the migration (leading `--` comment naming the spec), the four repos (same style as `VendorReadsRepo`), the settings and the section, the extra, and add the four tables to `ALL_TABLES` in FK-safe order. `RawPost` is defined in Task 2; for this task define it in `src/trader/calls/reader.py` already (Task 2 adds the rest of that module):
  `@dataclass(frozen=True) class RawPost: msg_id: int; posted_at: datetime; edited_at: datetime | None; reply_to: int | None; text: str; media: str | None`.
- [ ] **Step 4: Regenerate docs:** `uv run python scripts/gen_config_reference.py`; run `PGTZ=UTC TZ=UTC uv run pytest -q tests/test_calls_store.py tests/test_config_reference.py tests/test_config_view.py` → PASS.
- [ ] **Step 5: Commit** `feat(calls): tables, repos and settings for Telegram channel calls`.

### Task 2: Channel reader and the Spark's Telegram login

**Files:**
- Modify: `src/trader/calls/reader.py`, `src/trader/surfaces/cli/main.py`
- Test: `tests/test_calls_reader.py`

**Interfaces:**
- Consumes: `ChannelSessionsRepo`, `RawPost`, `Settings.secrets_key`.
- Produces:
  - `class ChannelReader(Protocol)`: `async def newer(self, channel_id: int, min_id: int, limit: int = 200) -> list[RawPost]` (ascending by msg_id); `async def latest(self, channel_id: int, limit: int) -> list[RawPost]` (newest first, for edit checks); `async def history(self, channel_id: int) -> AsyncIterator[RawPost]` (every post, oldest first).
  - `class TelethonReader(ChannelReader)`: `__init__(self, api_id: int, api_hash: str, session: str)`, async context manager (`connect`/`disconnect`). Uses `iter_messages` only. Maps a message to `RawPost` (text = `message.message or ""`, media = type name or None, reply_to = `reply_to_msg_id`).
  - `def seal(secrets_key: str, plaintext: str) -> str` and `def unseal(secrets_key: str, token: str) -> str` (Fernet, same as `ProviderDesk._encrypt/_decrypt`).
  - `async def reader_from_store(db, settings) -> TelethonReader | None` (None when no session row for `settings.calls_session_name` or `secrets_key` is empty).
  - CLI (local, not via the API, like `trader xai login`): `trader telegram login --name spark` prompts for `api_id` and `api_hash` (hidden), runs Telethon `qr_login()` printing the QR as ASCII (dependency `qrcode`, add to the `telegram` extra), handles 2FA with a hidden prompt, then stores api_id, `seal(api_hash)` and `seal(session)` via `ChannelSessionsRepo.put`. `trader telegram status` prints whether a session exists and the account's first name (never the session).
- [ ] **Step 1: Write failing tests:** `test_seal_round_trips_and_hides_plaintext` (token != plaintext; `unseal(seal(x)) == x`); `test_reader_from_store_is_none_without_a_session`; `test_message_to_rawpost_maps_fields` (build a `types.SimpleNamespace` shaped like a Telethon message and call the module's `_to_rawpost`); `test_reader_never_calls_send_or_read_ack` (grep-style test: the module source contains neither `send_` nor `read_acknowledge`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PGTZ=UTC TZ=UTC uv run pytest -q tests/test_calls_reader.py` → PASS.
- [ ] **Step 5: Commit** `feat(calls): read-only Telegram channel reader and QR login stored encrypted`.

### Task 3: Poller

**Files:**
- Create: `src/trader/calls/poller.py`
- Modify: `src/trader/surfaces/api/app.py` (lifespan task)
- Test: `tests/test_calls_poller.py`

**Interfaces:**
- Consumes: `ChannelReader`, `ChannelPostsRepo`, settings `calls_channel_id`, `calls_recheck_posts`, `calls_poll_seconds`, `calls_enabled`.
- Produces: `class ChannelPoller`: `__init__(self, db, reader: ChannelReader, *, channel_id: int, recheck: int, on_new: Callable[[], Awaitable[None]] | None = None)`; `async poll_once(self) -> dict[str, int]` returning `{"new": n, "edited": m}`; `async backfill(self) -> dict[str, int]` (stores every post from `history` with `backfilled=True`, idempotent); `async run_forever(self, interval_seconds: float, stopping: asyncio.Event) -> None` (pattern of `learning/settlement.py:run_forever`; calls `on_new` after a poll that stored anything). Started in `app.py` lifespan only when `calls_enabled` and `reader_from_store` returns a reader, as task name `trader-calls-poller`, stopped like the settlement task.
- [ ] **Step 1: Write failing tests** with a `FakeReader` (lists of RawPost; can raise once):
  - `test_poll_stores_new_posts_after_max_msg_id` (not backfilled).
  - `test_edit_adds_a_version_and_keeps_the_first_parse`: store post, add a parse row, return the same msg_id with new text from `latest` → `{"edited": 1}`, version 2 stored, `call_parses` unchanged.
  - `test_backfill_marks_posts_backfilled_and_is_idempotent` (run twice → same row count).
  - `test_poll_is_idempotent_after_a_failure`: reader raises on first `newer`; `run_forever` with a tiny interval and `stopping` set after the second pass → no exception escapes, rows equal a single clean poll.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `PGTZ=UTC TZ=UTC uv run pytest -q tests/test_calls_poller.py` → PASS.
- [ ] **Step 5: Commit** `feat(calls): channel poller with edit versions and backfill`.

### Task 4: Role `calls` and the parser

**Files:**
- Create: `src/trader/calls/parse.py`
- Modify: `src/trader/llm/registry.py` (`ROLES`, `RESEARCH_ROLES`), `src/trader/llm/caps.py` (`ROLES`), `src/trader/llm/profiles.py` (`ROLE_KINDS["calls"] = CHAT`), `src/trader/llm/probe.py` (`_contracts()` entry using `ParsedPost` and a one-post prompt)
- Test: `tests/test_calls_parse.py`; extend the Role-agreement test in `tests/test_learning.py` if it enumerates Roles.

**Interfaces:**
- Consumes: registry `complete(role, messages, *, schema=..., max_tokens=..., temperature=0.0) -> Completion`, `CallParsesRepo`, `ChannelPostsRepo.unparsed`.
- Produces:
  - `class CallLevels(BaseModel)`: `coin: str` (upper-case ticker, no quote currency), `side: Literal["long","short"]`, `entry: float | None`, `tps: list[float]` (0–3, in order), `stop: float | None`, `no_stop: bool`, `allocation_pct: float | None`, `leverage: float | None`.
  - `class CallUpdate(BaseModel)`: `coin: str | None`, `event: Literal["tp_hit","partial_profit","stop_to_breakeven","stop_moved","closed","other"]`, `tp_level: int | None`, `new_stop: float | None`.
  - `class ParsedPost(BaseModel)`: `kind: Literal["call","update","other"]`, `call: CallLevels | None`, `update: CallUpdate | None`, `flags: list[str] = []`.
  - `def validate_levels(levels: CallLevels) -> tuple[CallLevels, list[str]]`: for a long, drops TPs ≤ entry and a stop ≥ entry (short mirrored), sorts TPs away from entry, caps at 3; returns flags like `"tp_wrong_side"`, `"stop_wrong_side"`. With `entry is None`, TPs are only sorted.
  - `class CallParser`: `__init__(self, db, llm, *, channel_id: int)`; `async parse_pending(self, limit: int = 50) -> dict[str, int]` (counts per kind). System prompt states: extract only what the post states; never invent levels; `levels_only` posts (backfilled) must ignore any result/percentage/"TP hit" text and classify as a call when the post contains entry or TP levels for a coin; image-only or empty posts are `other`. A completion that fails JSON/schema validation is stored as kind `unparsed` with `error`; an `LLMError`/`NoRoute` stores nothing and stops the batch (retry next poll).
- [ ] **Step 1: Write failing tests** with a `StubLLM` (pattern `tests/test_loop.py:202`), using posts copied from the spec's examples (DOGE call text with "My Entry: 0,08054 … TP1: $0.0873 TP2: $0.0925 TP3: $0.0967 … not setting one … 0.5% … 7x"):
  - `test_call_post_is_parsed_and_stored` (kind call, payload levels as returned, model/cost recorded).
  - `test_backfilled_post_prompt_says_levels_only` (the user message for a backfilled post contains the levels-only instruction; for a live post it doesn't).
  - `test_malformed_json_is_stored_as_unparsed_never_guessed`.
  - `test_llm_error_stores_nothing`.
  - `test_inconsistent_levels_are_dropped_and_flagged`: long, entry 10, tps [9, 12, 11], stop 11 → tps [11, 12], stop None, flags contain `tp_wrong_side` and `stop_wrong_side`.
  - `test_calls_is_a_routable_role`: `"calls" in ROLES` of registry and caps, in `ROLE_KINDS`, and `probe` has a contract for it.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Comma decimals ("0,08054") are the model's job, stated in the prompt.
- [ ] **Step 4: Run** `PGTZ=UTC TZ=UTC uv run pytest -q tests/test_calls_parse.py tests/test_learning.py` → PASS.
- [ ] **Step 5: Commit** `feat(calls): Role calls and a validated post parser`.

### Task 5: Linking updates to calls

**Files:**
- Create: `src/trader/calls/link.py`
- Test: `tests/test_calls_link.py`

**Interfaces:**
- Consumes: `CallParsesRepo`, `ChannelPostsRepo`.
- Produces: `async def link_updates(db, channel_id: int) -> int` (number linked). For each `update` parse with `links_to` NULL: follow `reply_to` through posts until a post whose parse is a `call` (max depth 10); else the newest `call` with the same coin posted within 30 days before the update; else leave NULL. Sets `links_to` via `set_link`.
- [ ] **Step 1: Write failing tests:** `test_direct_reply_links`; `test_reply_to_an_update_walks_to_the_call`; `test_reply_to_commentary_falls_back_to_same_coin_recent_call`; `test_no_match_stays_unlinked` (coin differs, or call older than 30 days).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(calls): link updates to their calls`.

### Task 6: Bars by window and the scorer

**Files:**
- Modify: `src/trader/data/alpaca.py` (`AlpacaData.get_bars_between`), `src/trader/data/composite.py` (forwarding)
- Create: `src/trader/calls/score.py`
- Test: `tests/test_calls_score.py`, extend `tests/test_data.py` (the existing Alpaca data tests) for the window fetch.

**Interfaces:**
- Produces:
  - `async def get_bars_between(self, symbol: Symbol, interval: str, start: datetime, end: datetime) -> list[Bar]` (ascending; paginates with `start`/`end` query params on the same endpoints as `get_bars`; same interval set).
  - `@dataclass(frozen=True) class Fill: ts: datetime; price: float; fraction: float; reason: str` (`"entry"`, `"tp1"`..`"tp3"`, `"stop"`, `"breakeven"`, `"closed"`, `"mark"`).
  - `@dataclass(frozen=True) class CallScore: status: Literal["closed","open","unscorable"]; reason: str | None; entry: float | None; stated_entry: float | None; slippage_pct: float | None; stop_used: float | None; fills: tuple[Fill, ...]; gross_return: float; cost_return: float; net_return: float; first_touch: Literal["tp1","stop","neither"]; hours_held: float | None`, with `as_dict()`.
  - `def simulate(levels: CallLevels, posted_at: datetime, updates: list[tuple[datetime, CallUpdate]], bars: list[Bar], *, stop_frac: float, costs: ReplayCosts, asset_class: AssetClass) -> CallScore`, a pure function. Algorithm (state it in the docstring):
    1. Entry bar = first bar with `ts >= posted_at` rounded up to the next 15-minute boundary; entry = its open. No such bar → `unscorable`, reason `"no bars after post"`.
    2. Stop: long → `max(call stop, entry × (1 − stop_frac))`, short → `min(call stop, entry × (1 + stop_frac))`; missing call stop uses the fraction alone.
    3. TPs: the stated TPs on the profitable side of the actual entry; the position splits equally across them (3 → thirds, 2 → halves, 1 → all). No TP left → hold until stop, `closed` update or the end of bars.
    4. Walk bars from the entry bar: in each bar, check the stop first (low ≤ stop for a long), then TPs in order (high ≥ TP). After the first TP fill, stop = entry (break-even) unless already tighter.
    5. Updates take effect at the first bar starting at or after their `posted_at`: `stop_to_breakeven` → stop = entry; `stop_moved` with `new_stop` → stop = new_stop if it's tighter; `closed` → exit the rest at that bar's open. `tp_hit` and `partial_profit` change nothing (the Bot follows its own TPs).
    6. Bars run out with a position left → status `open`, a `"mark"` fill at the last close.
    7. Returns are fractions of the entry notional: gross from the fills; costs via `costs.fill(asset_class, notional=1000.0 * fraction)` per fill, summed and divided by 1000; net = gross − costs. `first_touch` = whichever of TP1 or the original stop was touched first (stop first on a tie).
  - `def size_usd(levels: CallLevels, sleeve_usd: float) -> float` = sleeve × (allocation_pct or 1.0)/100 × (leverage or 1.0).
- [ ] **Step 1: Write failing tests** with hand-built 15-minute bars (`Bar(symbol, "15m", ts, o, h, l, c, v)`) and `ReplayCosts()` (zero costs) unless stated:
  - `test_entry_is_next_bar_open_after_post` (post at 10:07 → entry = open of the 10:15 bar; slippage vs the stated entry recorded).
  - `test_thirds_at_each_tp_and_breakeven_after_tp1`: long entry 100, tps 110/120/130, stop none, frac 0.08 → bars hit 110 then fall to 100 → fills tp1 at 1/3, breakeven at 2/3; net = (0.10 × 1/3) = 0.0333 (abs 1e-9).
  - `test_stop_counts_first_when_one_bar_touches_both` (one bar with high ≥ TP1 and low ≤ stop → full stop loss, `first_touch == "stop"`).
  - `test_tighter_of_call_stop_and_frac`: call stop 90, frac 0.08, entry 100 → stop_used 92.0.
  - `test_closed_update_exits_the_rest_at_next_bar_open`.
  - `test_stop_moved_update_only_tightens`.
  - `test_short_is_mirrored` (scored even though not tradeable).
  - `test_open_when_bars_run_out` (status open, mark fill).
  - `test_unscorable_without_bars_after_post`.
  - `test_costs_reduce_net` (`ReplayCosts(crypto_taker_fee_bps=25.0)`: net = gross − 2 × 0.0025 for one entry and one full exit).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (`get_bars_between` with a test on a faked HTTP transport like the existing Alpaca data tests). **Step 4: Run** `PGTZ=UTC TZ=UTC uv run pytest -q tests/test_calls_score.py` tests/test_data.py → PASS.
- [ ] **Step 5: Commit** `feat(calls): window bars and the exit-ladder scorer`.

### Task 7: CallsDesk, scorecard and the three surfaces

**Files:**
- Create: `src/trader/calls/desk.py`
- Modify: `src/trader/surfaces/services.py` (`build_registry` gains `calls: CallsDesk | None = None`), `src/trader/surfaces/api/app.py`, `src/trader/surfaces/cli/main.py`, `CONTEXT.md`
- Test: `tests/test_calls_desk.py`, `tests/test_calls_surfaces.py`

**Interfaces:**
- Consumes: everything above; `costs_from_settings(settings)`; `settings.risk_stop_loss_frac`, `calls_sleeve_usd`; market data `get_bars_between`; `make_symbol(f"{coin}/USD", AssetClass.CRYPTO)`.
- Produces:
  - `class CallsDesk`: `__init__(self, db, settings, market, llm, reader: ChannelReader | None)`; `async poll(self) -> dict` (poll_once → parse_pending → link_updates → score_pending); `async backfill(self) -> dict` (backfill → parse until none pending → link → score all); `async score_pending(self) -> int` (scores calls with no score or status `open`; fetches bars from `posted_at − 15 min` to now; a coin without bars → `unscorable`, `tradeable=False`); `async scorecard(self) -> dict`; `async calls(self, *, limit: int = 100) -> list[dict]` (call levels + score + linked updates, newest first); `async posts(self, *, limit: int = 50) -> list[dict]` (latest versions with their parse kind).
  - `scorecard()` returns `{"calls": n, "tradeable": n, "scored": n, "open": n, "unscorable": n, "tp1_before_stop": n, "tp1_rate": x, "net_return_mean": x, "sleeve_pnl_usd": x, "by_coin": {...}, "untradeable": {"calls": n, "net_return_mean": x}, "live_days": d, "gate_pass": bool, "gate_reasons": [str]}`. `sleeve_pnl_usd` = Σ net_return × `size_usd(levels, calls_sleeve_usd)` over closed tradeable calls; `live_days` = days since the first non-backfilled post; `gate_pass` per Global Constraints.
  - Capabilities (registry names / API / CLI):
    - query `channel_posts` → `GET /calls/posts?limit=` → `trader calls posts`
    - query `channel_calls` → `GET /calls?limit=` → `trader calls list`
    - query `call_scorecard` → `GET /calls/scorecard` → `trader calls scorecard`
    - command `calls_poll` → `POST /calls/poll` → `trader calls poll`
    - command `calls_backfill` → `POST /calls/backfill` → `trader calls backfill`
    Each returns a clear error (`{"detail": "Telegram calls are off or no session is stored"}`, HTTP 409) when `calls` is None. The CLI group is `calls` (`trader telegram login/status` from Task 2 stays separate and local).
  - `CONTEXT.md` terms: **Channel call** (a trade a signals channel posts: coin, side, entry, take-profits, stop), **Call update** (a later post that changes a channel call: a take-profit hit, a stop moved, closed), **Call scorecard** (every channel call simulated against bars as the Bot would trade it).
- [ ] **Step 1: Write failing tests:**
  - `test_poll_runs_parse_link_and_score` (fakes for reader, LLM, market data; one call and one reply update → one score row, `links_to` set).
  - `test_unscorable_call_does_not_break_the_scorecard` (market raises/returns [] for one coin; scorecard still returns, `unscorable == 1`).
  - `test_scorecard_gate_needs_28_live_days_and_positive_pnl` (backfilled calls only → `gate_pass False`, reason mentions live days).
  - `test_sleeve_pnl_mirrors_allocation_times_leverage` (allocation 0.5, leverage 7, net +0.10 → +$35 on $10,000).
  - `test_surfaces_answer_409_when_calls_off` (TestClient on `/calls/scorecard` with `calls=None`).
  - `test_calls_capabilities_are_registered_and_on_mcp` (registry has the five names; MCP tool list contains `get_call_scorecard` and `calls_poll`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**; wire `CallsDesk` in `create_app` when `calls_enabled` (the poller from Task 3 calls `CallsDesk.poll` via `on_new`; the poller and the desk share one reader). **Step 4: Run** `PGTZ=UTC TZ=UTC uv run pytest -q` → all pass; `uv run ruff check . && uv run ruff format --check .` clean.
- [ ] **Step 5: Commit** `feat(calls): scorecard and calls on API, MCP and CLI`.

### Task 8: Desk panel

**Files:**
- Modify: `clients/desk/desk_logic.js`, `clients/desk/desk_logic.test.js`, `clients/desk/renderer.js`, `clients/desk/index.html` if a container is needed

**Interfaces:**
- Consumes: `GET /calls/scorecard`, `GET /calls`.
- Produces: `function scorecardTiles(sc) -> [{label, value, tone}]` and `function callRow(c) -> {coin, side, posted, entry, tps, stop, status, net, tradeable}` in `desk_logic.js`; a **Calls** section in the Bots view showing the tiles (tradeable calls, TP1 rate, sleeve P&L, live days, gate) and a table of calls. A 409 from the API shows "Telegram calls off".
- [ ] **Step 1: Write failing node tests:** `scorecardTiles` formats `tp1_rate 0.5` as `"50%"`, `sleeve_pnl_usd -12.3` with tone `"bad"`, `gate_pass false` as `"not yet"`; `callRow` shows `"—"` for a missing stop and marks untradeable rows.
- [ ] **Step 2: Run** `node --test clients/desk/desk_logic.test.js` → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Verify in the running desk** with Playwright's Electron driver (`playwright-core` `_electron.launch` on `clients/desk`, `TRADER_DESK_URL` at an API with calls on): open Bots → Calls, screenshot, no page errors, no `NaN`.
- [ ] **Step 6: Commit** `feat(desk): Telegram calls scorecard panel`.

### Task 9: Deploy runbook

**Files:**
- Create: `docs/runbooks/telegram-calls.md`
- Modify: `docs/architecture.md` (one paragraph under research inputs)

- [ ] **Step 1:** Write the runbook: `uv sync --all-extras`; on the Spark `trader telegram login --name spark` (QR scan from the phone: Settings → Devices → Link Desktop Device); set `TRADER_CALLS_ENABLED=true`; restart; `trader calls backfill`; `trader calls scorecard`; revoking the session (Telegram → Settings → Devices). State the gate and that Phase 2 needs its own design.
- [ ] **Step 2:** In the runbook, include setting the Role's Chain and cap after deploy: `trader profile set everyday calls --model grok-none:grok-4.3 --model openrouter:deepseek/deepseek-v4.1-flash` and `trader cap set calls 2`. Check the cap command's exact syntax with `trader cap --help`.
- [ ] **Step 3: Commit** `docs(calls): runbook and architecture note`.
