-- X sentiment scores. One row per poll, including unavailable outcomes — a degraded
-- read (no live citations) is journaled but never carries a number
-- (see docs/lessons.md#sentiment-no-citations-is-unavailable).
CREATE TABLE sentiment_scores (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    symbol    text        NOT NULL,
    status    text        NOT NULL CHECK (status IN ('ok', 'unavailable')),
    score     numeric     CHECK (score BETWEEN -1 AND 1),
    volume    text,                                      -- quiet | normal | elevated | storm
    drivers   jsonb       NOT NULL DEFAULT '[]',
    citations jsonb       NOT NULL DEFAULT '[]',
    model     text,
    reason    text,                                      -- why unavailable (degraded/cap/parse)
    CONSTRAINT sentiment_unavailable_has_no_score CHECK (status = 'ok' OR score IS NULL)
);
CREATE INDEX sentiment_scores_symbol_ts_idx ON sentiment_scores (symbol, ts DESC);
