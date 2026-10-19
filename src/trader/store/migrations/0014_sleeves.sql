-- Multiple Bots, slice 3 (#125, ADR 0003): each Bot trades a Sleeve of the one account.
--
-- bot_positions is each Bot's sub-ledger, folded from fills through orders.bot_id the
-- same way positions is. The Sleeves together must equal positions, which must equal
-- the venue; reconcile checks both, and a mismatch is an Account halt.
-- realized_pnl accumulates what the Bot's reductions of the symbol realized, so a
-- fixed-dollar Sleeve's equity (capital + realized + marked) is computable locally.
CREATE TABLE bot_positions (
    bot_id          bigint          NOT NULL REFERENCES bots (id),
    symbol          text            NOT NULL,
    qty             numeric(24, 10) NOT NULL,
    avg_entry_price numeric(18, 6),
    realized_pnl    numeric(18, 6)  NOT NULL DEFAULT 0,
    updated_at      timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, symbol)
);

-- Everything held before Sleeves existed was built by Bot #1's fills.
INSERT INTO bot_positions (bot_id, symbol, qty, avg_entry_price, updated_at)
SELECT 1, symbol, qty, avg_entry_price, updated_at FROM positions;

-- risk_state gains a scope: 'account' (the old singleton row, kept as it was) or
-- 'bot:<id>' (that Bot's own pause and breakers). No foreign key to bots on purpose: the
-- account row must never go with a bots cascade.
ALTER TABLE risk_state ADD COLUMN scope text;
UPDATE risk_state SET scope = 'account';
ALTER TABLE risk_state DROP CONSTRAINT risk_state_pkey;
ALTER TABLE risk_state DROP COLUMN id;
ALTER TABLE risk_state ALTER COLUMN scope SET NOT NULL;
ALTER TABLE risk_state ADD PRIMARY KEY (scope);
ALTER TABLE risk_state ADD CONSTRAINT risk_state_scope_check
    CHECK (scope = 'account' OR scope ~ '^bot:[1-9][0-9]*$');
INSERT INTO risk_state (scope) SELECT 'bot:' || id FROM bots;
