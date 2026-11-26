#!/bin/bash
# Starts Signal Chain Generator on the Mac mini. launchd runs this as the account
# the app lives in (see com.stompxl.server.plist and docs/mac-mini.md), whether
# or not anyone is logged in. Settings come from an env file only that account
# can read; the library lives in that account's Application Support folder, so
# every phone and computer that opens the app sees the same songs.
set -euo pipefail

ENV_FILE="${STOMPXL_ENV_FILE:-$HOME/.config/stompxl/stompxl.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "start-stompxl: missing $ENV_FILE (copy deploy/macos/stompxl.env.example)" >&2
  exit 1
fi
if [[ "$(stat -f '%Lp' "$ENV_FILE")" != "600" ]]; then
  echo "start-stompxl: $ENV_FILE must be chmod 600" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
# Pick up new code on every (re)start; carry on with what's here if offline.
git pull --ff-only -q || echo "start-stompxl: git pull failed, starting the code already here" >&2

# Listen on this Mac only. Tailscale Serve publishes it to your tailnet over
# HTTPS (needed for the phone's USB access), and STOMPXL_ALLOWED_LOGINS limits
# it to your own Tailscale login.
exec uv run stompxl --no-browser --host 127.0.0.1 --port "${STOMPXL_PORT:-8780}"
