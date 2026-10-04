#!/usr/bin/env bash
# Runs import_emails.py every hour as a systemd user timer. Re-run it if you move the folder.
# Settings go in an EnvironmentFile (default ~/.config/trader/investing-group.env):
#   TIG_GMAIL_TOKEN_FILE, TRADER_API_BASE_URL, TRADER_API_KEY (the capture-scoped key).
# Check it with: systemctl --user list-timers tig-import   and   journalctl --user -u tig-import
set -euo pipefail
IFS=$'\n\t'

here="$(cd "$(dirname "$0")" && pwd)"
uv="$(command -v uv || echo "$HOME/.local/bin/uv")"
envfile="${TIG_ENV_FILE:-$HOME/.config/trader/investing-group.env}"
units="$HOME/.config/systemd/user"
mkdir -p "$units"

cat >"$units/tig-import.service" <<UNIT
[Unit]
Description=Import The Investing Group emails into trader research notes

[Service]
Type=oneshot
EnvironmentFile=$envfile
ExecStart=$uv run --script $here/import_emails.py
UNIT

cat >"$units/tig-import.timer" <<UNIT
[Unit]
Description=Hourly Investing Group email import

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now tig-import.timer
echo "Installed tig-import.timer (settings: $envfile)"
