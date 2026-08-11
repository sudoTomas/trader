# Spike: xAI API surface for the SuperGrok OAuth bearer

**Question (EAI-173):** does the SuperGrok OAuth access token work on
`POST /v1/chat/completions`, or only on `POST /v1/responses`? v1 never resolved this —
Hermes used `/responses`, embry0 used `/messages`. `/v1/responses` hosts `x_search`
regardless, so the sentiment role will eventually need `/responses` either way.

## Status: implementation done, live verification pending operator login

No xAI credential exists on the Spark yet (`.env` absent, no `XAI_API_KEY`, no OAuth
lineage), so the bearer/endpoint matrix can't be probed live. The client code targets
`/chat/completions` (via the shared `OpenAIChatClient`); if the probe below shows the
OAuth bearer is `/responses`-only, the fix is confined to `llm/xai.py`.

## How to run the probe (operator, once)

1. `TRADER_XAI_TOKEN_KEY=<fernet key>` in `.env` (generate per `.env.example`), then
   `trader xai login` and approve in the browser.
2. Probe both endpoints with the OAuth bearer:

```bash
uv run python - <<'EOF'
import asyncio, httpx
from trader.ops.config import get_settings
from trader.llm.xai_auth import build_manager

async def main():
    token = await build_manager(get_settings()).get_access_token()
    async with httpx.AsyncClient(
        base_url="https://api.x.ai/v1",
        headers={"Authorization": f"Bearer {token}"}, timeout=60,
    ) as c:
        for path, body in [
            ("/chat/completions", {"model": "grok-4",
                "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}),
            ("/responses", {"model": "grok-4", "input": "ping", "max_output_tokens": 4}),
        ]:
            r = await c.post(path, json=body)
            print(path, r.status_code, r.text[:120])

asyncio.run(main())
EOF
```

3. Record the result matrix here and, if `/chat/completions` rejects the OAuth bearer
   (401/403 with a working `/responses`), point `xai_client` at `/responses` and adapt
   the payload mapping in `llm/xai.py`.

## Findings

| endpoint | OAuth bearer | XAI_API_KEY |
|---|---|---|
| `/v1/chat/completions` | _pending_ | _pending_ |
| `/v1/responses` | _pending_ | _pending_ |

## Known facts carried from v1

- The refresh token rotates on every use and is single-use — hence the
  persist-before-serve discipline in `llm/xai_auth.py`.
- 403 from `auth.x.ai` or `api.x.ai` is entitlement (SuperGrok-tier gating; X Premium+
  does NOT include API access) — quarantine, never retry-spin.
- Seeding from the Grok CLI's `~/.grok/auth.json` breaks the CLI's own login
  (two owners of one rotating lineage) — deliberately not implemented.
