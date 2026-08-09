-- 0001_baseline — the whole schema in one migration (not v1's 17).
-- Tables are designed for their M3/M4 consumers (docs/architecture.md); shipped
-- empty-but-correct now so later milestones only add code, not DDL.

-- ---------------------------------------------------------------------------
-- Decision journal (M3 loop). One row per wakeup — including suppressed and
-- shadow decisions. Replay-ready: the exact prompt, raw output, risk verdicts,
-- and the macro-view version the decider saw.
-- ---------------------------------------------------------------------------
CREATE TABLE decisions (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                 timestamptz NOT NULL DEFAULT now(),
    symbol             text,
    asset_class        text,
    trigger            text        NOT NULL,           -- heartbeat | price_move | vol_spike | ...
    status             text        NOT NULL,           -- shadow | proposed | denied | executed | error
    prompt             text,                           -- full decide prompt, verbatim
    raw_output         text,                           -- raw LLM completion, pre-validation
    decision           jsonb,                          -- schema-validated decision, if any
    risk_verdicts      jsonb,                          -- ordered RiskGuard check results
    macro_view_version bigint,                         -- NULL when no view existed / block omitted
    cost_usd           numeric(12, 6),
    latency_ms         integer
);
CREATE INDEX decisions_ts_idx ON decisions (ts DESC);
CREATE INDEX decisions_symbol_ts_idx ON decisions (symbol, ts DESC);

-- ---------------------------------------------------------------------------
-- Orders + fills (M3 executor). client_order_id is the idempotency key: the
-- row is persisted BEFORE submit, so a crash between insert and ack is
-- reconcilable against the broker.
-- ---------------------------------------------------------------------------
CREATE TABLE orders (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id     bigint REFERENCES decisions (id),
    client_order_id text        NOT NULL UNIQUE,
    broker_order_id text UNIQUE,
    symbol          text        NOT NULL,
    asset_class     text        NOT NULL,
    side            text        NOT NULL CHECK (side IN ('buy', 'sell')),
    qty             numeric(24, 10),
    notional        numeric(18, 2),
    order_type      text        NOT NULL DEFAULT 'market',
    limit_price     numeric(18, 6),
    time_in_force   text        NOT NULL DEFAULT 'gtc',
    status          text        NOT NULL DEFAULT 'new',  -- new | submitted | filled | canceled | rejected | error
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    raw             jsonb,                                -- last broker payload seen
    CHECK (qty IS NOT NULL OR notional IS NOT NULL)
);
CREATE INDEX orders_symbol_idx ON orders (symbol, created_at DESC);
CREATE INDEX orders_status_idx ON orders (status);

CREATE TABLE fills (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id       bigint      NOT NULL REFERENCES orders (id),
    broker_fill_id text UNIQUE,                          -- dedupes trade-updates replays
    ts             timestamptz NOT NULL,
    qty            numeric(24, 10) NOT NULL,
    price          numeric(18, 6)  NOT NULL,
    raw            jsonb
);
CREATE INDEX fills_order_idx ON fills (order_id);

-- ---------------------------------------------------------------------------
-- Positions (M3). A snapshot mirror of broker state, reconciled from fills —
-- not a ledger; fills are the ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE positions (
    symbol          text PRIMARY KEY,
    asset_class     text        NOT NULL,
    qty             numeric(24, 10) NOT NULL,
    avg_entry_price numeric(18, 6),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Append-only audit (every milestone). No UPDATE/DELETE path exists in code,
-- and the trigger below makes the table itself refuse both.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_events (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    actor    text        NOT NULL,   -- api:<key-id> | mcp | cli | loop | system
    category text        NOT NULL,   -- order | risk | approval | research | config | ...
    action   text        NOT NULL,
    subject  text,                   -- e.g. symbol, order id, note id
    payload  jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_events_ts_idx ON audit_events (ts DESC);
CREATE INDEX audit_events_category_idx ON audit_events (category, ts DESC);

CREATE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();

-- ---------------------------------------------------------------------------
-- Research Desk (M4). Notes are immutable content; only status/pin lifecycle
-- fields change. Auto-stale via expires_at unless pinned.
-- ---------------------------------------------------------------------------
CREATE TABLE research_notes (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    title         text        NOT NULL,
    body          text        NOT NULL,
    tags          text[]      NOT NULL DEFAULT '{}',
    asset_classes text[]      NOT NULL DEFAULT '{}',
    symbols       text[]      NOT NULL DEFAULT '{}',
    source        text,                                  -- url / "voice memo" / etc.
    status        text        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disputed', 'retired')),
    pinned        boolean     NOT NULL DEFAULT false,    -- evergreen: exempt from auto-stale
    expires_at    timestamptz                            -- NULL only when pinned
);
CREATE INDEX research_notes_status_idx ON research_notes (status);

-- Versioned compiled digest. A new row per compile; version is the identity —
-- decisions journal macro_view_version so any decision can be replayed against
-- the exact digest it saw.
CREATE TABLE macro_views (
    version       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    compiled_by   text        NOT NULL,                  -- model that compiled (grok | nemotron fallback)
    global_thesis text        NOT NULL,
    sections      jsonb       NOT NULL,                  -- {asset_class: digest, ...}; claims cite note ids
    note_ids      bigint[]    NOT NULL                   -- notes included in this compile
);

-- ---------------------------------------------------------------------------
-- Bars cache (M1). Immutable read-through replay cache — one candle per row,
-- NEVER a live-path dependency, MB-scale with bounded retention (v1:EAI-159).
-- ---------------------------------------------------------------------------
CREATE TABLE bars (
    symbol   text        NOT NULL,
    interval text        NOT NULL,                       -- 1m | 5m | 1h | 1d
    ts       timestamptz NOT NULL,
    open     numeric(18, 6) NOT NULL,
    high     numeric(18, 6) NOT NULL,
    low      numeric(18, 6) NOT NULL,
    close    numeric(18, 6) NOT NULL,
    volume   numeric(24, 8) NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
);

-- ---------------------------------------------------------------------------
-- Approvals (M3 HITL). Large orders park here as pending; a human resolves via
-- API/MCP/CLI. This *is* the human-in-the-loop — no graph interrupts.
-- ---------------------------------------------------------------------------
CREATE TABLE approvals (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    decision_id  bigint REFERENCES decisions (id),
    order_intent jsonb       NOT NULL,                   -- the clamped intent awaiting a human
    reason       text        NOT NULL,                   -- which RiskGuard check parked it
    status       text        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    resolved_by  text,
    resolved_at  timestamptz
);
CREATE INDEX approvals_status_idx ON approvals (status);

-- ---------------------------------------------------------------------------
-- Usage metering (M2). Real token counts per (day, role, provider, model) so
-- budget gates see actual spend — v1 recorded None and budgets always read $0.
-- ---------------------------------------------------------------------------
CREATE TABLE usage_daily (
    day               date    NOT NULL,
    role              text    NOT NULL,                  -- decide | brief | compiler | sentiment
    provider          text    NOT NULL,
    model             text    NOT NULL,
    requests          integer NOT NULL DEFAULT 0,
    prompt_tokens     bigint  NOT NULL DEFAULT 0,
    completion_tokens bigint  NOT NULL DEFAULT 0,
    cost_usd          numeric(12, 6) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, role, provider, model)
);
