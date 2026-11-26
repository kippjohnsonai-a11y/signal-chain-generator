#!/bin/bash
# Installs Signal Chain Generator as a background service on the Mac mini and
# publishes it to your tailnet. Run from an admin account:
#
#     sudo bash /Users/<account>/stompxl/deploy/macos/install.sh <account>
#
# <account> is the short name of the account the app runs as (the one that
# holds the repo, the env file and the library). Safe to run again: it
# reinstalls the service and restarts it.
set -euo pipefail

ACCOUNT="${1:-}"
if [[ -z "$ACCOUNT" ]]; then
  echo "usage: sudo bash install.sh <account short name>" >&2
  exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
  echo "install.sh: run with sudo, from an admin account" >&2
  exit 1
fi
HOME_DIR="$(dscl . -read "/Users/$ACCOUNT" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [[ -z "$HOME_DIR" || ! -d "$HOME_DIR/stompxl" ]]; then
  echo "install.sh: no repo at ~$ACCOUNT/stompxl (clone it in that account first)" >&2
  exit 1
fi
ENV_FILE="$HOME_DIR/.config/stompxl/stompxl.env"
[[ -r "$ENV_FILE" ]] || { echo "install.sh: missing $ENV_FILE (step 2 of docs/mac-mini.md)" >&2; exit 1; }
PORT="$(sed -n 's/^STOMPXL_PORT=\([0-9]*\).*/\1/p' "$ENV_FILE" | tail -1)"
PORT="${PORT:-8780}"

LABEL=com.stompxl.server
PLIST="/Library/LaunchDaemons/$LABEL.plist"
sed -e "s|__USER__|$ACCOUNT|g" -e "s|__HOME__|$HOME_DIR|g" \
  "$HOME_DIR/stompxl/deploy/macos/$LABEL.plist" > "$PLIST"
chown root:wheel "$PLIST"
chmod 644 "$PLIST"
sudo -u "$ACCOUNT" mkdir -p "$HOME_DIR/Library/Logs"

launchctl bootout "system/$LABEL" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
echo "Service installed and started (log: $HOME_DIR/Library/Logs/stompxl.log)."

if command -v tailscale >/dev/null 2>&1; then
  tailscale serve --bg --https="$PORT" "http://127.0.0.1:$PORT"
  echo
  tailscale serve status
  echo "Open the https address above (port $PORT) on your phone and computers."
else
  echo "tailscale CLI not found. Then run: sudo tailscale serve --bg --https=$PORT http://127.0.0.1:$PORT"
fi
