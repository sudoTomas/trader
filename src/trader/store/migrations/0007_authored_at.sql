-- Second date on research notes (Hermes capture requirement): created_at is when the
-- note was SUBMITTED (db default); authored_at is when the underlying research was
-- CREATED. NULL = unknown (treated as submitted). Recency weighting and the auto-stale
-- window anchor on authored_at when present — old information captured today is still
-- old information.
ALTER TABLE research_notes ADD COLUMN authored_at timestamptz;
