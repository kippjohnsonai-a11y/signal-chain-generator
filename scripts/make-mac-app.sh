#!/bin/bash
# Build "Signal Chain Generator.app" in ~/Applications: a one-click launcher.
#
# Clicking the app runs scripts/launch.sh: pull the latest code, restart the
# server if it's running an older version, start it if it isn't, and open the
# browser. The logic lives in that script so it updates with the repo; run this
# one again only if you move the stompxl folder.
#
#     bash scripts/make-mac-app.sh

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/Signal Chain Generator.app"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    for candidate in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
        if [ -x "$candidate" ]; then UV="$candidate"; break; fi
    done
fi
if [ -z "$UV" ]; then
    echo "uv not found. Install it first: brew install uv" >&2
    exit 1
fi

SCRIPT="$(mktemp -t stompxl-launcher).applescript"
cat > "$SCRIPT" <<APPLESCRIPT
-- All the logic is in scripts/launch.sh, which updates with the repo.
try
    do shell script "STOMPXL_UV=" & quoted form of "$UV" & " /bin/bash " & quoted form of "$REPO/scripts/launch.sh"
on error errorMessage
    display dialog "Signal Chain Generator couldn't start: " & errorMessage buttons {"OK"} default button 1 with icon caution
end try
APPLESCRIPT

mkdir -p "$HOME/Applications"
rm -rf "$APP"
osacompile -o "$APP" "$SCRIPT"
rm -f "$SCRIPT"
echo "Created: $APP"
echo "Open it from Finder > Applications (your home folder's), then drag it to the Dock."
