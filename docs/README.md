# docs/

The index. The [top-level README](../README.md) is the pitch and the ten-minute path; this
folder holds everything behind it.

## Start here

| Page | What it is |
|---|---|
| [`architecture.md`](architecture.md) | The design: prime directive, the money path, the research desk, the LLM and data layers, the three surfaces, safety invariants, and what was deliberately not carried over from v1. Read it before non-trivial work. |
| [`quickstart.md`](quickstart.md) | The shadow-only profile in under ten minutes, with the expected output of every step and a table of the common `trader doctor` misses. |
| [`configuration.md`](configuration.md) | Every environment variable, generated from the `Settings` model by `scripts/gen_config_reference.py` and checked in CI. Do not edit by hand. |
| [`lessons.md`](lessons.md) | The hardened lessons behind the code, one slug per lesson: what broke, what we do now, and where the rule lives. Comments in `src/` and `tests/` cite these slugs. |
| [`tracker.md`](tracker.md) | Map of remaining work: which GitHub issues are open, closed, or held, and in what order they should land. |

## Runbooks (operator procedures)

| Page | When you need it |
|---|---|
| [`runbooks/deploy.md`](runbooks/deploy.md) | Standing the service up: compose with the published image, or the API as a host systemd user unit over the compose Postgres. Post-deploy checks. |
| [`runbooks/decide-change-gate.md`](runbooks/decide-change-gate.md) | Before any prompt edit, model swap, new prompt lane, desk change, or new live symbol: replay, then a shadow window, then one flip. One gate at a time. |
| [`runbooks/first-live-paper-trade.md`](runbooks/first-live-paper-trade.md) | Flipping shadow off for the first time, deliberately, with paper keys, and verifying the first round trip against the venue. |
| [`runbooks/research-capture.md`](runbooks/research-capture.md) | Feeding the research desk: notes through CLI, API, or MCP; the compiled Macro View; the one-way markdown mirror. |

## Deploy appendix

| Page | What it is |
|---|---|
| [`deploy/dgx-spark.md`](deploy/dgx-spark.md) | The one box the project was developed and first run on. An appendix, not a requirement: everything the repo needs is in the deploy runbook and `infra/`. |

## Integration contract

| Page | What it is |
|---|---|
| [`hermes/README.md`](hermes/README.md) | How an external agent (Hermes, or any MCP/HTTP client) reads state and submits research. It cannot place orders; no such tool exists on any surface. |
| [`hermes/skills/research-capture/SKILL.md`](hermes/skills/research-capture/SKILL.md) | The capture skill: distilling a forwarded article or memo into a note with an honest `authored_at`. |

## Research notes (spikes and decision records)

Dated investigations that shaped a decision. They record what was true when written; the
code and `configuration.md` are current.

| Page | Question it answered |
|---|---|
| [`research/2026-09-04-robinhood-agentic-trading.md`](research/2026-09-04-robinhood-agentic-trading.md) | What Robinhood's agentic trading with MCP actually is, and whether it fits as a broker. |
| [`research/2026-09-08-twelve-data-vs-alpaca.md`](research/2026-09-08-twelve-data-vs-alpaca.md) | Which market-data backbone to build on; why Alpaca is primary and Twelve Data a fallback. |
| [`research/2026-09-16-grok-bots-macro-team.md`](research/2026-09-16-grok-bots-macro-team.md) | Grok "bots" as a macro research team: product truth and the integration design that was accepted. |
| [`research/xai-api.md`](research/xai-api.md) | Which xAI endpoints a SuperGrok OAuth bearer can reach, and where `x_search` lives; how to fill the matrix with `trader xai probe` (in-service, operator-only). |

## Reports

Dated write-ups of a batch of work land in `reports/` (for example, the batch-one report
of 2026-09-29, which has the honest numbers behind the README's status section). Each is
a point-in-time record, not a living document.

## Assets

`assets/` holds hand-written SVGs used by the README, such as the money-path diagram. No
binary images.
