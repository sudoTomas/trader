#!/usr/bin/env bash
# Deploy the checkout to a Shape B host (systemd user unit, docs/runbooks/deploy.md).
#
# Pulls (fast-forward only), syncs every extra, restarts trader-api and checks /health.
# Plain `uv sync` drops the extras: without `telegram` the calls poller cannot connect
# and nothing else fails (2026-11-18). Run from the checkout: `scripts/deploy-host.sh`.
set -euo pipefail

cd "$(dirname "$0")/.."
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

git pull --ff-only
"$UV" sync --all-extras --dev
systemctl --user restart trader-api

# The API answers /health before Telegram connects; give the poller its first attempts.
for _ in $(seq 1 12); do
  sleep 5
  if health=$("$UV" run trader health 2>/dev/null); then
    status=$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    connected=$(printf '%s' "$health" | python3 -c \
      'import json,sys; p=json.load(sys.stdin).get("calls_poller"); print("n/a" if p is None else p["connected"])')
    if [ "$status" = ok ] && [ "$connected" != False ]; then
      git log --oneline -1
      printf '%s\n' "$health"
      exit 0
    fi
  fi
done
echo "deploy: trader-api is not healthy after restart" >&2
printf '%s\n' "${health:-no /health answer}" >&2
exit 1
