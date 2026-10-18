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
  local status=$?
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in {1..20}; do
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    # A broken quit path must fail CI rather than leave the cleanup waiting forever.
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      kill -KILL "$SERVER_PID" 2>/dev/null || true
    fi
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ "$status" -ne 0 && -f "$SMOKE_HOME/server.out" ]]; then
    cat "$SMOKE_HOME/server.out" >&2
  fi
  rm -rf "$SMOKE_HOME"
  exit "$status"
}
trap cleanup EXIT

test -x "$BIN"
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" | grep -qx 'com.tuneconsole.TuneConsole'
/usr/libexec/PlistBuddy -c 'Print :SUFeedURL' "$APP/Contents/Info.plist" | grep -q "appcast-macos-$(uname -m).xml"
/usr/libexec/PlistBuddy -c 'Print :SUPublicEDKey' "$APP/Contents/Info.plist" | grep -q .
test -x "$APP/Contents/Frameworks/Sparkle.framework/Sparkle"
codesign --verify --deep --strict "$APP"
"$BIN" --help >/dev/null

YT_PLAYLIST_HOME="$SMOKE_HOME" YT_PLAYLIST_NO_OPEN=1 TUNECONSOLE_DISABLE_UPDATES=1 \
  "$BIN" --port "$SMOKE_PORT" >"$SMOKE_HOME/server.out" 2>&1 &
SERVER_PID=$!

for _ in {1..80}; do
  if curl --connect-timeout 1 --max-time 2 --silent --show-error --fail "http://127.0.0.1:$SMOKE_PORT/" >/dev/null; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Packaged app exited before serving HTTP" >&2
    cat "$SMOKE_HOME/server.out" >&2
    exit 1
  fi
  sleep 0.25
done

curl --connect-timeout 1 --max-time 2 --silent --show-error --fail "http://127.0.0.1:$SMOKE_PORT/bridge/status" >/dev/null
curl --connect-timeout 1 --max-time 2 --silent --show-error --fail "http://127.0.0.1:$SMOKE_PORT/app-update/status" >/dev/null
test -f "$SMOKE_HOME/state.db"
test -f "$SMOKE_HOME/logs/app.log"
hdiutil verify dist/TuneConsole-*.dmg
# SIGTERM must reach Cocoa, gracefully stop uvicorn, and exit without a stranded process.
kill "$SERVER_PID"
for _ in {1..80}; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID"
    SERVER_PID=""
    break
  fi
  sleep 0.25
done
if [[ -n "$SERVER_PID" ]]; then
  echo "Packaged app did not exit after SIGTERM" >&2
  exit 1
fi
echo "Packaged app smoke test passed ($(uname -m))."
