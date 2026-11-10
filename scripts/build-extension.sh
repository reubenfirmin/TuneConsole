#!/usr/bin/env bash
# Build a Chrome Web Store upload package (a ZIP of the extension/ directory).
#
# The Web Store wants a plain ZIP of the extension's files: it repackages and signs the upload.
# Public builds include the verified audio runtime and omit the development `key`.
# Run: scripts/build-extension.sh
set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "Usage: $0" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
EXT_DIR="extension"
OUT_DIR="dist"

# Fail fast on a broken manifest, and read the version from it.
python3 -c "import json; json.load(open('$EXT_DIR/manifest.json'))"
VERSION="$(python3 -c "import json; print(json.load(open('$EXT_DIR/manifest.json'))['version'])")"

mkdir -p "$OUT_DIR"
ZIP="$OUT_DIR/tuneconsole-extension-$VERSION.zip"
ZIP_ABS="$(pwd)/$ZIP"

# Stage into a temp dir so the source manifest stays unchanged.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$EXT_DIR/." "$STAGE/"
rm -f "$STAGE/README.md"
python3 scripts/audio_model/bundle.py --destination "$STAGE/audio_model"
python3 - "$STAGE" <<'PY'
import json
import sys
from pathlib import Path

stage = Path(sys.argv[1])
manifest = stage / "manifest.json"
m = json.loads(manifest.read_text())
m.pop("key", None)  # omit the unpacked development identity from the store package
manifest.write_text(json.dumps(m, indent=2) + "\n")

PY

# Use a fresh ZIP so removed source files cannot survive an incremental rebuild.
ARCHIVE="$STAGE/package.zip"
( cd "$STAGE" && zip -rq "$ARCHIVE" . -x ".*" -x "*/.*" -x "*.map" -x "package.zip" )
mv "$ARCHIVE" "$ZIP_ABS"

echo "Built $ZIP"
echo "Development key stripped for the Web Store."
echo "Contents:"
unzip -l "$ZIP"
