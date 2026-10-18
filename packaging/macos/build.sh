#!/usr/bin/env bash
# Build "TuneConsole.app" and a .dmg with PyInstaller. Run on macOS.
#
#   ./build.sh
#
# Produces:
#   dist/TuneConsole.app
#   dist/TuneConsole-<version>.dmg   (version comes from the git tag)
#
# The bundle is ad-hoc signed. First launch needs right-click -> Open; see ../README.md.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd ../.. && pwd)

# Sparkle is copied intact after PyInstaller so its signed nested helpers and framework
# symlinks are preserved. Pin and verify the upstream distribution before loading any tools.
SPARKLE_VERSION=2.10.0
SPARKLE_SHA256=c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c
mkdir -p .sparkle
if [[ ! -f ".sparkle/$SPARKLE_VERSION.verified" ]]; then
  curl --fail --location --retry 3 \
    "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz" \
    --output .sparkle/distribution.tar.xz
  echo "$SPARKLE_SHA256  .sparkle/distribution.tar.xz" | shasum -a 256 --check
  tar -xJf .sparkle/distribution.tar.xz -C .sparkle
  touch ".sparkle/$SPARKLE_VERSION.verified"
fi
: "${SPARKLE_PUBLIC_ED_KEY:?Set SPARKLE_PUBLIC_ED_KEY to the release signing public key; see packaging/README.md}"
export SPARKLE_PUBLIC_ED_KEY

# 1. Isolated build environment with the app + PyInstaller installed (so data/metadata collect).
python3 -m venv .build-venv
# shellcheck disable=SC1091
. .build-venv/bin/activate
python -m pip install -U pip wheel
python -m pip install "$ROOT" pyinstaller 'pyobjc-framework-Cocoa>=11,<13'

# Version is derived from the git tag by hatch-vcs and baked into the installed package metadata,
# so nothing here needs bumping per release.
VERSION=$(python -c "import importlib.metadata as m; print(m.version('yt-playlist'))")

# 2. Optional custom icon (svg -> icns). Non-fatal if the tools are missing.
./make-icns.sh || true

# 3. Build the .app.
rm -rf build dist
pyinstaller --noconfirm --clean yt-playlist.spec

# Embed Sparkle with its existing helper signatures, then refresh only the outer ad-hoc
# signature. Developer ID signing/notarization can replace this outer signature later.
ditto .sparkle/Sparkle.framework dist/TuneConsole.app/Contents/Frameworks/Sparkle.framework
codesign --force --sign - dist/TuneConsole.app
codesign --verify --deep --strict dist/TuneConsole.app

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
