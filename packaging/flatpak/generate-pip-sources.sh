#!/usr/bin/env bash
# Generate python3-requirements.json — the offline pip sources the Flatpak build installs.
#
# This is the one step that needs network (and the freedesktop SDK). It runs pip *inside* the SDK
# via flatpak-pip-generator so the resolved wheels match the runtime's Python version and arch,
# then writes python3-requirements.json next to the manifest. Re-run it when deps change.
set -euo pipefail
cd "$(dirname "$0")"

RUNTIME_VERSION=24.08

if ! flatpak info org.freedesktop.Sdk//$RUNTIME_VERSION >/dev/null 2>&1; then
  echo "Installing org.freedesktop.Sdk//$RUNTIME_VERSION (needed to resolve matching wheels)…"
  flatpak install -y flathub org.freedesktop.Sdk//$RUNTIME_VERSION
fi

GEN=$(command -v flatpak-pip-generator || true)
if [ -z "$GEN" ]; then
  echo "Fetching flatpak-pip-generator…"
  curl -fsSL -o /tmp/flatpak-pip-generator \
    https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator
  GEN="python3 /tmp/flatpak-pip-generator"
fi

# Keep this list in sync with [project.dependencies] in ../../pyproject.toml (minus tomli, which is
# only needed on Python < 3.11 — the runtime ships a newer Python).
$GEN --runtime="org.freedesktop.Sdk//$RUNTIME_VERSION" \
  --output python3-requirements \
  ytmusicapi fastapi uvicorn jinja2 python-multipart rapidfuzz

echo "Wrote $(pwd)/python3-requirements.json"
