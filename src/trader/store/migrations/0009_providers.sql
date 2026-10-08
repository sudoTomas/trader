-- Model picker, slice 1 (#103): Providers, where models are served from. A hosted
-- Provider's key is Fernet ciphertext (master key TRADER_SECRETS_KEY, never in the db);
-- key_last4 is the only part of it any read returns. Local Providers have no key.
CREATE TABLE llm_providers (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    preset      text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('openai_compat', 'anthropic', 'xai', 'jev')),
    base_url    text NOT NULL,
    local       boolean NOT NULL DEFAULT false,
    api_key_enc text,
    key_last4   text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
