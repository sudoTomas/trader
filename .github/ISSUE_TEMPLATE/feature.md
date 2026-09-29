---
name: Feature
about: A capability that belongs on all three surfaces
title: ""
labels: enhancement
---

## Capability

## Why

## Surfaces

Register it in `src/trader/surfaces/services.py` first, then the REST API, MCP, and CLI.
Do not add a raw place-order tool.

## Decide-change gate

If this touches a prompt, a model, or the risk chain, say so. Those changes follow
`docs/runbooks/decide-change-gate.md`, one gate at a time.
