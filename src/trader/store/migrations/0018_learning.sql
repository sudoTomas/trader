-- Learning loop (#143). A Settlement scores one journaled decision 5 trading days on,
-- from daily bars: the symbol's forward return, the benchmark's (SPY for equities and
-- ETFs, BTC/USD for crypto) and the excess between them. direction is the decision's
-- view (+1 long, -1 short, 0 hold); correct says whether that view matched the excess
-- return's sign, NULL for a hold. settled_at is when the outcome became known (the end
-- of the exit bar), the instant a lesson about it may be read from.
CREATE TABLE decision_settlements (
    decision_id      bigint PRIMARY KEY REFERENCES decisions (id),
    bot_id           bigint NOT NULL REFERENCES bots (id),
    symbol           text   NOT NULL,
    horizon_days     integer NOT NULL,
    entry_ts         timestamptz NOT NULL,
    entry_price      double precision NOT NULL,
    exit_ts          timestamptz NOT NULL,
    exit_price       double precision NOT NULL,
    benchmark        text   NOT NULL,
    benchmark_return double precision NOT NULL,
    forward_return   double precision NOT NULL,
    excess_return    double precision NOT NULL,
    direction        smallint NOT NULL CHECK (direction IN (-1, 0, 1)),
    correct          boolean,
    settled_at       timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decision_settlements_bot_symbol_idx
    ON decision_settlements (bot_id, symbol, settled_at DESC);

-- A Lesson: what the reflect Role wrote about one settled decision. Kept per Bot and
-- symbol; available_at is its Settlement's settled_at, so a replay at an earlier bar
-- never reads it.
CREATE TABLE lessons (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id  bigint NOT NULL UNIQUE REFERENCES decision_settlements (decision_id),
    bot_id       bigint NOT NULL REFERENCES bots (id),
    symbol       text   NOT NULL,
    lesson       text   NOT NULL,
    written_by   text   NOT NULL,
    available_at timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lessons_bot_symbol_idx ON lessons (bot_id, symbol, available_at DESC);

CREATE FUNCTION learning_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER decision_settlements_no_update_delete
    BEFORE UPDATE OR DELETE ON decision_settlements
    FOR EACH ROW EXECUTE FUNCTION learning_append_only();
CREATE TRIGGER lessons_no_update_delete
    BEFORE UPDATE OR DELETE ON lessons
    FOR EACH ROW EXECUTE FUNCTION learning_append_only();
