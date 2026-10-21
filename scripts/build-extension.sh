#!/usr/bin/env bash
# Build a Chrome Web Store upload package (a ZIP of the extension/ directory).
#
# The Web Store wants a plain ZIP of the extension's files: it repackages and signs the upload.
# Public builds omit the manifest's development `key`; internal previews retain it for a stable
# unpacked extension id.
# Run: scripts/build-extension.sh for a store package with radio disabled.
#      scripts/build-extension.sh --enable-radio for an internal radio preview package.
set -euo pipefail

RADIO_ENABLED=false
case "${1:-}" in
  "") ;;
  --enable-radio) RADIO_ENABLED=true; shift ;;
  *) echo "Usage: $0 [--enable-radio]" >&2; exit 2 ;;
esac
if [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--enable-radio]" >&2
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
if [ "$RADIO_ENABLED" = true ]; then
  ZIP="$OUT_DIR/tuneconsole-extension-$VERSION-radio-preview.zip"
fi
ZIP_ABS="$(pwd)/$ZIP"
rm -f "$ZIP_ABS"

# Stage into a temp dir so the source manifest and build flags stay unchanged.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$EXT_DIR/." "$STAGE/"
rm -f "$STAGE/README.md"
python3 - "$STAGE" "$RADIO_ENABLED" <<'PY'
import json
import sys
from pathlib import Path

stage = Path(sys.argv[1])
radio_enabled = sys.argv[2] == "true"
manifest = stage / "manifest.json"
m = json.loads(manifest.read_text())
if radio_enabled:
    m["name"] += " (radio preview)"
    m["permissions"].append("storage")  # radio persists deck state across worker restarts
else:
    m.pop("key", None)  # omit the unpacked development identity from the store package
manifest.write_text(json.dumps(m, indent=2) + "\n")

# Both execution contexts must agree. Fail the build if a source flag was changed independently.
flag = "const RADIO_ENABLED = false;"
for name in ("background.js", "content.js"):
    path = stage / name
    source = path.read_text()
    if source.count(flag) != 1:
        raise SystemExit(f"{name}: expected exactly one disabled radio build flag")
    path.write_text(source.replace(flag, f"const RADIO_ENABLED = {str(radio_enabled).lower()};"))
PY

( cd "$STAGE" && zip -rq "$ZIP_ABS" . -x ".*" -x "*/.*" -x "*.map" )

echo "Built $ZIP"
echo "Radio enabled: $RADIO_ENABLED"
if [ "$RADIO_ENABLED" = true ]; then
  echo "Internal preview: development key retained for unpacked loading; do not submit to the Web Store."
else
  echo "Development key stripped for the Web Store."
fi
echo "Contents:"
unzip -l "$ZIP"
