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
export UV_PROJECT_ENVIRONMENT="$ROOT/packaging/macos/.build-venv"
export UV_HTTP_TIMEOUT=30
export UV_HTTP_RETRIES=2

if [[ "${1:-}" != --build-only ]]; then

# Sparkle is copied intact after PyInstaller so its signed nested helpers and framework
# symlinks are preserved. Pin and verify the upstream distribution before loading any tools.
SPARKLE_VERSION=2.10.0
SPARKLE_SHA256=c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c
mkdir -p .sparkle
if [[ ! -f ".sparkle/$SPARKLE_VERSION.verified" ]]; then
  if [[ ! -f .sparkle/distribution.tar.xz ]]; then
  curl --fail --location --connect-timeout 15 --max-time 120 --retry 2 --retry-max-time 180 \
    "https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/Sparkle-$SPARKLE_VERSION.tar.xz" \
    --output .sparkle/distribution.tar.xz.download
  echo "$SPARKLE_SHA256  .sparkle/distribution.tar.xz.download" | shasum -a 256 --check
  mv .sparkle/distribution.tar.xz.download .sparkle/distribution.tar.xz
  fi
  echo "$SPARKLE_SHA256  .sparkle/distribution.tar.xz" | shasum -a 256 --check
  tar -xJf .sparkle/distribution.tar.xz -C .sparkle
  touch ".sparkle/$SPARKLE_VERSION.verified"
fi
: "${SPARKLE_PUBLIC_ED_KEY:?Set SPARKLE_PUBLIC_ED_KEY to the release signing public key; see packaging/README.md}"
export SPARKLE_PUBLIC_ED_KEY

# 1. Isolated build environment with the app + PyInstaller installed (so data/metadata collect).
uv sync --project "$ROOT" --frozen --no-dev --group build --group macos --python 3.12 --no-install-project
# Freeze a regular installed wheel, using only the locked build dependencies.
uv sync --project "$ROOT" --frozen --no-dev --group build --group macos --python 3.12 --no-build-isolation --no-editable
fi
if [[ "${1:-}" == --prepare-only ]]; then exit 0; fi
: "${SPARKLE_PUBLIC_ED_KEY:?Set SPARKLE_PUBLIC_ED_KEY to the release signing public key}"
export SPARKLE_PUBLIC_ED_KEY
# shellcheck disable=SC1091
. "$UV_PROJECT_ENVIRONMENT/bin/activate"

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
