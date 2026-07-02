#!/usr/bin/env bash
# Build the app icon (TuneConsole.icns) for the .app bundle from the shared mixer PNG. Runs on macOS,
# using the built-in `sips` (resize) + `iconutil` (pack). Best-effort: if the tools are missing the
# build still works — the app just gets PyInstaller's default icon.
#
# Source is the same 256px mixer icon used by the web app, extension, and Flatpak (TuneConsole.png, a
# copy of the shared icon). Sizes above 256 are upscaled; drop in a larger master PNG for crisper
# Retina @2x tiles.
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
