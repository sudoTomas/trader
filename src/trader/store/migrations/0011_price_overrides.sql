-- Model picker, slice 4 (#105): a manual price per (Provider, model), USD per 1M prompt
-- and completion tokens. The top layer of the price lookup after "local is free".
CREATE TABLE llm_price_overrides (
    provider_id bigint NOT NULL REFERENCES llm_providers (id) ON DELETE CASCADE,
    model       text NOT NULL,
    price_in    numeric(12, 6) NOT NULL CHECK (price_in >= 0),
    price_out   numeric(12, 6) NOT NULL CHECK (price_out >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_id, model)
);
