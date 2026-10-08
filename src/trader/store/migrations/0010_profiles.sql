-- Model picker, slice 2 (#107): Profiles. A Profile is a named, renamable combination of
-- Chains; every edit appends a Profile version (append-only) and moves the Profile's
-- current pointer, so a revert is a pointer move back to an earlier version. Exactly one
-- Profile is active once any exists; a Role the active version leaves out keeps .env.
CREATE TABLE llm_profiles (
    id              bigserial PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    active          boolean NOT NULL DEFAULT false,
    current_version bigint,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX llm_profiles_one_active ON llm_profiles ((true)) WHERE active;

-- roles: {role: [{"provider_id": int, "model": text}, ...]}; an empty list is "off"
-- (confirm only). Providers are referenced by id so a rename never breaks a Chain.
CREATE TABLE llm_profile_versions (
    id          bigserial PRIMARY KEY,
    profile_id  bigint NOT NULL REFERENCES llm_profiles (id),
    version     integer NOT NULL,
    roles       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (profile_id, version)
);

-- Which Profile version was active for the decide call, and which model answered it
-- ("provider:model"), fallbacks included. NULL profile_version = served from .env.
ALTER TABLE decisions ADD COLUMN profile_version bigint;
ALTER TABLE decisions ADD COLUMN served_by text;
