#!/bin/bash
# Desk shadow-window watcher (EAI-190). Hourly via infra/systemd/trader-desk-watch.timer:
# surfaces the events the operator wants to see the day they happen, not at the
# scheduled review — the first drafted intent, critic vetoes, decide errors, critic
# spend approaching its cap. Monitoring only: this script never flips anything.
#
# Inputs (env):
#   TRADER_REPO          checkout the watcher belongs to; desk-watch.log lands here
#                        (default: the repo this script lives in)
#   TRADER_DATABASE_URL  postgresql://user:pass@host:port/db (default: the
#                        TRADER_DATABASE_URL line of $TRADER_REPO/.env, else the
#                        compose default postgresql://trader:trader@localhost:5544/trader)
#   TRADER_DB_CONTAINER  docker container name filter used when no host psql exists
#                        (default: trader-db — matches the compose db service)
# State: last-seen decision id in ~/.local/state/trader/desk-watch.last
# Output: appends to $TRADER_REPO/desk-watch.log (gitignored) + systemd journal;
# best-effort notify-send when a display is around.
set -u

TRADER_REPO="${TRADER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [ -z "${TRADER_DATABASE_URL:-}" ] && [ -f "$TRADER_REPO/.env" ]; then
  TRADER_DATABASE_URL=$(grep -E '^TRADER_DATABASE_URL=' "$TRADER_REPO/.env" | tail -1 \
    | cut -d= -f2- | tr -d '"'"'")
fi
TRADER_DATABASE_URL="${TRADER_DATABASE_URL:-postgresql://trader:trader@localhost:5544/trader}"

# psql_t: run one query, tuples only. Host psql if present, else docker exec into the
# compose db container (the URL's host:port is meaningless inside the container, so
# only its user and database name are reused there).
if command -v psql >/dev/null 2>&1; then
  psql_t() { psql "$TRADER_DATABASE_URL" -tAc "$1"; }
else
  DB=$(docker ps -qf "name=${TRADER_DB_CONTAINER:-trader-db}" 2>/dev/null | head -1)
  [ -n "$DB" ] || exit 0  # db down is trader-api's problem, not the watcher's
  _rest=${TRADER_DATABASE_URL#*://}      # user:pass@host:port/db?params
  _creds=${_rest%%@*}
  DB_USER=${_creds%%:*}
  _path=${_rest#*/}
  DB_NAME=${_path%%\?*}
  psql_t() { docker exec "$DB" psql -U "$DB_USER" -d "$DB_NAME" -tAc "$1"; }
fi

STATE_DIR="$HOME/.local/state/trader"
STATE="$STATE_DIR/desk-watch.last"
LOG="$TRADER_REPO/desk-watch.log"
mkdir -p "$STATE_DIR"
LAST=$(cat "$STATE" 2>/dev/null || echo 0)

MAX_ID=$(psql_t "SELECT coalesce(max(id),0) FROM decisions")
[ -n "${MAX_ID:-}" ] || exit 0  # query failed (db unreachable); try again next tick

# Intents that reached the critic (verdict != skipped) since last check.
INTENTS=$(psql_t "SELECT count(*) FROM decisions WHERE id > $LAST
  AND desk_verdicts->'critic'->>'verdict' NOT IN ('skipped')")
VETOES=$(psql_t "SELECT count(*) FROM decisions WHERE id > $LAST
  AND desk_verdicts->'critic'->>'verdict' = 'veto'")
ERRORS=$(psql_t "SELECT count(*) FROM decisions WHERE id > $LAST AND status = 'error'")
EXECUTED=$(psql_t "SELECT count(*) FROM decisions WHERE id > $LAST AND status = 'executed'")
CRITIC_SPEND=$(psql_t "SELECT coalesce(round(sum(cost_usd)::numeric,3),0) FROM usage_daily
  WHERE day = current_date AND role = 'critic'")

echo "$MAX_ID" > "$STATE"

NOTE=""
[ "${EXECUTED:-0}" -gt 0 ] && NOTE="$NOTE EXECUTED=$EXECUTED(!)"
[ "${INTENTS:-0}" -gt 0 ] && NOTE="$NOTE intents=$INTENTS (vetoes=$VETOES)"
[ "${ERRORS:-0}" -gt 0 ] && NOTE="$NOTE errors=$ERRORS(!)"
awk -v s="${CRITIC_SPEND:-0}" 'BEGIN{exit !(s>=0.8)}' && NOTE="$NOTE critic_spend=\$$CRITIC_SPEND(near cap)"

if [ -n "$NOTE" ]; then
  MSG="desk-watch [ids $((LAST+1))..$MAX_ID]:$NOTE"
  echo "$(date -Is) $MSG" >> "$LOG"
  echo "$MSG" | systemd-cat -t trader-desk-watch -p notice
  command -v notify-send >/dev/null && notify-send "trader desk" "$MSG" 2>/dev/null
else
  echo "$(date -Is) quiet [ids $((LAST+1))..$MAX_ID] critic_spend=\$${CRITIC_SPEND:-0}" >> "$LOG"
fi
