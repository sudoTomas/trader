-- Multiple Bots, slice 2 (#124): the Sources a decision read. One entry per Source the
-- Bot version subscribed to and that produced a lane: {source: {version, as_of}}, where
-- version is the Source's immutable row id (macro_views.version, briefs.version,
-- sentiment_scores.id), series@obs_date for FRED, or a content hash for the held
-- position. Replay reads the same Sources pinned to as_of. NULL on rows journaled before
-- Sources existed; macro_view_version and brief_version stay filled beside it.
ALTER TABLE decisions ADD COLUMN source_versions jsonb;
