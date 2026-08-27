-- Per-candidate research briefs (EAI-192). One versioned row per compile, per symbol —
-- version is a single global identity (like macro_views) so a decision can point at
-- exactly the brief it saw. Advisory by construction: nothing in risk reads this table.
CREATE TABLE briefs (
    version      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    symbol       text     NOT NULL,
    compiled_by  text     NOT NULL,                     -- provider:model that compiled it
    thesis       text     NOT NULL,
    points       jsonb    NOT NULL DEFAULT '[]',
    note_ids     bigint[] NOT NULL DEFAULT '{}',
    sentiment_id bigint   REFERENCES sentiment_scores (id)
);
CREATE INDEX briefs_symbol_version_idx ON briefs (symbol, version DESC);

-- Evidence-bus: which brief the decider saw (NULL while the lane is dark).
ALTER TABLE decisions ADD COLUMN brief_version bigint;
