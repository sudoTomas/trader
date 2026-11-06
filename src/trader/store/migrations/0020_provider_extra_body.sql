-- Extra request fields per Provider: a JSON object merged into every chat request a
-- Profile Chain sends it (OpenRouter's {"reasoning": {"enabled": false}}, say), the
-- picker's counterpart of TRADER_DECIDE_EXTRA_BODY. '{}' sends nothing extra.
ALTER TABLE llm_providers ADD COLUMN extra_body jsonb NOT NULL DEFAULT '{}'::jsonb;
