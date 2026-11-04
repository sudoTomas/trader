-- Vendor research reads (#142): one row per producer fetch of SEC EDGAR filings,
-- Polymarket odds or StockTwits/Reddit counts. Append-only; the id is the Source
-- version a decision journals. ts is when the producer fetched it: the availability time
-- of data with no historical vintage (Polymarket, retail counts). SEC payload items carry
-- their own acceptance times, so a later row still serves the past item by item.
-- symbol is NULL for a market-wide read (Polymarket). Payloads are small digests (counts,
-- a few filings and as-filed facts), never a market-data window.
CREATE TABLE vendor_reads (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    source  text        NOT NULL,                      -- the Source that reads it
    symbol  text,
    payload jsonb       NOT NULL
);
CREATE INDEX vendor_reads_source_symbol_ts_idx ON vendor_reads (source, symbol, ts DESC);
