#!/usr/bin/env bash
# Build "TuneConsole.app" and a .dmg with PyInstaller. Run on macOS.
#
#   ./build.sh
#
# Produces:
#   dist/TuneConsole.app
#   dist/TuneConsole-0.1.3.dmg
#
# The bundle is unsigned, so first launch needs right-click -> Open (or System Settings ->
# Privacy & Security -> Open Anyway). Add an Apple Developer cert + notarization later for
# distribution; see ../README.md.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd ../.. && pwd)
VERSION=0.1.3

# 1. Isolated build environment with the app + PyInstaller installed (so data/metadata collect).
python3 -m venv .build-venv
# shellcheck disable=SC1091
. .build-venv/bin/activate
python -m pip install -U pip wheel
python -m pip install "$ROOT" pyinstaller

# 2. Optional custom icon (svg -> icns). Non-fatal if the tools are missing.
./make-icns.sh || true

# 3. Build the .app.
rm -rf build dist
pyinstaller --noconfirm --clean yt-playlist.spec

# 4. Wrap it in a compressed .dmg.
APP="dist/TuneConsole.app"
DMG="dist/TuneConsole-$VERSION.dmg"
hdiutil create -volname "TuneConsole" -srcfolder "$APP" -ov -format UDZO "$DMG"

echo
echo "Built:"
echo "  $APP"
echo "  $DMG"
