-- Decision desk: analyst reads + critic verdict + what was applied, journaled
-- per decision (like risk_verdicts) so replay can score the desk. NULL while dark.
ALTER TABLE decisions ADD COLUMN desk_verdicts jsonb;
