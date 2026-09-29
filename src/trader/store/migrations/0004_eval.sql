-- Replay eval harness. Offline reruns of the decide stage journal HERE, never into
-- decisions — the live journal stays evidence of what actually ran (see
-- docs/lessons.md#replay-before-flip).
CREATE TABLE eval_runs (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    label      text        NOT NULL,
    symbol     text        NOT NULL,
    interval   text        NOT NULL DEFAULT '1h',
    start_ts   timestamptz NOT NULL,
    end_ts     timestamptz NOT NULL,
    config     jsonb       NOT NULL DEFAULT '{}',
    status     text        NOT NULL DEFAULT 'running'
               CHECK (status IN ('running', 'done', 'failed')),
    summary    jsonb
);

CREATE TABLE eval_steps (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     bigint      NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    ts         timestamptz NOT NULL,
    status     text        NOT NULL,                 -- decided | error
    price      numeric,
    prompt     text,                                 -- replay-ready, like decisions
    raw_output text,
    decision   jsonb,
    latency_ms integer
);
CREATE INDEX eval_steps_run_idx ON eval_steps (run_id, ts);
