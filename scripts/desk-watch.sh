#!/bin/bash
# Desk shadow-window watcher (EAI-190). Hourly via the trader-desk-watch systemd user
# timer: surfaces the events the operator wants to see the day they happen, not at the
# scheduled review — the first drafted intent, critic vetoes, decide errors, critic
# spend approaching its cap. Monitoring only: this script never flips anything.
#
# State: last-seen decision id in ~/.local/state/trader/desk-watch.last
# Output: appends to ~/repos/trader/desk-watch.log (gitignored) + systemd journal;
# best-effort notify-send when a display is around.
set -u
DB=$(docker ps -qf name=trader-db)
[ -n "$DB" ] || exit 0  # db down is trader-api's problem, not the watcher's

STATE_DIR="$HOME/.local/state/trader"
STATE="$STATE_DIR/desk-watch.last"
LOG="$HOME/repos/trader/desk-watch.log"
mkdir -p "$STATE_DIR"
LAST=$(cat "$STATE" 2>/dev/null || echo 0)

psql_t() { docker exec "$DB" psql -U trader -d trader -tAc "$1"; }

MAX_ID=$(psql_t "SELECT coalesce(max(id),0) FROM decisions")

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
