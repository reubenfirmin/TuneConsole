#!/usr/bin/env bash
# Build a Chrome Web Store upload package (a ZIP of the extension/ directory).
#
# The Web Store wants a plain ZIP of the extension's files, not a .crx: it repackages and signs the
# upload itself. It also FORBIDS the manifest `key` field, which we keep in source only to pin a
# stable id for local unpacked development, so this script strips `key` from the packaged manifest.
# Run: scripts/build-extension.sh  ->  dist/tuneconsole-extension-<version>.zip
set -euo pipefail

cd "$(dirname "$0")/.."
EXT_DIR="extension"
OUT_DIR="dist"

# Fail fast on a broken manifest, and read the version from it.
python3 -c "import json; json.load(open('$EXT_DIR/manifest.json'))"
VERSION="$(python3 -c "import json; print(json.load(open('$EXT_DIR/manifest.json'))['version'])")"

mkdir -p "$OUT_DIR"
ZIP="$OUT_DIR/tuneconsole-extension-$VERSION.zip"
ZIP_ABS="$(pwd)/$ZIP"
rm -f "$ZIP_ABS"

# Stage into a temp dir so we can ship a store-safe manifest (no `key`, no README, no dotfiles).
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$EXT_DIR/." "$STAGE/"
rm -f "$STAGE/README.md"
python3 - "$STAGE/manifest.json" <<'PY'
import json, sys
p = sys.argv[1]
m = json.load(open(p))
m.pop("key", None)     # the Chrome Web Store rejects uploads that contain a `key`
json.dump(m, open(p, "w"), indent=2)
PY

( cd "$STAGE" && zip -rq "$ZIP_ABS" . -x ".*" -x "*/.*" -x "*.map" )

echo "Built $ZIP (key stripped for the Web Store)"
echo "Contents:"
unzip -l "$ZIP"
