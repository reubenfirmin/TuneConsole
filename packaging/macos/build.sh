#!/usr/bin/env bash
# Build "TuneConsole.app" and a .dmg with PyInstaller. Run on macOS.
#
#   ./build.sh
#
# Produces:
#   dist/TuneConsole.app
#   dist/TuneConsole-<version>.dmg   (version comes from the git tag)
#
# The bundle is intentionally unsigned. First launch needs right-click -> Open; see ../README.md.
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

# 4. Wrap it in a compressed drag-to-install .dmg.
APP="dist/TuneConsole.app"
DMG="dist/TuneConsole-$VERSION.dmg"
DMG_ROOT="dist/dmg-root"
mkdir -p "$DMG_ROOT"
cp -R "$APP" "$DMG_ROOT/TuneConsole.app"
ln -s /Applications "$DMG_ROOT/Applications"
hdiutil create -volname "TuneConsole" -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"
rm -rf "$DMG_ROOT"

echo
echo "Built:"
echo "  $APP"
echo "  $DMG"
