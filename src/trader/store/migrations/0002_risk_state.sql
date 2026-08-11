-- 0002_risk_state — DB-backed kill switches and breaker state.
--
-- Breaker + pause state must survive restart (docs/architecture.md §Safety invariants):
-- a process bounce must never silently un-halt trading. Single row, id = 1.
CREATE TABLE risk_state (
    id                     smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    trading_paused         boolean     NOT NULL DEFAULT false,
    pause_reason           text,
    -- Position drift between broker and local ledger; set by the M3 reconciler.
    drift_detected         boolean     NOT NULL DEFAULT false,
    -- NULL | daily_loss | weekly_drawdown. Halts new opens; exits stay managed.
    halted_reason          text,
    halted_at              timestamptz,
    -- Daily-loss window, bucketed by the venue's session day (see core/calendars).
    day_session            date,
    day_start_equity       numeric(18, 2),
    -- Rolling equity high-water for the drawdown breaker. Operator resume
    -- re-baselines this (v1 lesson: a monotonic high-water re-trips the kill on the
    -- very next tick after resume).
    week_high_equity       numeric(18, 2),
    updated_at             timestamptz NOT NULL DEFAULT now()
);

INSERT INTO risk_state (id) VALUES (1);
