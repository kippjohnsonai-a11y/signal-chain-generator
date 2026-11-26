#!/bin/bash
# What the "Signal Chain Generator" app runs on every click. It lives in the repo,
# so a `git pull` updates the launcher too.
#
#  1. Pull the latest code (carry on with what's there if offline).
#  2. If the app is running an older version, restart it.
#  3. If it isn't running, start it in the background.
#  4. Open it in the default browser.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# One port per Mac user account, so a second user's click never opens the first
# user's server (and their library and API key). The first account (uid 501)
# keeps 8765; the next gets 8766, and so on. STOMPXL_PORT overrides.
uid="$(id -u)"
PORT="${STOMPXL_PORT:-$(( uid >= 501 ? 8765 + uid - 501 : 8765 ))}"
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/Library/Logs/stompxl.log"
UV="${STOMPXL_UV:-$(command -v uv || true)}"
for candidate in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
    [ -z "$UV" ] && [ -x "$candidate" ] && UV="$candidate"
done
[ -z "$UV" ] && { echo "uv not found: brew install uv" >&2; exit 1; }

cd "$REPO" || exit 1
git pull --ff-only -q >/dev/null 2>&1 || true
latest="$(git rev-parse --short HEAD 2>/dev/null)"

# The root, not /api/version: a server from before that endpoint existed must
# still count as running, so it gets restarted rather than started twice.
up() { /usr/bin/curl -fs -o /dev/null "$URL"; }
running="$(/usr/bin/curl -fs "${URL}api/version" 2>/dev/null | sed -n 's/.*"commit": *"\([^"]*\)".*/\1/p')"

if up && [ -n "$latest" ] && [ "$running" != "$latest" ]; then
    # Only the server process: its command line runs the venv's stompxl script.
    pkill -U "$uid" -f "\.venv/bin/stompxl" || true
    for _ in $(seq 20); do up || break; sleep 0.5; done
fi

if ! up; then
    mkdir -p "$(dirname "$LOG")"
    nohup "$UV" run stompxl --no-browser --port "$PORT" >"$LOG" 2>&1 </dev/null &
    # The first run installs Python and the libraries, so allow a few minutes.
    for _ in $(seq 240); do up && break; sleep 1; done
fi

up || { echo "The app didn't start. Details: $LOG" >&2; exit 1; }
/usr/bin/open "$URL"
