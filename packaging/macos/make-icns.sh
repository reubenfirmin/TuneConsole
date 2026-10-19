#!/usr/bin/env bash
# Build the app icon (TuneConsole.icns) for the .app bundle from the shared logo PNG. Runs on macOS,
# using the built-in `sips` (resize) + `iconutil` (pack). Best-effort: if the tools are missing the
# build still works — the app just gets PyInstaller's default icon.
#
# TuneConsole.png is a 1024px export of src/yt_playlist/web/static/favicon.svg. All sizes are
# downsampled from that master, including the Retina @2x tiles.
set -euo pipefail
cd "$(dirname "$0")"

SRC=TuneConsole.png
OUT=TuneConsole.icns

if ! command -v sips >/dev/null || ! command -v iconutil >/dev/null; then
  echo "sips and/or iconutil not found — skipping custom icon."
  exit 1
fi

SET=$(mktemp -d)/icon.iconset
mkdir -p "$SET"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s"          "$SRC" --out "$SET/icon_${s}x${s}.png"    >/dev/null
  sips -z $((s*2)) $((s*2))  "$SRC" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$OUT"
echo "Wrote $(pwd)/$OUT"
