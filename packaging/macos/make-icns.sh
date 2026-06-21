#!/usr/bin/env bash
# Render the app icon (svg -> YtPlaylist.icns) for the .app bundle. Best-effort: needs `rsvg-convert`
# (brew install librsvg) and the macOS `iconutil`. If either is missing, the build still works — the
# app just gets PyInstaller's default icon.
set -euo pipefail
cd "$(dirname "$0")"

SVG=../flatpak/io._4rc.YtPlaylist.svg
OUT=YtPlaylist.icns

if ! command -v rsvg-convert >/dev/null || ! command -v iconutil >/dev/null; then
  echo "rsvg-convert and/or iconutil not found — skipping custom icon."
  exit 1
fi

SET=$(mktemp -d)/icon.iconset
mkdir -p "$SET"
for s in 16 32 128 256 512; do
  rsvg-convert -w "$s"   -h "$s"   "$SVG" -o "$SET/icon_${s}x${s}.png"
  rsvg-convert -w $((s*2)) -h $((s*2)) "$SVG" -o "$SET/icon_${s}x${s}@2x.png"
done
iconutil -c icns "$SET" -o "$OUT"
echo "Wrote $(pwd)/$OUT"
