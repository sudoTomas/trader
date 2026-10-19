-- Multiple Bots, slice 1 (#123): the running agent becomes Bot #1. A Bot is one strategy
-- (symbols and their Symbol modes, triggers, lanes, prompt, account); every change to
-- its spec appends a Bot version (append-only, like Profile versions) and moves the
-- Bot's current pointer. Bot #1's spec is seeded from Settings at boot, so this
-- migration creates only the Bot itself, for the backfill below to point at.
CREATE TABLE bots (
    id              bigserial PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    current_version integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bot_versions (
    id          bigserial PRIMARY KEY,
    bot_id      bigint  NOT NULL REFERENCES bots (id),
    version     integer NOT NULL,
    spec        jsonb   NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bot_id, version)
);

CREATE FUNCTION bot_versions_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'bot_versions is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER bot_versions_no_update_delete
    BEFORE UPDATE OR DELETE ON bot_versions
    FOR EACH ROW EXECUTE FUNCTION bot_versions_append_only();

INSERT INTO bots (id, name) VALUES (1, 'bot-1');
SELECT setval(pg_get_serial_sequence('bots', 'id'), 1);

-- Which Bot a row belongs to. Everything written before Bots existed was Bot #1's.
-- NULL on a new row = not attributed to a Bot (eval runs and usage are not stamped yet).
ALTER TABLE decisions ADD COLUMN bot_id bigint REFERENCES bots (id);
ALTER TABLE orders ADD COLUMN bot_id bigint REFERENCES bots (id);
ALTER TABLE approvals ADD COLUMN bot_id bigint REFERENCES bots (id);
ALTER TABLE eval_runs ADD COLUMN bot_id bigint REFERENCES bots (id);
-- Not part of usage_daily's key: the rollup still sums per day, Role, Provider and model.
ALTER TABLE usage_daily ADD COLUMN bot_id bigint REFERENCES bots (id);
UPDATE decisions SET bot_id = 1;
UPDATE orders SET bot_id = 1;
UPDATE approvals SET bot_id = 1;
UPDATE eval_runs SET bot_id = 1;
UPDATE usage_daily SET bot_id = 1;

-- The Bot version that made the decision (its per-Bot number). NULL on rows journaled
-- before Bot versions existed: no version describes them truthfully.
ALTER TABLE decisions ADD COLUMN bot_version integer;
ALTER TABLE decisions ADD CONSTRAINT decisions_bot_version_fk
    FOREIGN KEY (bot_id, bot_version) REFERENCES bot_versions (bot_id, version);
