-- Telegram channel calls scorecard, Phase 1
-- (docs/superpowers/specs/2026-11-12-telegram-signals-bot-design.md). Read-only on
-- Telegram: posts are stored as versions (an edit is a new version, never an overwrite),
-- each post is parsed once from its first version, and scores hold results, not bars.

-- The Telegram login. A full-account credential: api_hash and the session string are
-- Fernet-encrypted with TRADER_SECRETS_KEY and never leave this table in plaintext.
CREATE TABLE channel_sessions (
    name          text PRIMARY KEY,
    api_id        bigint NOT NULL,
    api_hash_enc  text NOT NULL,
    session_enc   text NOT NULL,
    created_at    timestamptz DEFAULT now()
);

-- One row per seen version of a post. version 1 is the first-seen text; an edit (text or
-- edited_at changed) adds version n+1. backfilled marks posts read from history rather
-- than seen live.
CREATE TABLE channel_posts (
    channel_id  bigint NOT NULL,
    msg_id      bigint NOT NULL,
    version     int NOT NULL,
    posted_at   timestamptz NOT NULL,
    edited_at   timestamptz,
    seen_at     timestamptz NOT NULL DEFAULT now(),
    reply_to    bigint,
    text        text NOT NULL DEFAULT '',
    media       text,
    backfilled  bool NOT NULL,
    PRIMARY KEY (channel_id, msg_id, version)
);

-- One parse per post: the first one. 'unparsed' rows record a failed parse ({} payload).
CREATE TABLE call_parses (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id       bigint NOT NULL,
    msg_id           bigint NOT NULL,
    version          int NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('call', 'update', 'other', 'unparsed')),
    payload          jsonb NOT NULL,
    links_to         bigint,
    model            text,
    profile_version  int,
    cost_usd         numeric(12, 6),
    error            text,
    created_at       timestamptz DEFAULT now(),
    UNIQUE (channel_id, msg_id),
    FOREIGN KEY (channel_id, msg_id, version) REFERENCES channel_posts
);

-- The latest score per call (upserted on every rescore).
CREATE TABLE call_scores (
    channel_id  bigint NOT NULL,
    msg_id      bigint NOT NULL,
    status      text NOT NULL CHECK (status IN ('closed', 'open', 'unscorable')),
    tradeable   bool NOT NULL,
    result      jsonb NOT NULL,
    scored_at   timestamptz DEFAULT now(),
    PRIMARY KEY (channel_id, msg_id),
    FOREIGN KEY (channel_id, msg_id) REFERENCES call_parses (channel_id, msg_id)
);
