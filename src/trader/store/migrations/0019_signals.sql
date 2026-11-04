-- Shadow signals (#144): every Kronos forecast and crypto derivatives reading a decide
-- made, in the prompt or not: {source: {version, as_of, in_prompt, reading}}. The
-- reading is a handful of summary numbers (share of paths up, p10/p90; funding, open
-- interest, basis), not bars, so scoring it against later bars stores no market data.
-- NULL when the Bot version subscribed to no shadow signal or none produced a reading.
ALTER TABLE decisions ADD COLUMN signals jsonb;
