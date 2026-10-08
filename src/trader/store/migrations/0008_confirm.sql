-- Confirm seat, first stage (#99): Kev's judgment of every open or close Draft,
-- journaled beside the decision it judged. Journal-only: nothing reads it back into the
-- money path. NULL when the Draft was a hold or no Confirm is configured.
ALTER TABLE decisions ADD COLUMN confirm jsonb;
