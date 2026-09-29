# Runbook: the first gated live-paper trade (EAI-176)

Everything below the credentials line is built and tested against a fake venue. This
runbook is the part that **needs operator Alpaca paper keys** — it cannot be completed
without them, and it should be walked deliberately rather than automated.

## 0. Preconditions

- Alpaca **paper** keys (distinct from live keys; generate at the paper dashboard).
- Postgres up: `docker compose -f infra/docker-compose.yml up -d db`
- A decide model reachable: `TRADER_DECIDE_*` for any OpenAI-compatible endpoint, or a
  self-hosted Nemotron vLLM at `TRADER_NEMOTRON_BASE_URL` (the preset).

```bash
# .env
ALPACA_API_KEY_ID=...
ALPACA_API_SECRET=...
TRADER_AUTH_DISABLED=true          # or set TRADER_API_KEY
TRADER_SHADOW_MODE=true            # still shadow at this point
TRADER_LOOP_ENABLED=true
TRADER_LOOP_SYMBOLS=ETH/USD
# TRADER_ALPACA_LIVE_ENABLED stays UNSET. Live must remain impossible.
```

## 1. Prove the venue connection read-only

```bash
uv run python - <<'EOF'
import asyncio
from trader.brokers.alpaca import AlpacaBroker
from trader.ops.config import get_settings

async def main():
    broker = AlpacaBroker.from_settings(get_settings())
    print("live:", broker.live)                 # MUST be False
    print("account:", await broker.get_account())
    print("positions:", await broker.get_positions())
    await broker.close()

asyncio.run(main())
EOF
```

Expected: `live: False`, account `status='ACTIVE'`, `tradable=True`. If `tradable` is
False the account is blocked — stop here.

## 2. Watch a shadow decision cycle

```bash
uv run uvicorn trader.surfaces.api.app:app --port 8080
trader loop status      # shadow: true
trader wake             # force a decide pass
trader decisions --limit 3
```

Expected: a journaled row with `status: shadow`, a full prompt/raw output, and risk
verdicts. `trader orders` must be empty — shadow submits nothing.

## 3. Flip shadow off (the deliberate edge gate)

This is the moment the system can place an order. RiskGuard is unchanged by it; shadow
mode was never the safety mechanism, only the throttle.

```bash
# .env
TRADER_SHADOW_MODE=false
```

Restart the API (`docs/runbooks/deploy.md`). On boot the executor runs `recover_pending()` and
`reconcile_positions()` before consuming the trade stream.

```bash
trader loop status      # shadow: false
trader broker reconcile # expect {"drift": {}}
```

If reconcile reports drift, routing is now paused by the kill switch. Investigate, then
either fix the ledger or adopt the venue's view (`Executor.adopt_venue_positions()`),
which clears the flag.

## 4. The first round trip

Keep the size small. `TRADER_RISK_APPROVAL_NOTIONAL_USD` parks anything larger than the
threshold, which is a useful second gate for the first trade.

```bash
trader wake
trader decisions --limit 1     # expect status: executed (or denied, with a reason)
trader orders                  # client_order_id, broker_order_id, status
trader positions               # after the fill streams in
```

Then verify the **same** order in the Alpaca paper dashboard — order id, quantity, and
fill price must match the `orders`/`fills` rows. This cross-check is the actual
acceptance criterion for EAI-176; a green test suite is not a substitute.

Close it out:

```bash
trader broker flatten-all --confirm FLATTEN
trader positions               # empty
```

## 5. What to watch afterwards

- `trader risk status` — breaker windows now track real equity.
- `trader audit --limit 20` — every submit, verdict, and reconcile is recorded.
- Alpaca paper does **not** simulate fees, slippage, or queue position; treat fills as
  optimistic (v1 findings §1/§5).

## Rollback

Set `TRADER_SHADOW_MODE=true` and restart: the build then has no broker attached at all
(the executor is only constructed when shadow is off), so it cannot reach a venue.
`trader risk pause --reason "..."` is the faster in-flight stop — it blocks every order,
exits included.
