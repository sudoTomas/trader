-- Model picker, slice 5 (#108): a daily USD Spend cap per Role, independent of the
-- active Profile (switching Profiles never lifts a cap). No row: the Role's default
-- (the critic's is TRADER_DESK_CRITIC_DAILY_USD), else uncapped.
CREATE TABLE llm_spend_caps (
    role       text PRIMARY KEY,
    daily_usd  numeric(12, 6) NOT NULL CHECK (daily_usd >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
