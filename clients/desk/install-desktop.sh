#!/usr/bin/env bash
# Adds Salamander (the trader desk) to the desktop app launcher by writing a freedesktop .desktop entry
# that points at this checkout. Re-run it if you move the folder.
set -euo pipefail
IFS=$'\n\t'

here="$(cd "$(dirname "$0")" && pwd)"
electron="$here/node_modules/electron/dist/electron"
[[ -x $electron ]] || { echo "Electron missing: run 'npm install' in $here first" >&2; exit 1; }

mkdir -p "$HOME/.local/share/applications"
cat >"$HOME/.local/share/applications/trader-desk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Salamander
Comment=Slow gold. Start the trader backend and watch the agent
Exec=$electron $here
Icon=$here/brand/salamander-icon.png
Terminal=false
Categories=Finance;Development;
StartupWMClass=trader-desk
EOF

echo "Installed ~/.local/share/applications/trader-desk.desktop"
