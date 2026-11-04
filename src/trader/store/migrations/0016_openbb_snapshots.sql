-- OpenBB research inputs (#141): what the OpenBB producer read for one symbol, one row
-- per (kind, symbol) per refresh. kind is the Source it feeds: earnings (next report date
-- and time, consensus EPS, last surprise), options (ATM implied vol, implied move to the
-- next expiry, put/call skew) or fundamentals (price-target consensus, insider trades,
-- short interest, ratios). data is a compact dated summary, never a chain or a price
-- window: the live path stores no market data. Rows are append-only; the id is the
-- Source version, and a pinned read takes the newest row with ts <= as_of, so replay
-- reads only what had been fetched by then. A failed fetch writes no row: the last good
-- one ages until it reads as stale.
CREATE TABLE openbb_snapshots (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts       timestamptz NOT NULL DEFAULT now(),
    kind     text        NOT NULL CHECK (kind IN ('earnings', 'options', 'fundamentals')),
    symbol   text        NOT NULL,
    provider text        NOT NULL,  -- the OpenBB provider(s) the summary came from
    data     jsonb       NOT NULL
);
CREATE INDEX openbb_snapshots_kind_symbol_ts_idx ON openbb_snapshots (kind, symbol, ts DESC);
