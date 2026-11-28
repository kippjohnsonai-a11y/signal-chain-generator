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

# The app can update itself (More > Updates): it writes the newer release to
# app-version in the data folder, and from then on this starts that release.
SUPPORT="$HOME/Library/Application Support"
DATA="$SUPPORT/Signal Chain Generator"
[ -d "$SUPPORT/StompXL Tone Studio" ] && DATA="$SUPPORT/StompXL Tone Studio"
[ -n "${STOMPXL_DATA_DIR:-}" ] && DATA="$STOMPXL_DATA_DIR"
# True when $1 is a plain release number (1.2.3) newer than $2.
newer() {
    [[ "$1" =~ ^[0-9]+(\.[0-9]+){1,3}$ ]] || return 1
    local i a b
    IFS=. read -ra a <<< "$1"
    IFS=. read -ra b <<< "$2"
    for i in 0 1 2 3; do
        (( 10#${a[i]:-0} > 10#${b[i]:-0} )) && return 0
        (( 10#${a[i]:-0} < 10#${b[i]:-0} )) && return 1
    done
    return 1
}
PINNED="$(tr -d '[:space:]' < "$DATA/app-version" 2>/dev/null || true)"
newer "$PINNED" "$VERSION" && VERSION="$PINNED"
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
    SCG_UV="$UV" SCG_LAUNCHER="$CONTENTS/Resources/launcher.sh" nohup "$UV" tool run --python 3.13 --from "signal-chain-generator==$VERSION" \
        signal-chain-generator --no-browser --port "$PORT" >"$LOG" 2>&1 </dev/null &
    for _ in $(seq 300); do up && break; sleep 1; done
fi

up || fail "Details are in ~/Library/Logs/Signal Chain Generator.log."
[ -n "${SCG_NO_OPEN:-}" ] || /usr/bin/open "$URL"
