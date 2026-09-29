# Security

## Report privately

Do not open a public GitHub issue for a vulnerability.

Use [private vulnerability reporting](https://github.com/sudoTomas/trader/security/advisories/new).
If that form is unavailable, email tmcmonigal@protonmail.com.

Include what you found, how to reproduce it, and the impact you expect. Please
give us time to respond before any public disclosure.

## What counts

- Weakening the paper-only gate. A live Alpaca client must not construct unless
  `TRADER_ALPACA_LIVE_ENABLED=true`.
- Any path that submits an order without RiskGuard.
- Secrets in the repo, issues, or logs: `.env`, Alpaca keys, `TRADER_XAI_TOKEN_KEY`,
  the xAI token file.

## Supported versions

Paper-only, pre-1.0. Fixes land on `main`. Live trading is unsupported.
