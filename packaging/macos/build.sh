#!/usr/bin/env bash
# Build "TuneConsole.app" and a .dmg with PyInstaller. Run on macOS.
#
#   ./build.sh
#
# Produces:
#   dist/TuneConsole.app
#   dist/TuneConsole-<version>.dmg   (version comes from the git tag)
#
# The bundle is unsigned, so first launch needs right-click -> Open (or System Settings ->
# Privacy & Security -> Open Anyway). Add an Apple Developer cert + notarization later for
# distribution; see ../README.md.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd ../.. && pwd)

# 1. Isolated build environment with the app + PyInstaller installed (so data/metadata collect).
python3 -m venv .build-venv
# shellcheck disable=SC1091
. .build-venv/bin/activate
python -m pip install -U pip wheel
python -m pip install "$ROOT" pyinstaller

# Version is derived from the git tag by hatch-vcs and baked into the installed package metadata,
# so nothing here needs bumping per release.
VERSION=$(python -c "import importlib.metadata as m; print(m.version('yt-playlist'))")

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
