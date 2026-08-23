#!/usr/bin/env bash
# Smoke-test the actual frozen app, not the source checkout. Run on macOS after build.sh.
set -euo pipefail

cd "$(dirname "$0")"
APP=${1:-dist/TuneConsole.app}
BIN="$APP/Contents/MacOS/yt-playlist"
SMOKE_HOME=$(mktemp -d)
SMOKE_PORT=18765
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID"
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$SMOKE_HOME"
}
trap cleanup EXIT

test -x "$BIN"
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" | grep -qx 'com.tuneconsole.TuneConsole'
"$BIN" --help >/dev/null

YT_PLAYLIST_HOME="$SMOKE_HOME" YT_PLAYLIST_NO_OPEN=1 \
  "$BIN" --port "$SMOKE_PORT" >"$SMOKE_HOME/server.out" 2>&1 &
SERVER_PID=$!

for _ in {1..80}; do
  if curl --silent --show-error --fail "http://127.0.0.1:$SMOKE_PORT/" >/dev/null; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Packaged app exited before serving HTTP" >&2
    cat "$SMOKE_HOME/server.out" >&2
    exit 1
  fi
  sleep 0.25
done

curl --silent --show-error --fail "http://127.0.0.1:$SMOKE_PORT/bridge/status" >/dev/null
test -f "$SMOKE_HOME/state.db"
test -f "$SMOKE_HOME/logs/app.log"
hdiutil verify dist/TuneConsole-*.dmg
echo "Packaged app smoke test passed ($(uname -m))."
