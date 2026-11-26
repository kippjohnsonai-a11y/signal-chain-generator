#!/bin/bash
# What "Signal Chain Generator.app" does on each click:
#
#  1. If this version of the app is already running, open it in the browser.
#  2. If an older version is running, stop it.
#  3. Otherwise start the release this app was built for, with the copy of uv
#     inside the app (the first start downloads Python and the libraries, so it
#     takes a minute or two; later starts work offline).
#  4. Open it in the default browser.

set -u
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
UV="$CONTENTS/MacOS/uv"
VERSION="$(cat "$CONTENTS/Resources/VERSION")"
# One port per Mac user account, so a second user's click never opens the
# first user's library. The first account (uid 501) keeps 8765.
uid="$(id -u)"
PORT="${STOMPXL_PORT:-$(( uid >= 501 ? 8765 + uid - 501 : 8765 ))}"
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/Library/Logs/Signal Chain Generator.log"

say() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"Signal Chain Generator\"" >/dev/null 2>&1 || true
}
fail() {
    /usr/bin/osascript -e "display dialog \"Signal Chain Generator couldn't start. $1\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1 || true
    exit 1
}

up() { /usr/bin/curl -fs -o /dev/null "$URL"; }
running="$(/usr/bin/curl -fs "${URL}api/version" 2>/dev/null | sed -n 's/.*"commit": *"\([^"]*\)".*/\1/p')"

if up && [ "$running" != "$VERSION" ]; then
    pkill -U "$uid" -f "signal-chain-generator --no-browser --port $PORT" || true
    for _ in $(seq 20); do up || break; sleep 0.5; done
fi

if ! up; then
    mkdir -p "$(dirname "$LOG")"
    [ -d "$HOME/.cache/uv" ] || say "Setting up for the first time. This takes a minute or two."
    nohup "$UV" tool run --python 3.13 --from "signal-chain-generator==$VERSION" \
        signal-chain-generator --no-browser --port "$PORT" >"$LOG" 2>&1 </dev/null &
    for _ in $(seq 300); do up && break; sleep 1; done
fi

up || fail "Details are in ~/Library/Logs/Signal Chain Generator.log."
/usr/bin/open "$URL"
